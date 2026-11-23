#!/usr/bin/env python3
"""Oracle SUT: emit perfect traces from golden expected + sidecars (test-only)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EVALS_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = EVALS_ROOT / "golden"
SUITES = ("router", "email", "calendar", "sheet", "safety")

_DRAFT_KIND = {
    "draft_email": "email",
    "create_event": "event",
    "update_event": "event_update",
    "cancel_event": "event_cancel",
}


def _load_all_cases() -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for suite in SUITES:
        for split in ("dev", "test"):
            path = GOLDEN / suite / f"{split}.jsonl"
            if not path.is_file():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                case = json.loads(line)
                by_id[case["id"]] = case
    return by_id


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _drafts_from_tools(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for call in tool_calls:
        kind = _DRAFT_KIND.get(call["name"])
        if kind:
            drafts.append({"kind": kind, "payload": dict(call.get("args") or {})})
    return drafts


def perfect_trace(
    case: dict[str, Any],
    slot_truth: dict[str, Any],
    triage_truth: dict[str, Any],
) -> dict[str, Any]:
    expected = case["expected"]
    tool_calls = list(expected.get("tool_calls") or [])
    escalated = expected.get("escalation") == "escalate"
    reason = expected.get("escalate_reason") if escalated else None
    if escalated and reason is None:
        for call in tool_calls:
            if call.get("name") == "escalate":
                reason = (call.get("args") or {}).get("reason")
                break

    answer = None
    case_id = case["id"]
    if case_id in slot_truth:
        truth = slot_truth[case_id]
        answer = json.dumps(truth if truth is not None else {"start": None, "end": None})
    elif case_id in triage_truth:
        answer = json.dumps(triage_truth[case_id])
    elif expected.get("rubric"):
        # Minimal non-empty answer so rubric path has text (judge usually skipped).
        answer = "Oracle summary for rubric scoring."

    trace: dict[str, Any] = {
        "case_id": case_id,
        "route": {"label": expected["route"], "confidence": 1.0},
        "tool_calls": tool_calls,
        "drafts": _drafts_from_tools(tool_calls),
        "escalation": {
            "escalated": escalated,
            "reason": reason,
            "payload_bytes": 0,
        },
        "executed_side_effects": [],
        "steps": max(1, len(tool_calls)),
        "latency_ms": 1,
        "peak_rss_bytes": 1,
    }
    if answer is not None:
        trace["answer"] = answer
    return trace


def main() -> int:
    cases = _load_all_cases()
    slot_truth = _load_json(GOLDEN / "calendar" / "slot_truth.json")
    triage_truth = _load_json(GOLDEN / "email" / "triage_truth.json")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        inbound = json.loads(line)
        case_id = inbound["id"]
        case = cases.get(case_id)
        if case is None:
            # Unknown id: emit a schema-valid miss.
            trace = {
                "case_id": case_id,
                "route": {"label": "general", "confidence": 0.0},
                "tool_calls": [],
                "drafts": [],
                "escalation": {"escalated": False, "reason": None, "payload_bytes": 0},
                "executed_side_effects": [],
                "steps": 0,
                "latency_ms": 0,
                "peak_rss_bytes": 0,
                "error": "unknown_case",
            }
        else:
            trace = perfect_trace(case, slot_truth, triage_truth)
        sys.stdout.write(json.dumps(trace, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
