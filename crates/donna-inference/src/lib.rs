//! Managed `llama-server` child-process backend behind [`Inference`].

mod frontier;

pub use frontier::{MockProvider, OpenAiCompatibleProvider};

use donna_core::ToolCall;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use thiserror::Error;

/// Last live llama-server child pid (0 = none). Used for SIGTERM/SIGINT emergency unload.
static LIVE_CHILD_PID: AtomicU32 = AtomicU32::new(0);

/// Kill any tracked llama-server child (and its process group). Safe from signal handlers.
pub fn emergency_kill_child() {
    let pid = LIVE_CHILD_PID.swap(0, Ordering::SeqCst);
    if pid != 0 {
        // Kill the process group first (child was started in its own group).
        let _ = Command::new("kill")
            .args(["-TERM", &format!("-{pid}")])
            .status();
        let _ = Command::new("kill")
            .args(["-KILL", &format!("-{pid}")])
            .status();
        let _ = Command::new("kill")
            .args(["-KILL", &pid.to_string()])
            .status();
    }
    // Reap any remaining direct children (covers Drop/signal races that cleared LIVE_CHILD_PID).
    let me = std::process::id().to_string();
    let _ = Command::new("pkill").args(["-KILL", "-P", &me]).status();
}

/// Configuration for a managed `llama-server` process.
#[derive(Debug, Clone)]
pub struct Config {
    /// Binary name or path. Overridden by `DONNA_LLAMA_SERVER` when set.
    pub binary: String,
    pub model_path: PathBuf,
    pub ctx: u32,
    /// `0` picks a free local port (TOCTOU-acceptable for local use).
    pub port: u16,
    pub extra_args: Vec<String>,
    /// Named LoRA adapters `(name, path)` loaded at startup.
    pub loras: Vec<(String, PathBuf)>,
    /// Idle unload timeout. Default 300s.
    pub idle_unload: Duration,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            binary: "llama-server".into(),
            model_path: PathBuf::new(),
            ctx: 4096,
            port: 0,
            extra_args: Vec::new(),
            loras: Vec::new(),
            idle_unload: Duration::from_secs(300),
        }
    }
}

/// One chat message for `/v1/chat/completions`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

/// Generation request.
#[derive(Debug, Clone)]
pub struct Request {
    pub messages: Vec<ChatMessage>,
    pub json_schema: Option<Value>,
    pub tools: Option<Value>,
    pub max_tokens: u32,
    pub temperature: f32,
    pub seed: i64,
    /// Apply named LoRA at `scale`, or clear adapters when `None`.
    pub lora: Option<(String, f32)>,
    /// When set, request top-N token logprobs (llama-server `n_probs` / OpenAI `top_logprobs`).
    pub logprobs: Option<u32>,
}

impl Request {
    pub fn new(messages: Vec<ChatMessage>, max_tokens: u32) -> Self {
        Self {
            messages,
            json_schema: None,
            tools: None,
            max_tokens,
            temperature: 0.0,
            seed: default_seed(),
            lora: None,
            logprobs: None,
        }
    }
}

fn default_seed() -> i64 {
    std::env::var("DONNA_SEED")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0)
}

/// One sampled completion token with optional top-N alternatives.
#[derive(Debug, Clone)]
pub struct TokenLogprob {
    pub token: String,
    pub logprob: f32,
    /// Competing tokens at this position `(token, logprob)`.
    pub top: Vec<(String, f32)>,
}

/// Generation response.
#[derive(Debug, Clone)]
pub struct Response {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub latency_ms: u64,
    /// Per completion token logprobs when requested; empty if absent.
    pub token_logprobs: Vec<TokenLogprob>,
}

