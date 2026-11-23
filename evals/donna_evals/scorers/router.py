"""Router / escalation scorers."""
from __future__ import annotations

from typing import Any


def score_case(case: dict[str, Any], trace: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    del ctx
    expected = case["expected"]
    out: dict[str, Any] = {}

    got_label = (trace.get("route") or {}).get("label")
    out["route_accuracy"] = 1.0 if got_label == expected.get("route") else 0.0

    esc = expected.get("escalation")
    escalated = bool((trace.get("escalation") or {}).get("escalated"))
    if esc == "escalate":
        out["escalation_recall"] = 1.0 if escalated else 0.0
    elif esc == "local":
        out["over_escalation_rate"] = 1.0 if escalated else 0.0
    return out
