//! Privacy-safe escalate payload builder and unified frontier handoff path.

use donna_core::{
    AgentEvent, Consent, ConsentPolicy, Escalation, EventSink, FrontierProvider, ToolCall,
};
use serde_json::{json, Value};

/// Default privacy payload size cap (16 KiB).
pub const DEFAULT_MAX_PAYLOAD_BYTES: usize = 16 * 1024;

/// Inputs for the privacy payload builder.
#[derive(Debug, Clone)]
pub struct PayloadInput<'a> {
    pub task: &'a str,
    pub reason: &'a str,
    pub profile: Option<&'a Value>,
    pub query_results: Option<&'a Value>,
    pub allow_raw_rows: bool,
    pub max_bytes: usize,
}

impl<'a> PayloadInput<'a> {
    pub fn new(task: &'a str, reason: &'a str) -> Self {
        Self {
            task,
            reason,
            profile: None,
            query_results: None,
            allow_raw_rows: false,
            max_bytes: DEFAULT_MAX_PAYLOAD_BYTES,
        }
    }
}

fn strip_sample_rows(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            for (k, v) in map {
                if k == "sample_rows" {
                    continue;
                }
                out.insert(k.clone(), strip_sample_rows(v));
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(strip_sample_rows).collect()),
        other => other.clone(),
    }
}

fn truncate_json_to(value: &Value, max_bytes: usize) -> Value {
    let full = serde_json::to_string(value).unwrap_or_else(|_| "{}".into());
    if full.len() <= max_bytes {
        return value.clone();
    }
    // Prefer keeping a truncated string form rather than leaking by falling back to full.
    let cut = full
        .chars()
        .take(max_bytes.saturating_sub(20))
        .collect::<String>();
    json!({ "_truncated": format!("{cut}…") })
}

/// Build a privacy-safe escalate payload.
///
/// Returns `(payload_string, payload_bytes, payload_description)`.
/// Task is never truncated. Profile `sample_rows` / raw dumps omitted unless `allow_raw_rows`.
pub fn build_privacy_payload(input: &PayloadInput<'_>) -> (String, u64, String) {
    let description = if input.allow_raw_rows {
        "Sheet profile and query results; raw rows included by consent."
    } else if input.profile.is_some() && input.query_results.is_some() {
        "Sheet profile and prior query results only; raw rows omitted by default."
    } else if input.profile.is_some() {
        "Sheet profile only; raw rows omitted by default."
    } else {
        "Sheet profile and prior query results only; raw rows omitted by default."
    };

    let profile = input.profile.map(|p| {
        if input.allow_raw_rows {
            p.clone()
        } else {
            strip_sample_rows(p)
        }
    });
    let mut results = input.query_results.cloned();

    // Reserve room for task + reason + framing; truncate results then profile only.
    let framing = json!({
        "task": input.task,
        "reason": input.reason,
        "payload_description": description,
    });
    let framing_len = serde_json::to_string(&framing)
        .map(|s| s.len())
        .unwrap_or(0);
    let budget = input
        .max_bytes
        .saturating_sub(framing_len)
        .saturating_sub(64);

    if let Some(ref mut r) = results {
        let r_len = serde_json::to_string(r).map(|s| s.len()).unwrap_or(0);
        let p_len = profile
            .as_ref()
            .and_then(|p| serde_json::to_string(p).ok())
            .map(|s| s.len())
            .unwrap_or(0);
        if framing_len + r_len + p_len > input.max_bytes {
            let results_budget = budget.saturating_sub(p_len.min(budget / 2));
            *r = truncate_json_to(r, results_budget.max(32));
        }
    }

    let profile = profile.map(|p| {
        let cur = json!({
            "task": input.task,
            "reason": input.reason,
            "payload_description": description,
            "profile": p,
            "query_results": results.clone(),
        });
        let cur_len = serde_json::to_string(&cur).map(|s| s.len()).unwrap_or(0);
        if cur_len > input.max_bytes {
            let p_budget = budget / 2;
            truncate_json_to(cur.get("profile").unwrap_or(&Value::Null), p_budget.max(32))
        } else {
            cur.get("profile").cloned().unwrap_or(Value::Null)
        }
    });

    let body = json!({
        "task": input.task,
        "reason": input.reason,
        "payload_description": description,
        "profile": profile,
        "query_results": results,
    });
    let mut payload = serde_json::to_string(&body).unwrap_or_else(|_| "{}".into());
    if payload.len() > input.max_bytes {
        // Last resort: keep task+reason intact, drop bulky fields.
        let slim = json!({
            "task": input.task,
            "reason": input.reason,
            "payload_description": description,
            "profile": Value::Null,
            "query_results": Value::Null,
            "_truncated": true,
        });
        payload = serde_json::to_string(&slim).unwrap_or_else(|_| "{}".into());
    }
    let bytes = payload.len() as u64;
    (payload, bytes, description.to_string())
}