#[derive(Debug, Error)]
pub enum InferenceError {
    #[error("model not loaded")]
    NotLoaded,
    #[error("failed to spawn llama-server: {0}")]
    Spawn(String),
    #[error("llama-server health check timed out")]
    HealthTimeout,
    #[error("HTTP error: {0}")]
    Http(String),
    #[error("JSON error: {0}")]
    Json(String),
    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, InferenceError>;

/// Local model lifecycle and generation via managed `llama-server`.
pub trait Inference {
    fn load(&mut self) -> Result<()>;
    fn unload(&mut self) -> Result<()>;
    fn generate(&mut self, req: &Request) -> Result<Response>;
    fn rss_bytes(&self) -> u64;
    fn pid(&self) -> Option<u32>;
}

struct Inner {
    child: Option<Child>,
    port: Option<u16>,
    /// Injected base URL for unit tests (skips real child).
    base_url: Option<String>,
    generation: u64,
    last_activity: Instant,
    /// Current LoRA scales by adapter index.
    lora_scales: Vec<f32>,
    /// Name → index into `config.loras` / `lora_scales`.
    lora_index: HashMap<String, usize>,
    stop_idle: bool,
}

/// Managed `llama-server` backend.
pub struct LlamaServer {
    config: Config,
    inner: Arc<Mutex<Inner>>,
    idle_cv: Arc<Condvar>,
}

impl LlamaServer {
    pub fn new(config: Config) -> Self {
        let lora_index: HashMap<String, usize> = config
            .loras
            .iter()
            .enumerate()
            .map(|(i, (name, _))| (name.clone(), i))
            .collect();
        let n = config.loras.len();
        Self {
            config,
            inner: Arc::new(Mutex::new(Inner {
                child: None,
                port: None,
                base_url: None,
                generation: 0,
                last_activity: Instant::now(),
                lora_scales: vec![0.0; n],
                lora_index,
                stop_idle: false,
            })),
            idle_cv: Arc::new(Condvar::new()),
        }
    }

    fn resolve_binary(&self) -> String {
        std::env::var("DONNA_LLAMA_SERVER").unwrap_or_else(|_| self.config.binary.clone())
    }

    fn pick_port(configured: u16) -> Result<u16> {
        if configured != 0 {
            return Ok(configured);
        }
        let listener = TcpListener::bind("127.0.0.1:0")
            .map_err(|e| InferenceError::Spawn(format!("bind free port: {e}")))?;
        let port = listener
            .local_addr()
            .map_err(|e| InferenceError::Spawn(format!("local_addr: {e}")))?
            .port();
        drop(listener);
        Ok(port)
    }

    fn base_url_locked(inner: &Inner) -> Result<String> {
        if let Some(url) = &inner.base_url {
            return Ok(url.clone());
        }
        let port = inner.port.ok_or(InferenceError::NotLoaded)?;
        Ok(format!("http://127.0.0.1:{port}"))
    }

    fn wait_healthy(port: u16, child: &mut Child, timeout: Duration) -> Result<()> {
        let url = format!("http://127.0.0.1:{port}/health");
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some(status) = child.try_wait().ok().flatten() {
                return Err(InferenceError::Spawn(format!(
                    "llama-server exited early: {status}"
                )));
            }
            match ureq::get(&url).timeout(Duration::from_secs(1)).call() {
                Ok(resp) if resp.status() == 200 => return Ok(()),
                _ => thread::sleep(Duration::from_millis(200)),
            }
        }
        Err(InferenceError::HealthTimeout)
    }

