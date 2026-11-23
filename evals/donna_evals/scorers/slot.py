"""Free-slot sidecar scorer."""
from __future__ import annotations

import json
from typing import Any

from donna_evals.scorers.tools import values_equal


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
        if isinstance(payload, dict) and "start" in payload and "end" in payload:
            return payload
    return None


def score_case(case: dict[str, Any], trace: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    truth_map = ctx.get("slot_truth") or {}
    case_id = case["id"]
    if case_id not in truth_map:
        return {}

    truth = truth_map[case_id]
    payload = _parse_answer(trace) or _from_drafts(trace)
    if payload is None:
        # Args-only path: do not count toward slot metric.
        return {"slot_match": "n/a"}

    if truth is None:
        ok = payload.get("start") is None and payload.get("end") is None
        return {"slot_match": 1.0 if ok else 0.0}

    ok = values_equal(truth.get("start"), payload.get("start")) and values_equal(
        truth.get("end"), payload.get("end")
    )
    return {"slot_match": 1.0 if ok else 0.0}
