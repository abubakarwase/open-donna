"""LLM-judge rubric scorer (skipped without --judge-url)."""
from __future__ import annotations

from typing import Any

from donna_evals import judge as judge_mod


def _output_text(trace: dict[str, Any]) -> str:
    if isinstance(trace.get("answer"), str) and trace["answer"].strip():
        return trace["answer"]
    for draft in trace.get("drafts") or []:
        payload = draft.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("body", "text", "summary"):
                if isinstance(payload.get(key), str) and payload[key].strip():
                    return payload[key]
            return json_dumps_safe(payload)
    return ""


def json_dumps_safe(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def score_case(case: dict[str, Any], trace: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    rubric_id = case["expected"].get("rubric")
    if not rubric_id:
        return {}

    judge_url = ctx.get("judge_url")
    judge_model = ctx.get("judge_model")
    if not judge_url or not judge_model:
        return {"rubric_mean_score": "skipped", "invented_fact_rate": "skipped"}

    source = (case.get("input") or {}).get("text") or ""
    output = _output_text(trace)
    try:
        result = judge_mod.score(
            rubric_id,
            source=source,
            output=output,
            judge_url=judge_url,
            judge_model=judge_model,
        )
    except Exception:
        return {"rubric_mean_score": 0.0, "invented_fact_rate": 1.0}

    scores: list[float] = []
    criteria = result.get("criteria") or result.get("scores") or []
    if isinstance(criteria, list):
        for item in criteria:
            if isinstance(item, dict):
                s = item.get("score")
                if isinstance(s, (int, float)):
                    scores.append(float(s))
            elif isinstance(item, (int, float)):
                scores.append(float(item))
    elif isinstance(criteria, dict):
        for s in criteria.values():
            if isinstance(s, (int, float)):
                scores.append(float(s))
    mean = sum(scores) / len(scores) if scores else 0.0

    invented = result.get("invented_fact")
    if isinstance(invented, dict):
        inv_flag = bool(invented.get("present") or invented.get("invented"))
    else:
        inv_flag = bool(invented)

    return {
        "rubric_mean_score": mean,
        "invented_fact_rate": 1.0 if inv_flag else 0.0,
    }
