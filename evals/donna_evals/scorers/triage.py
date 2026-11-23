"""Email triage sidecar scorer."""
from __future__ import annotations

import json
from typing import Any


def _parse_answer(trace: dict[str, Any]) -> dict[str, Any] | None:
    ans = trace.get("answer")
    if not isinstance(ans, str) or not ans.strip():
        return None
    try:
        obj = json.loads(ans)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _from_drafts(trace: dict[str, Any]) -> dict[str, Any] | None:
    for draft in trace.get("drafts") or []:
        payload = draft.get("payload")
        if isinstance(payload, dict) and payload:
            # Heuristic: triage map is string->string labels.
            if all(isinstance(k, str) and isinstance(v, str) for k, v in payload.items()):
                return payload
    return None


def score_case(case: dict[str, Any], trace: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    truth_map = ctx.get("triage_truth") or {}
    case_id = case["id"]
    if case_id not in truth_map:
        return {}

    truth = truth_map[case_id]
    payload = _parse_answer(trace) or _from_drafts(trace)
    if payload is None:
        return {"triage_match": "n/a"}

    ok = payload == truth
    return {"triage_match": 1.0 if ok else 0.0}
