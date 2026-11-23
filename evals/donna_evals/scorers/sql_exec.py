"""SQL execution match scorer for sheet cases."""
from __future__ import annotations

import re
from typing import Any

from donna_evals.sheets import load_workbook_to_duckdb, normalize_sql_result, run_sql

_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
_REL_TOL = 1e-6


def _sheet_ref(case: dict[str, Any]) -> str | None:
    for ref in case.get("context", {}).get("fixtures") or []:
        if isinstance(ref, str) and ref.startswith("sheets/"):
            return ref
    return None


def _floats_close(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        af, bf = float(a), float(b)
        if af == bf:
            return True
        denom = max(abs(af), abs(bf), 1e-12)
        return abs(af - bf) / denom <= _REL_TOL
    return a == b


def _row_values(row: dict[str, Any]) -> tuple[Any, ...]:
    """Column values in row order; aliases/names ignored for equality."""
    return tuple(row.values())


def _values_equal(va: tuple[Any, ...], vb: tuple[Any, ...]) -> bool:
    if len(va) != len(vb):
        return False
    return all(_floats_close(x, y) for x, y in zip(va, vb, strict=True))


def _rows_equal(a: list[dict[str, Any]], b: list[dict[str, Any]], ordered: bool) -> bool:
    """Compare result sets by cell values in column order (ignore aliases)."""
    na = normalize_sql_result(a)
    nb = normalize_sql_result(b)
    if len(na) != len(nb):
        return False
    if na and nb and len(_row_values(na[0])) != len(_row_values(nb[0])):
        return False

    va = [_row_values(r) for r in na]
    vb = [_row_values(r) for r in nb]

    if ordered:
        return all(_values_equal(x, y) for x, y in zip(va, vb, strict=True))
    # Bag compare
    used = [False] * len(vb)
    for ra in va:
        found = False
        for j, rb in enumerate(vb):
            if used[j]:
                continue
            if _values_equal(ra, rb):
                used[j] = True
                found = True
                break
        if not found:
            return False
    return True


def score_case(case: dict[str, Any], trace: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    del ctx
    expected = case["expected"]
    if expected.get("match") != "sql_exec" and expected.get("sql_result") is None:
        return {}

    expected_result = expected.get("sql_result")
    if expected_result is None:
        return {"sql_execution_match": 0.0}

    sql = None
    for call in trace.get("tool_calls") or []:
        if call.get("name") == "query_sheet":
            args = call.get("args") or {}
            sql = args.get("sql")
            break
    if not sql:
        return {"sql_execution_match": 0.0}

    ref = _sheet_ref(case)
    if not ref:
        return {"sql_execution_match": 0.0}

    ordered = False
    for call in expected.get("tool_calls") or []:
        if call.get("name") == "query_sheet":
            exp_sql = (call.get("args") or {}).get("sql") or ""
            ordered = bool(_ORDER_BY.search(exp_sql))
            break

    try:
        con = load_workbook_to_duckdb(ref)
        try:
            got = run_sql(con, sql)
        finally:
            con.close()
    except Exception:
        return {"sql_execution_match": 0.0}

    ok = _rows_equal(list(expected_result), got, ordered=ordered)
    return {"sql_execution_match": 1.0 if ok else 0.0}
