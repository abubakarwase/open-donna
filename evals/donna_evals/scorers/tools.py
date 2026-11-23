"""Tool-call name / argument / schema scorers."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from donna_evals.validate import _validate_tool_args


def _as_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def values_equal(expected: Any, got: Any) -> bool:
    if isinstance(expected, list) and isinstance(got, list):
        if len(expected) != len(got):
            return False
        # Order-insensitive multiset for list arg values (e.g. attendees).
        exp_norm = sorted((_norm_atom(x) for x in expected), key=lambda x: str(x))
        got_norm = sorted((_norm_atom(x) for x in got), key=lambda x: str(x))
        return exp_norm == got_norm
    ea = _as_instant(expected)
    ga = _as_instant(got)
    if ea is not None and ga is not None:
        return ea == ga
    return expected == got


def _norm_atom(v: Any) -> Any:
    inst = _as_instant(v)
    if inst is not None:
        return inst.isoformat()
    return v


def args_exact_match(expected: dict[str, Any], got: dict[str, Any]) -> bool:
    if set(expected.keys()) != set(got.keys()):
        return False
    for key, ev in expected.items():
        if not values_equal(ev, got.get(key)):
            return False
    return True


def arg_key_f1(expected: dict[str, Any], got: dict[str, Any]) -> float:
    ek = set(expected.keys())
    gk = set(got.keys())
    if not ek and not gk:
        return 1.0
    inter = ek & gk
    precision = len(inter) / len(gk) if gk else 0.0
    recall = len(inter) / len(ek) if ek else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _schema_hits(got_calls: list[dict[str, Any]], schemas: dict[str, Any]) -> tuple[int, int]:
    """Return (valid_count, emitted_count). Unknown tool name counts as invalid."""
    if not got_calls:
        return 0, 0
    valid = 0
    for call in got_calls:
        name = call.get("name")
        args = call.get("args")
        if name and _validate_tool_args(schemas, name, args) is None:
            valid += 1
    return valid, len(got_calls)


def score_case(case: dict[str, Any], trace: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    got_calls = list(trace.get("tool_calls") or [])
    schemas = ctx.get("tool_schemas") or {}
    hits, total = _schema_hits(got_calls, schemas)
    out: dict[str, Any] = {
        "_schema_valid_hits": hits,
        "_schema_valid_total": total,
    }

    expected_calls = case["expected"].get("tool_calls")
    if not expected_calls:
        return out

    exp_names = [c["name"] for c in expected_calls]
    got_names = [c.get("name") for c in got_calls]
    out["tool_name_accuracy"] = 1.0 if exp_names == got_names else 0.0

    n = len(expected_calls)
    exact_hits = 0
    f1_sum = 0.0
    for i, exp in enumerate(expected_calls):
        got = got_calls[i] if i < len(got_calls) else None
        if got is None or not isinstance(got.get("args"), dict):
            f1_sum += 0.0
            continue
        if args_exact_match(exp["args"], got["args"]):
            exact_hits += 1
        f1_sum += arg_key_f1(exp["args"], got["args"])
    out["argument_exact_match"] = exact_hits / n if n else 1.0
    out["argument_f1"] = f1_sum / n if n else 1.0
    return out


def aggregate_schema(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    hits = sum(int(r.get("_schema_valid_hits") or 0) for r in rows)
    total = sum(int(r.get("_schema_valid_total") or 0) for r in rows)
    if total == 0:
        return {"schema_valid_rate": {"value": None, "n": 0}}
    return {"schema_valid_rate": {"value": hits / total, "n": total}}
