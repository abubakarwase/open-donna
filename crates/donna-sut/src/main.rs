//! SUT process: JSONL cases on stdin → one trace JSON line per case on stdout.
//! Logs go to stderr only. Long-lived; EOF ends the loop.
//!
//! With `--model <gguf>` or `DONNA_MODEL`, uses [`SkillAgent`] (router + skills); otherwise [`StubAgent`].

use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use anyhow::{bail, Context as _, Result};
use donna_core::{Agent, ConsentPolicy, Context, DenyConsent, Input, NoopSink, StubAgent};
use donna_inference::{emergency_kill_child, Config, Inference, LlamaServer, MockProvider};
use donna_skills::{LlmRouter, SkillAgent};
use serde::Deserialize;

static STOP: AtomicBool = AtomicBool::new(false);
/// Set before intentional Drop so the parent-death watch does not `_exit` mid-unload.
static NORMAL_EXIT: AtomicBool = AtomicBool::new(false);

#[derive(Debug, Deserialize)]
struct SutCase {
    id: String,
    input: Input,
    context: Context,
}

fn self_rss_bytes() -> u64 {
    // SAFETY: getrusage with RUSAGE_SELF is well-defined and only writes into `usage`.
    unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) != 0 {
            return 0;
        }
        let rss = usage.ru_maxrss;
        #[cfg(target_os = "macos")]
        {
            // macOS: ru_maxrss is already in bytes.
            rss as u64
        }
        #[cfg(target_os = "linux")]
        {
            // Linux: ru_maxrss is in kibibytes.
            (rss as u64).saturating_mul(1024)
        }
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        {
            let _ = rss;
            0
        }
    }
}

/// Peak RSS for a case: max(SUT process, llama-server child). Stub path passes child=0.
fn peak_rss_bytes(child_rss: u64) -> u64 {
    self_rss_bytes().max(child_rss)
}

fn parse_model_arg() -> Option<PathBuf> {
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--model" {
            return args.next().map(PathBuf::from);
        }
        if let Some(path) = a.strip_prefix("--model=") {
            return Some(PathBuf::from(path));
        }
    }
    std::env::var_os("DONNA_MODEL").map(PathBuf::from)
}

fn parse_lora_arg() -> Option<String> {
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--lora" {
            return args.next();
        }
        if let Some(name) = a.strip_prefix("--lora=") {
            return Some(name.to_string());
        }
    }
    std::env::var("DONNA_LORA").ok().filter(|s| !s.is_empty())
}

/// Resolve `--model` like model_only.resolve_model, plus short names (`Qwen3.5-2B` → Q4_K_M gguf).
fn resolve_model_path(raw: PathBuf) -> Result<PathBuf> {
    if raw.is_file() {
        return Ok(raw.canonicalize().unwrap_or(raw));
    }
    let name = raw
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or_else(|| raw.to_str().unwrap_or(""))
        .to_string();
    if name.is_empty() {
        bail!("empty --model path");
    }

    let mut dirs = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        let mut cur = cwd;
        for _ in 0..8 {
            dirs.push(cur.join("bench/models/gguf"));
            if !cur.pop() {
                break;
            }
        }
    }
    // Binary often lives under target/release/; walk up from exe too.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(mut cur) = exe.parent().map(Path::to_path_buf) {
            for _ in 0..8 {
                dirs.push(cur.join("bench/models/gguf"));
                if !cur.pop() {
                    break;
                }
            }
        }
    }

    let candidates = [
        name.clone(),
        format!("{name}.gguf"),
        format!("{name}-Q4_K_M.gguf"),
    ];
    for dir in &dirs {
        for c in &candidates {
            let p = dir.join(c);
            if p.is_file() {
                return Ok(p.canonicalize().unwrap_or(p));
            }
        }
    }
    bail!(
        "GGUF not found for --model {:?} (looked under bench/models/gguf for {:?})",
        raw,
        candidates
    );
}