    fn kill_child(child: &mut Child) {
        let pid = child.id();
        LIVE_CHILD_PID
            .compare_exchange(pid, 0, Ordering::SeqCst, Ordering::SeqCst)
            .ok();
        // Process-group TERM (child started with setpgid).
        let _ = Command::new("kill")
            .args(["-TERM", &format!("-{pid}")])
            .status();
        let _ = child.terminate();
        match child.wait_timeout(Duration::from_secs(5)) {
            Ok(Some(_)) => {}
            _ => {
                let _ = Command::new("kill")
                    .args(["-KILL", &format!("-{pid}")])
                    .status();
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }

    fn spawn_idle_watcher(&self, generation: u64) {
        let inner = Arc::clone(&self.inner);
        let cv = Arc::clone(&self.idle_cv);
        let idle = self.config.idle_unload;
        thread::Builder::new()
            .name("donna-inference-idle".into())
            .spawn(move || loop {
                let guard = inner.lock().unwrap();
                if guard.stop_idle || guard.generation != generation || guard.child.is_none() {
                    return;
                }
                let elapsed = guard.last_activity.elapsed();
                if elapsed >= idle {
                    let mut g = guard;
                    if g.generation == generation && g.child.is_some() {
                        if let Some(mut child) = g.child.take() {
                            Self::kill_child(&mut child);
                        }
                        g.port = None;
                        g.lora_scales.fill(0.0);
                        // Keep generation so a late wake does not resurrect; next load bumps it.
                    }
                    return;
                }
                let wait = idle.saturating_sub(elapsed);
                let (_g, _) = cv.wait_timeout(guard, wait).unwrap();
            })
            .expect("spawn idle watcher");
    }

    fn touch_activity(inner: &mut Inner, cv: &Condvar) {
        inner.last_activity = Instant::now();
        cv.notify_all();
    }

    fn apply_lora(&self, base: &str, req_lora: &Option<(String, f32)>) -> Result<()> {
        let inner = self.inner.lock().unwrap();
        if inner.lora_index.is_empty() {
            return Ok(());
        }
        let mut desired = vec![0.0_f32; inner.lora_scales.len()];
        if let Some((name, scale)) = req_lora {
            let idx = *inner
                .lora_index
                .get(name)
                .ok_or_else(|| InferenceError::Other(format!("unknown LoRA adapter: {name}")))?;
            desired[idx] = *scale;
        }
        if desired == inner.lora_scales {
            return Ok(());
        }
        let body: Vec<Value> = desired
            .iter()
            .enumerate()
            .map(|(id, scale)| json!({"id": id, "scale": scale}))
            .collect();
        drop(inner);

        let url = format!("{base}/lora-adapters");
        let resp = ureq::post(&url)
            .set("Content-Type", "application/json")
            .send_json(Value::Array(body))
            .map_err(|e| InferenceError::Http(e.to_string()))?;
        if !(200..300).contains(&resp.status()) {
            return Err(InferenceError::Http(format!(
                "lora-adapters HTTP {}",
                resp.status()
            )));
        }

        let mut inner = self.inner.lock().unwrap();
        inner.lora_scales = desired;
        Ok(())
    }

    fn build_chat_body(req: &Request) -> Value {
        let mut body = json!({
            "model": "local",
            "messages": req.messages,
            "temperature": req.temperature,
            "seed": req.seed,
            "max_tokens": req.max_tokens,
            "stream": false,
        });
        if let Some(tools) = &req.tools {
            body["tools"] = tools.clone();
            body["tool_choice"] = json!("auto");
        }
        if let Some(schema) = &req.json_schema {
            body["response_format"] = json!({
                "type": "json_schema",
                "json_schema": {
                    "name": "donna",
                    "strict": true,
                    "schema": schema,
                }
            });
        }
        if let Some(n) = req.logprobs {
            // OpenAI-style + llama-server `n_probs` for broader build coverage.
            body["logprobs"] = json!(true);
            body["top_logprobs"] = json!(n);
            body["n_probs"] = json!(n);
        }
        body
    }

    fn post_chat(&self, base: &str, req: &Request) -> Result<(Value, u64)> {
        let url = format!("{base}/v1/chat/completions");
        let primary = Self::build_chat_body(req);
        let t0 = Instant::now();
        match Self::post_json(&url, &primary) {
            Ok(v) => Ok((v, t0.elapsed().as_millis() as u64)),
            Err(e) if req.json_schema.is_some() => {
                // Fallback: some builds reject nested json_schema; try json_object.
                let mut fallback = primary;
                fallback["response_format"] = json!({"type": "json_object"});
                let t1 = Instant::now();
                match Self::post_json(&url, &fallback) {
                    Ok(v) => Ok((v, t1.elapsed().as_millis() as u64)),
                    Err(_) => Err(e),
                }
            }
            Err(e) => Err(e),
        }
    }

    fn post_json(url: &str, body: &Value) -> Result<Value> {
        let resp = ureq::post(url)
            .set("Content-Type", "application/json")
            .timeout(Duration::from_secs(180))
            .send_json(body.clone())
            .map_err(|e| match e {
                ureq::Error::Status(code, resp) => {
                    let detail = resp.into_string().unwrap_or_default();
                    InferenceError::Http(format!(
                        "HTTP {code}: {}",
                        &detail[..detail.len().min(500)]
                    ))
                }
                other => InferenceError::Http(other.to_string()),
            })?;
        resp.into_json::<Value>()
            .map_err(|e| InferenceError::Json(e.to_string()))
    }

    fn parse_response(payload: &Value, latency_ms: u64) -> Result<Response> {
        let message = payload
            .pointer("/choices/0/message")
            .ok_or_else(|| InferenceError::Json("missing choices[0].message".into()))?;
        let text = message
            .get("content")
            .and_then(|c| c.as_str())
            .unwrap_or("")
            .to_string();
        let tool_calls = parse_tool_calls(message);
        let prompt_tokens = payload
            .pointer("/usage/prompt_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32;
        let completion_tokens = payload
            .pointer("/usage/completion_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32;
        let token_logprobs = parse_token_logprobs(payload);
        Ok(Response {
            text,
            tool_calls,
            prompt_tokens,
            completion_tokens,
            latency_ms,
            token_logprobs,
        })
    }

    /// Test helper: treat `base_url` as an already-loaded server (no child).
    #[cfg(test)]
    fn with_base_url(config: Config, base_url: String) -> Self {
        let s = Self::new(config);
        let mut inner = s.inner.lock().unwrap();
        inner.base_url = Some(base_url);
        inner.port = Some(0);
        inner.generation = 1;
        drop(inner);
        s
    }
}

/// Tolerant parse of OpenAI-style `choices[0].logprobs.content` (and a few aliases).
fn parse_token_logprobs(payload: &Value) -> Vec<TokenLogprob> {
    let content = payload
        .pointer("/choices/0/logprobs/content")
        .or_else(|| payload.pointer("/choices/0/logprobs/tokens"))
        .and_then(|v| v.as_array());
    let Some(arr) = content else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in arr {
        // Object form: {token, logprob, top_logprobs: [{token, logprob}, ...]}
        if item.is_object() {
            let token = item
                .get("token")
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .to_string();
            let logprob = item
                .get("logprob")
                .and_then(|v| v.as_f64())
                .unwrap_or(f64::NEG_INFINITY) as f32;
            let mut top = Vec::new();
            if let Some(tops) = item.get("top_logprobs").and_then(|v| v.as_array()) {
                for t in tops {
                    let tok = t
                        .get("token")
                        .and_then(|x| x.as_str())
                        .unwrap_or("")
                        .to_string();
                    let lp = t
                        .get("logprob")
                        .and_then(|x| x.as_f64())
                        .unwrap_or(f64::NEG_INFINITY) as f32;
                    if !tok.is_empty() {
                        top.push((tok, lp));
                    }
                }
            }
            if !token.is_empty() {
                out.push(TokenLogprob {
                    token,
                    logprob,
                    top,
                });
            }
            continue;
        }
        // String token with parallel arrays (older shapes): skip if not object.
    }
    out
}

fn parse_tool_calls(message: &Value) -> Vec<ToolCall> {
    let Some(arr) = message.get("tool_calls").and_then(|v| v.as_array()) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for tc in arr {
        let name = tc
            .pointer("/function/name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let args_str = tc
            .pointer("/function/arguments")
            .and_then(|v| v.as_str())
            .unwrap_or("{}");
        let args: Value = serde_json::from_str(args_str).unwrap_or_else(|_| json!({}));
        if !name.is_empty() {
            out.push(ToolCall { name, args });
        }
    }
    out
}

fn rss_bytes_for_pid(pid: u32) -> u64 {
    let output = Command::new("ps")
        .args(["-o", "rss=", "-p", &pid.to_string()])
        .output();
    match output {
        Ok(out) if out.status.success() => {
            let s = String::from_utf8_lossy(&out.stdout);
            let kb: u64 = s.trim().parse().unwrap_or(0);
            kb.saturating_mul(1024)
        }
        _ => 0,
    }
}

/// Child wait with timeout (no external crate).
trait WaitTimeout {
    fn wait_timeout(&mut self, dur: Duration) -> std::io::Result<Option<std::process::ExitStatus>>;
    fn terminate(&mut self) -> std::io::Result<()>;
}

impl WaitTimeout for Child {
    fn terminate(&mut self) -> std::io::Result<()> {
        // Prefer SIGTERM so llama-server can flush; fall back handled by caller via kill().
        let _ = Command::new("kill")
            .args(["-TERM", &self.id().to_string()])
            .status();
        Ok(())
    }

    fn wait_timeout(&mut self, dur: Duration) -> std::io::Result<Option<std::process::ExitStatus>> {
        let deadline = Instant::now() + dur;
        loop {
            if let Some(status) = self.try_wait()? {
                return Ok(Some(status));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            thread::sleep(Duration::from_millis(50));
        }
    }
}

impl Inference for LlamaServer {
    fn load(&mut self) -> Result<()> {
        // Unload any existing child first.
        let _ = self.unload();

        let port = Self::pick_port(self.config.port)?;
        let binary = self.resolve_binary();
        let mut cmd = Command::new(&binary);
        cmd.arg("-m")
            .arg(&self.config.model_path)
            .arg("-c")
            .arg(self.config.ctx.to_string())
            .arg("--port")
            .arg(port.to_string())
            .arg("--jinja")
            .arg("-np")
            .arg("1")
            .arg("--reasoning")
            .arg("off")
            .arg("--reasoning-budget")
            .arg("0")
            // Host prompt cache default is 8192 MiB; uncapped RSS blew the 3.5 GB M1 gate.
            // Sheet-dev (15 cases) on M1 Pro: --cache-ram 256 → p50≈3859 ms / max RSS≈1.87G;
            // 512 → p50≈3173 ms / max RSS≈2.09G. Both ≤3.5G; pick 512 for latency.
            // Override with DONNA_CACHE_RAM or Config.extra_args (later flag wins).
            .arg("--cache-ram")
            .arg(std::env::var("DONNA_CACHE_RAM").unwrap_or_else(|_| "512".into()));

        for (_name, path) in &self.config.loras {
            cmd.arg("--lora").arg(path);
        }
        if !self.config.loras.is_empty() {
            cmd.arg("--lora-init-without-apply");
        }
        for a in &self.config.extra_args {
            cmd.arg(a);
        }
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
        // Own process group so SUT SIGTERM/Drop can kill the whole tree.
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            // SAFETY: pre_exec runs in the child after fork, before exec.
            unsafe {
                cmd.pre_exec(|| {
                    // libc is linked via the libc crate dependency.
                    #[allow(unused_unsafe)]
                    {
                        if ::libc::setpgid(0, 0) != 0 {
                            return Err(std::io::Error::last_os_error());
                        }
                    }
                    Ok(())
                });
            }
        }

        let mut child = cmd
            .spawn()
            .map_err(|e| InferenceError::Spawn(format!("{binary}: {e}")))?;

        match Self::wait_healthy(port, &mut child, Duration::from_secs(120)) {
            Ok(()) => {}
            Err(e) => {
                Self::kill_child(&mut child);
                return Err(e);
            }
        }

        LIVE_CHILD_PID.store(child.id(), Ordering::SeqCst);

        let generation = {
            let mut inner = self.inner.lock().unwrap();
            inner.child = Some(child);
            inner.port = Some(port);
            inner.base_url = None;
            inner.generation = inner.generation.wrapping_add(1);
            inner.lora_scales.fill(0.0);
            inner.stop_idle = false;
            Self::touch_activity(&mut inner, &self.idle_cv);
            inner.generation
        };
        self.spawn_idle_watcher(generation);
        Ok(())
    }

    fn unload(&mut self) -> Result<()> {
        let mut inner = self.inner.lock().unwrap();
        inner.stop_idle = true;
        self.idle_cv.notify_all();
        if let Some(mut child) = inner.child.take() {
            Self::kill_child(&mut child);
        }
        inner.port = None;
        // Keep base_url for test clients; clear only real ports.
        if inner.base_url.is_none() {
            // nothing
        }
        inner.lora_scales.fill(0.0);
        Ok(())
    }

    fn generate(&mut self, req: &Request) -> Result<Response> {
        {
            let inner = self.inner.lock().unwrap();
            let loaded = inner.child.is_some() || inner.base_url.is_some();
            if !loaded {
                drop(inner);
                self.load()?;
            }
        }

        {
            let mut inner = self.inner.lock().unwrap();
            Self::touch_activity(&mut inner, &self.idle_cv);
        }

        let base = {
            let inner = self.inner.lock().unwrap();
            Self::base_url_locked(&inner)?
        };

        self.apply_lora(&base, &req.lora)?;
        let (payload, latency_ms) = self.post_chat(&base, req)?;
        let response = Self::parse_response(&payload, latency_ms)?;

        {
            let mut inner = self.inner.lock().unwrap();
            Self::touch_activity(&mut inner, &self.idle_cv);
        }
        Ok(response)
    }

    fn rss_bytes(&self) -> u64 {
        let inner = self.inner.lock().unwrap();
        match &inner.child {
            Some(child) => rss_bytes_for_pid(child.id()),
            None => 0,
        }
    }

    fn pid(&self) -> Option<u32> {
        let inner = self.inner.lock().unwrap();
        inner.child.as_ref().map(|c| c.id())
    }
}

impl Drop for LlamaServer {
    fn drop(&mut self) {
        let _ = self.unload();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::sync::Mutex as StdMutex;

    enum FakeStep {
        Expect {
            method: &'static str,
            path: &'static str,
            check_body: Box<dyn Fn(&Value) + Send>,
            response: Value,
            status: u16,
        },
    }

    fn parse_http_request(raw: &str) -> (String, String, Value) {
        let mut lines = raw.split("\r\n");
        let request_line = lines.next().unwrap_or("");
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or("").to_string();
        let path = parts.next().unwrap_or("").to_string();
        let mut content_length = 0usize;
        for line in lines.by_ref() {
            if line.is_empty() {
                break;
            }
            let lower = line.to_ascii_lowercase();
            if let Some(v) = lower.strip_prefix("content-length:") {
                content_length = v.trim().parse().unwrap_or(0);
            }
        }
        let body_str = if let Some(idx) = raw.find("\r\n\r\n") {
            let b = &raw[idx + 4..];
            if content_length > 0 && b.len() >= content_length {
                &b[..content_length]
            } else {
                b
            }
        } else {
            ""
        };
        let body = if body_str.trim().is_empty() {
            Value::Null
        } else {
            serde_json::from_str(body_str).unwrap_or(Value::Null)
        };
        (method, path, body)
    }

    fn write_http(stream: &mut impl Write, status: u16, body: &Value) {
        let payload = serde_json::to_vec(body).unwrap();
        let reason = if status == 200 { "OK" } else { "ERR" };
        let header = format!(
            "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            payload.len()
        );
        let _ = stream.write_all(header.as_bytes());
        let _ = stream.write_all(&payload);
        let _ = stream.flush();
    }

    fn run_fake(steps: Vec<FakeStep>) -> (String, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let base = format!("http://127.0.0.1:{port}");
        let script = Arc::new(StdMutex::new(steps));
        let handle = thread::spawn(move || {
            listener.set_nonblocking(false).ok();
            for stream in listener.incoming() {
                let mut stream = match stream {
                    Ok(s) => s,
                    Err(_) => break,
                };
                // Read headers + body (ureq may keep-alive; read until Content-Length satisfied).
                let mut acc = Vec::new();
                let mut tmp = [0u8; 8192];
                loop {
                    match stream.read(&mut tmp) {
                        Ok(0) => break,
                        Ok(n) => {
                            acc.extend_from_slice(&tmp[..n]);
                            if let Some(idx) = find_header_end(&acc) {
                                let headers = String::from_utf8_lossy(&acc[..idx]);
                                let mut cl = 0usize;
                                for line in headers.lines() {
                                    let lower = line.to_ascii_lowercase();
                                    if let Some(v) = lower.strip_prefix("content-length:") {
                                        cl = v.trim().parse().unwrap_or(0);
                                    }
                                }
                                if acc.len() >= idx + cl {
                                    break;
                                }
                            }
                        }
                        Err(_) => break,
                    }
                }
                let raw = String::from_utf8_lossy(&acc);
                let (method, path, body) = parse_http_request(&raw);

                let step = {
                    let mut steps = script.lock().unwrap();
                    if steps.is_empty() {
                        write_http(&mut stream, 500, &json!({"error":"unexpected"}));
                        break;
                    }
                    steps.remove(0)
                };
                match step {
                    FakeStep::Expect {
                        method: em,
                        path: ep,
                        check_body,
                        response,
                        status,
                    } => {
                        assert_eq!(method, em);
                        assert_eq!(path, ep);
                        check_body(&body);
                        write_http(&mut stream, status, &response);
                    }
                }
                if script.lock().unwrap().is_empty() {
                    break;
                }
            }
        });
        (base, handle)
    }

    fn find_header_end(buf: &[u8]) -> Option<usize> {
        buf.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4)
    }

    fn chat_ok(text: &str) -> Value {
        json!({
            "choices": [{"message": {"content": text, "role": "assistant"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5}
        })
    }

    #[test]
    fn chat_shaping_and_schema_field() {
        let (base, handle) = run_fake(vec![FakeStep::Expect {
            method: "POST",
            path: "/v1/chat/completions",
            check_body: Box::new(|body| {
                assert_eq!(body["model"], "local");
                assert_eq!(body["temperature"], 0.0);
                assert_eq!(body["seed"], 0);
                assert_eq!(body["max_tokens"], 32);
                assert_eq!(body["stream"], false);
                assert_eq!(body["messages"][0]["role"], "user");
                assert_eq!(body["response_format"]["type"], "json_schema");
                assert_eq!(body["response_format"]["json_schema"]["name"], "donna");
                assert_eq!(body["response_format"]["json_schema"]["strict"], true);
                assert_eq!(
                    body["response_format"]["json_schema"]["schema"]["type"],
                    "object"
                );
            }),
            response: chat_ok("{\"ok\":true}"),
            status: 200,
        }]);

        let cfg = Config {
            model_path: "unused".into(),
            ..Config::default()
        };
        let mut server = LlamaServer::with_base_url(cfg, base);
        let mut req = Request::new(
            vec![ChatMessage {
                role: "user".into(),
                content: "hi".into(),
            }],
            32,
        );
        req.json_schema = Some(json!({"type": "object"}));
        let resp = server.generate(&req).unwrap();
        assert_eq!(resp.text, "{\"ok\":true}");
        assert_eq!(resp.prompt_tokens, 3);
        assert_eq!(resp.completion_tokens, 5);
        handle.join().unwrap();
    }

    #[test]
    fn lora_post_then_chat() {
        let (base, handle) = run_fake(vec![
            FakeStep::Expect {
                method: "POST",
                path: "/lora-adapters",
                check_body: Box::new(|body| {
                    assert!(body.is_array());
                    assert_eq!(body[0]["id"], 0);
                    assert_eq!(body[0]["scale"], 0.75);
                }),
                response: json!({"success": true}),
                status: 200,
            },
            FakeStep::Expect {
                method: "POST",
                path: "/v1/chat/completions",
                check_body: Box::new(|body| {
                    assert_eq!(body["messages"][0]["content"], "ping");
                }),
                response: chat_ok("pong"),
                status: 200,
            },
        ]);

        let cfg = Config {
            model_path: "unused".into(),
            loras: vec![("adapter_a".into(), "/tmp/a.gguf".into())],
            ..Config::default()
        };
        let mut server = LlamaServer::with_base_url(cfg, base);
        let mut req = Request::new(
            vec![ChatMessage {
                role: "user".into(),
                content: "ping".into(),
            }],
            16,
        );
        req.lora = Some(("adapter_a".into(), 0.75));
        let resp = server.generate(&req).unwrap();
        assert_eq!(resp.text, "pong");
        handle.join().unwrap();
    }

    #[test]
    fn non_200_maps_to_error() {
        let (base, handle) = run_fake(vec![FakeStep::Expect {
            method: "POST",
            path: "/v1/chat/completions",
            check_body: Box::new(|_| {}),
            response: json!({"error": "nope"}),
            status: 500,
        }]);
        let cfg = Config {
            model_path: "unused".into(),
            ..Config::default()
        };
        let mut server = LlamaServer::with_base_url(cfg, base);
        let req = Request::new(
            vec![ChatMessage {
                role: "user".into(),
                content: "x".into(),
            }],
            8,
        );
        let err = server.generate(&req).unwrap_err();
        match err {
            InferenceError::Http(msg) => assert!(msg.contains("500"), "{msg}"),
            other => panic!("expected Http, got {other:?}"),
        }
        handle.join().unwrap();
    }

    #[test]
    fn logprobs_round_trip() {
        let (base, handle) = run_fake(vec![FakeStep::Expect {
            method: "POST",
            path: "/v1/chat/completions",
            check_body: Box::new(|body| {
                assert_eq!(body["logprobs"], true);
                assert_eq!(body["top_logprobs"], 5);
                assert_eq!(body["n_probs"], 5);
            }),
            response: json!({
                "choices": [{
                    "message": {"content": "{\"label\":\"email\"}", "role": "assistant"},
                    "logprobs": {
                        "content": [
                            {
                                "token": "{\"",
                                "logprob": -0.1,
                                "top_logprobs": [{"token": "{\"", "logprob": -0.1}]
                            },
                            {
                                "token": "label",
                                "logprob": -0.05,
                                "top_logprobs": [{"token": "label", "logprob": -0.05}]
                            },
                            {
                                "token": "\":\"",
                                "logprob": -0.02,
                                "top_logprobs": [{"token": "\":\"", "logprob": -0.02}]
                            },
                            {
                                "token": "email",
                                "logprob": -0.2,
                                "top_logprobs": [
                                    {"token": "email", "logprob": -0.2},
                                    {"token": "calendar", "logprob": -1.5},
                                    {"token": "sheet", "logprob": -2.0},
                                    {"token": "general", "logprob": -2.5},
                                    {"token": "escalate", "logprob": -3.0}
                                ]
                            }
                        ]
                    }
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4}
            }),
            status: 200,
        }]);
        let mut server = LlamaServer::with_base_url(
            Config {
                model_path: "unused".into(),
                ..Config::default()
            },
            base,
        );
        let mut req = Request::new(
            vec![ChatMessage {
                role: "user".into(),
                content: "route".into(),
            }],
            32,
        );
        req.logprobs = Some(5);
        let resp = server.generate(&req).unwrap();
        assert_eq!(resp.text, "{\"label\":\"email\"}");
        assert_eq!(resp.token_logprobs.len(), 4);
        assert_eq!(resp.token_logprobs[3].token, "email");
        assert_eq!(resp.token_logprobs[3].top.len(), 5);
        assert!((resp.token_logprobs[3].logprob - (-0.2)).abs() < 1e-5);
        handle.join().unwrap();
    }

    #[test]
    fn tool_calls_parsed() {
        let (base, handle) = run_fake(vec![FakeStep::Expect {
            method: "POST",
            path: "/v1/chat/completions",
            check_body: Box::new(|body| {
                assert!(body.get("tools").is_some());
                assert_eq!(body["tool_choice"], "auto");
            }),
            response: json!({
                "choices": [{
                    "message": {
                        "content": null,
                        "tool_calls": [{
                            "function": {
                                "name": "search_email",
                                "arguments": "{\"q\":\"hi\"}"
                            }
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}
            }),
            status: 200,
        }]);
        let mut server = LlamaServer::with_base_url(
            Config {
                model_path: "unused".into(),
                ..Config::default()
            },
            base,
        );
        let mut req = Request::new(
            vec![ChatMessage {
                role: "user".into(),
                content: "find".into(),
            }],
            16,
        );
        req.tools = Some(json!([{"type":"function","function":{"name":"search_email"}}]));
        let resp = server.generate(&req).unwrap();
        assert_eq!(resp.tool_calls.len(), 1);
        assert_eq!(resp.tool_calls[0].name, "search_email");
        assert_eq!(resp.tool_calls[0].args["q"], "hi");
        handle.join().unwrap();
    }
}

#[cfg(test)]
mod integration {
    use super::*;
    use std::path::Path;

    fn llama_server_available() -> bool {
        Command::new("which")
            .arg("llama-server")
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    #[test]
    #[ignore]
    fn load_generate_idle_unload() {
        assert!(
            llama_server_available(),
            "llama-server not on PATH; install or set DONNA_LLAMA_SERVER"
        );
        // CARGO_MANIFEST_DIR is crates/donna-inference; models live at workspace root.
        let model = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../bench/models/gguf/Qwen3.5-2B-Q4_K_M.gguf");
        let model = model.canonicalize().unwrap_or(model);
        assert!(model.exists(), "missing model at {}", model.display());

        let cfg = Config {
            model_path: model,
            idle_unload: Duration::from_secs(2),
            ..Config::default()
        };
        let mut server = LlamaServer::new(cfg);
        server.load().expect("load");
        let rss = server.rss_bytes();
        assert!(rss > 0, "expected rss > 0 after load, got {rss}");
        assert!(server.pid().is_some());

        let schema = json!({
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"}
            },
            "required": ["ok"],
            "additionalProperties": false
        });
        let mut req = Request::new(
            vec![ChatMessage {
                role: "user".into(),
                content: "Reply with JSON only: {\"ok\": true}".into(),
            }],
            64,
        );
        req.json_schema = Some(schema);
        let resp = server.generate(&req).expect("generate");
        let parsed: Value = serde_json::from_str(&resp.text)
            .unwrap_or_else(|e| panic!("invalid JSON {:?}: {e}", resp.text));
        assert!(parsed.get("ok").is_some(), "schema round-trip: {parsed}");

        // Wait past idle unload.
        thread::sleep(Duration::from_secs(4));
        // Poll until unloaded (idle thread may need a moment).
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if server.pid().is_none() && server.rss_bytes() == 0 {
                break;
            }
            thread::sleep(Duration::from_millis(200));
        }
        assert!(server.pid().is_none(), "expected idle unload to clear pid");
        assert_eq!(server.rss_bytes(), 0);

        server.unload().unwrap();
        drop(server);
    }
}