/// Unified escalate path: privacy payload → consent → optional frontier call.
///
/// Always records `escalated: true` + an `escalate` tool call for recall/safety, even when
/// consent is denied or policy is [`ConsentPolicy::LocalOnly`] (nothing is sent).
pub fn run_escalation(
    policy: ConsentPolicy,
    consent: &dyn Consent,
    provider: Option<&dyn FrontierProvider>,
    input: &PayloadInput<'_>,
    events: &mut dyn EventSink,
) -> (Escalation, ToolCall, Option<String>) {
    let (payload, payload_bytes, payload_description) = build_privacy_payload(input);

    let (consent_state, call_provider) = match policy {
        ConsentPolicy::LocalOnly => ("local_only", false),
        ConsentPolicy::AskEachTime => {
            if consent.allow_escalate(input.reason, payload_bytes) {
                ("granted", true)
            } else {
                // Record that consent was needed but not granted; do not call provider.
                ("denied", false)
            }
        }
        ConsentPolicy::Auto => ("granted", true),
    };

    let frontier_answer = if call_provider {
        provider.and_then(|p| p.complete(&payload).ok())
    } else {
        None
    };

    let escalation = Escalation {
        escalated: true,
        reason: Some(input.reason.to_string()),
        payload_bytes,
        consent: Some(consent_state.to_string()),
    };

    events.emit(AgentEvent::EscalationRequested {
        reason: input.reason.to_string(),
        payload_bytes,
    });

    let tool = ToolCall {
        name: "escalate".into(),
        args: json!({
            "reason": input.reason,
            "task": input.task,
            "payload_description": payload_description,
        }),
    };
    events.emit(AgentEvent::ToolCalled(tool.clone()));

    (escalation, tool, frontier_answer)
}

#[cfg(test)]
mod tests {
    use super::*;
    use donna_core::{DenyConsent, GrantConsent, NoopSink};
    use donna_inference::MockProvider;
    use std::sync::Arc;

    #[test]
    fn default_payload_strips_sample_rows() {
        let profile = json!({
            "tables": [{
                "name": "t",
                "columns": [{"name": "a", "data_type": "INTEGER"}],
                "row_count": 2,
                "sample_rows": [{"a": 1}, {"a": 2}]
            }],
            "messy": false
        });
        let input = PayloadInput {
            task: "Sum column a",
            reason: "messy_sheet",
            profile: Some(&profile),
            query_results: None,
            allow_raw_rows: false,
            max_bytes: DEFAULT_MAX_PAYLOAD_BYTES,
        };
        let (payload, bytes, desc) = build_privacy_payload(&input);
        assert!(bytes > 0);
        assert!(!payload.contains("sample_rows"));
        assert!(!payload.contains("\"a\":1"));
        assert!(desc.to_lowercase().contains("raw rows omitted"));
        assert!(payload.contains("Sum column a"));
    }

    #[test]
    fn byte_cap_truncates_results_keeps_task() {
        let task = "KEEP_THIS_TASK_INTACT_PLEASE";
        let big = "x".repeat(8_000);
        let results = json!({ "rows": [big] });
        let profile = json!({ "tables": [{"name": "t", "row_count": 1}] });
        let input = PayloadInput {
            task,
            reason: "schema_failed",
            profile: Some(&profile),
            query_results: Some(&results),
            allow_raw_rows: false,
            max_bytes: 1024,
        };
        let (payload, bytes, _) = build_privacy_payload(&input);
        assert!(bytes as usize <= 1024, "bytes={bytes}");
        assert!(payload.contains(task));
    }

    #[test]
    fn local_only_never_calls_provider() {
        let mock = MockProvider::new("should-not-see");
        let mut sink = NoopSink;
        let input = PayloadInput::new("task", "low_confidence");
        let (esc, tool, answer) = run_escalation(
            ConsentPolicy::LocalOnly,
            &DenyConsent,
            Some(&mock),
            &input,
            &mut sink,
        );
        assert_eq!(mock.call_count(), 0);
        assert!(esc.escalated);
        assert_eq!(esc.consent.as_deref(), Some("local_only"));
        assert_eq!(tool.name, "escalate");
        assert!(answer.is_none());
    }

    #[test]
    fn ask_each_time_deny_records_without_call() {
        let mock = MockProvider::new("nope");
        let mut sink = NoopSink;
        let input = PayloadInput::new("task", "user_requested");
        let (esc, tool, answer) = run_escalation(
            ConsentPolicy::AskEachTime,
            &DenyConsent,
            Some(&mock),
            &input,
            &mut sink,
        );
        assert_eq!(mock.call_count(), 0);
        assert!(esc.escalated);
        assert_eq!(esc.consent.as_deref(), Some("denied"));
        assert_eq!(tool.name, "escalate");
        assert!(answer.is_none());
    }

    #[test]
    fn ask_each_time_grant_calls_once() {
        let mock = MockProvider::new("frontier-answer");
        let mut sink = NoopSink;
        let input = PayloadInput::new("do the thing", "multi_step");
        let (esc, _tool, answer) = run_escalation(
            ConsentPolicy::AskEachTime,
            &GrantConsent,
            Some(&mock),
            &input,
            &mut sink,
        );
        assert_eq!(mock.call_count(), 1);
        assert!(mock.last_payload().unwrap().contains("do the thing"));
        assert_eq!(esc.consent.as_deref(), Some("granted"));
        assert_eq!(answer.as_deref(), Some("frontier-answer"));
    }

    #[test]
    fn auto_calls_provider() {
        let mock = Arc::new(MockProvider::new("auto-ok"));
        let mut sink = NoopSink;
        let input = PayloadInput::new("t", "ambiguous");
        let (esc, _, answer) = run_escalation(
            ConsentPolicy::Auto,
            &DenyConsent, // ignored under Auto
            Some(mock.as_ref()),
            &input,
            &mut sink,
        );
        assert_eq!(mock.call_count(), 1);
        assert_eq!(esc.consent.as_deref(), Some("granted"));
        assert_eq!(answer.as_deref(), Some("auto-ok"));
    }
}
