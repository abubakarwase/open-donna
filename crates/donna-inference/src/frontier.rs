//! OpenAI-compatible frontier HTTP client and a network-free mock.

use donna_core::{FrontierError, FrontierProvider};
use serde_json::{json, Value};
use std::sync::Mutex;
use std::time::Duration;

/// OpenAI-compatible `/v1/chat/completions` frontier client.
pub struct OpenAiCompatibleProvider {
    pub base_url: String,
    pub model: String,
    pub api_key: Option<String>,
}

impl OpenAiCompatibleProvider {
    /// Build from `DONNA_FRONTIER_BASE_URL`, `DONNA_FRONTIER_MODEL`, `DONNA_FRONTIER_API_KEY`.
    pub fn from_env() -> Result<Self, FrontierError> {
        let base_url = std::env::var("DONNA_FRONTIER_BASE_URL")
            .map_err(|_| FrontierError::Other("DONNA_FRONTIER_BASE_URL not set".into()))?;
        let model = std::env::var("DONNA_FRONTIER_MODEL").unwrap_or_else(|_| "gpt-4o-mini".into());
        let api_key = std::env::var("DONNA_FRONTIER_API_KEY").ok();
        Ok(Self {
            base_url,
            model,
            api_key,
        })
    }
}

impl FrontierProvider for OpenAiCompatibleProvider {
    fn complete(&self, payload: &str) -> Result<String, FrontierError> {
        let url = format!(
            "{}/v1/chat/completions",
            self.base_url.trim_end_matches('/')
        );
        let body = json!({
            "model": self.model,
            "messages": [{"role": "user", "content": payload}],
            "temperature": 0.0,
            "stream": false,
        });
        let mut req = ureq::post(&url)
            .set("Content-Type", "application/json")
            .timeout(Duration::from_secs(120));
        if let Some(key) = &self.api_key {
            req = req.set("Authorization", &format!("Bearer {key}"));
        }
        let resp = req.send_json(body).map_err(|e| match e {
            ureq::Error::Status(code, resp) => {
                let detail = resp.into_string().unwrap_or_default();
                FrontierError::Http(format!("HTTP {code}: {}", &detail[..detail.len().min(500)]))
            }
            other => FrontierError::Http(other.to_string()),
        })?;
        let payload: Value = resp
            .into_json()
            .map_err(|e| FrontierError::Json(e.to_string()))?;
        let text = payload
            .pointer("/choices/0/message/content")
            .and_then(|c| c.as_str())
            .unwrap_or("")
            .to_string();
        Ok(text)
    }
}

/// Records calls; never opens a network connection.
#[derive(Debug, Default)]
pub struct MockProvider {
    response: String,
    calls: Mutex<Vec<String>>,
}

impl MockProvider {
    pub fn new(response: impl Into<String>) -> Self {
        Self {
            response: response.into(),
            calls: Mutex::new(Vec::new()),
        }
    }

    pub fn call_count(&self) -> usize {
        self.calls.lock().unwrap().len()
    }

    pub fn last_payload(&self) -> Option<String> {
        self.calls.lock().unwrap().last().cloned()
    }

    pub fn payloads(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}

impl FrontierProvider for MockProvider {
    fn complete(&self, payload: &str) -> Result<String, FrontierError> {
        self.calls.lock().unwrap().push(payload.to_string());
        Ok(self.response.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mock_records_without_network() {
        let mock = MockProvider::new("frontier-ok");
        assert_eq!(mock.call_count(), 0);
        let out = mock.complete("hello payload").unwrap();
        assert_eq!(out, "frontier-ok");
        assert_eq!(mock.call_count(), 1);
        assert_eq!(mock.last_payload().as_deref(), Some("hello payload"));
    }
}