/// Install SIGINT/SIGTERM/SIGHUP handlers that kill llama-server then exit (Drop may not run).
fn install_signal_handlers() {
    extern "C" fn on_signal(_: libc::c_int) {
        STOP.store(true, Ordering::SeqCst);
        emergency_kill_child();
        // Immediate exit: avoid re-entering Rust Drop from the handler.
        // SAFETY: _exit is async-signal-safe.
        unsafe { libc::_exit(128 + libc::SIGTERM) };
    }
    unsafe {
        libc::signal(libc::SIGINT, on_signal as *const () as usize);
        libc::signal(libc::SIGTERM, on_signal as *const () as usize);
        libc::signal(libc::SIGHUP, on_signal as *const () as usize);
    }
}

/// When the eval runner uses `shell=True`, killing the shell can orphan us (PPID→1)
/// without a clean Drop. Watch for parent death and kill llama-server.
fn install_parent_death_watch() {
    let ppid = unsafe { libc::getppid() };
    std::thread::Builder::new()
        .name("donna-sut-parent-watch".into())
        .spawn(move || loop {
            std::thread::sleep(Duration::from_millis(400));
            if NORMAL_EXIT.load(Ordering::SeqCst) {
                return;
            }
            let cur = unsafe { libc::getppid() };
            if cur != ppid {
                emergency_kill_child();
                // Brief grace so pkill can land before we disappear.
                std::thread::sleep(Duration::from_millis(200));
                emergency_kill_child();
                unsafe { libc::_exit(1) };
            }
        })
        .ok();
}

fn run_loop(agent: &dyn Agent, mut child_rss: impl FnMut() -> u64) -> Result<()> {
    let stdin = io::stdin();
    let mut stdout = io::stdout();
    let mut sink = NoopSink;

    for line in stdin.lock().lines() {
        if STOP.load(Ordering::SeqCst) {
            break;
        }
        let line = line.context("read stdin")?;
        if line.trim().is_empty() {
            continue;
        }

        let case: SutCase = match serde_json::from_str(&line) {
            Ok(c) => c,
            Err(e) => {
                eprintln!("donna-sut: bad case json: {e}");
                continue;
            }
        };

        let started = Instant::now();
        let mut peak = peak_rss_bytes(child_rss());
        let mut trace = agent.run(&case.input, &case.context, &mut sink);
        peak = peak.max(peak_rss_bytes(child_rss()));
        let latency_ms = started.elapsed().as_secs_f64() * 1000.0;
        trace = trace
            .with_case_id(case.id)
            .with_latency_ms(latency_ms)
            .with_peak_rss_bytes(peak);

        let out = serde_json::to_string(&trace).context("serialize trace")?;
        writeln!(stdout, "{out}").context("write stdout")?;
        stdout.flush().context("flush stdout")?;
    }

    Ok(())
}

fn main() -> Result<()> {
    install_signal_handlers();
    install_parent_death_watch();
    if let Some(raw_model) = parse_model_arg() {
        let model_path = resolve_model_path(raw_model)?;
        let lora = parse_lora_arg();
        // M1 eval: one 2B llama-server serves both router and skills for the whole
        // run ("router always loaded" = keep this process's server up until exit).
        // Idle unload still works if timeout elapses; generate() reloads on demand.
        let cfg = Config {
            model_path,
            idle_unload: Duration::from_secs(3600),
            ..Config::default()
        };
        let mut server = LlamaServer::new(cfg);
        // Eager load warms the eval SUT; load-on-demand still works if unloaded later.
        server.load().context("load llama-server model")?;
        let mut router = LlmRouter::new(Box::new(server));
        if let Some(name) = lora {
            router = router.with_lora(name);
        }
        let inference = router.shared_inference();
        // Evals: AskEachTime + DenyConsent + MockProvider (never real frontier HTTP).
        let agent = SkillAgent::from_router(router)
            .with_consent_policy(ConsentPolicy::AskEachTime)
            .with_consent(Box::new(DenyConsent))
            .with_frontier(Some(Box::new(MockProvider::new("sut-mock-frontier"))));
        let result = run_loop(&agent, || inference.borrow().rss_bytes());
        // Drop agent → LlmRouter/SkillAgent → LlamaServer::Drop → unload.
        NORMAL_EXIT.store(true, Ordering::SeqCst);
        drop(agent);
        drop(inference);
        emergency_kill_child();
        // Second pass after Drop in case a grandchild survived.
        std::thread::sleep(Duration::from_millis(150));
        emergency_kill_child();
        result
    } else {
        run_loop(&StubAgent, || 0)
    }
}
