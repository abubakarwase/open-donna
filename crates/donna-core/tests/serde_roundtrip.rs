use donna_core::{Context, Input, Trace};
use serde::Deserialize;
use serde_json::Value;
use std::path::PathBuf;

fn examples_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../evals/spec/examples")
}

const TRACE_REQUIRED: &[&str] = &[
    "case_id",
    "route",
    "tool_calls",
    "drafts",
    "escalation",
    "executed_side_effects",
    "steps",
    "latency_ms",
    "peak_rss_bytes",
];

#[test]
fn traces_jsonl_roundtrip_keeps_required_keys() {
    let path = examples_dir().join("traces.jsonl");
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!("read {}: {e}", path.display());
    });

    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let trace: Trace = serde_json::from_str(line)
            .unwrap_or_else(|e| panic!("traces.jsonl line {}: deserialize: {e}", i + 1));
        let again = serde_json::to_value(&trace)
            .unwrap_or_else(|e| panic!("traces.jsonl line {}: serialize: {e}", i + 1));
        let obj = again
            .as_object()
            .unwrap_or_else(|| panic!("traces.jsonl line {}: not an object", i + 1));
        for key in TRACE_REQUIRED {
            assert!(
                obj.contains_key(*key),
                "traces.jsonl line {}: missing required key {key}",
                i + 1
            );
        }
        let round: Trace = serde_json::from_value(again)
            .unwrap_or_else(|e| panic!("traces.jsonl line {}: re-deserialize: {e}", i + 1));
        assert_eq!(round.case_id, trace.case_id);
        assert_eq!(round.steps, trace.steps);
    }
}

#[derive(Deserialize)]
struct CaseSlice {
    input: Input,
    context: Context,
}

#[test]
fn cases_jsonl_input_context_deserialize() {
    let path = examples_dir().join("cases.jsonl");
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!("read {}: {e}", path.display());
    });

    let mut n = 0;
    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let raw: Value = serde_json::from_str(line)
            .unwrap_or_else(|e| panic!("cases.jsonl line {}: json: {e}", i + 1));
        let slice: CaseSlice = serde_json::from_value(raw)
            .unwrap_or_else(|e| panic!("cases.jsonl line {}: input/context: {e}", i + 1));
        assert!(
            !slice.input.text.is_empty(),
            "cases.jsonl line {}: empty input.text",
            i + 1
        );
        assert!(
            !slice.context.timezone.is_empty(),
            "cases.jsonl line {}: empty context.timezone",
            i + 1
        );
        n += 1;
    }
    assert!(n > 0, "expected at least one case");
}
