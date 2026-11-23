"""Per-kind scorers for donna-evals."""
from __future__ import annotations

from typing import Any, Callable

from donna_evals.scorers import perf, router, rubric, safety, slot, sql_exec, tools, triage

ScoreFn = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]

SCORERS: list[tuple[str, ScoreFn]] = [
    ("router", router.score_case),
    ("tools", tools.score_case),
    ("sql_exec", sql_exec.score_case),
    ("slot", slot.score_case),
    ("triage", triage.score_case),
    ("rubric", rubric.score_case),
    ("safety", safety.score_case),
    ("perf", perf.score_case),
]


def aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate per-case metric contributions into {metric: {value, n}}."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for row in rows:
        for key, val in row.items():
            if key.startswith("_"):
                continue
            if val == "skipped":
                skipped[key] = skipped.get(key, 0) + 1
                continue
            if val == "n/a":
                continue
            if not isinstance(val, (int, float)):
                continue
            sums[key] = sums.get(key, 0.0) + float(val)
            counts[key] = counts.get(key, 0) + 1

    out: dict[str, dict[str, Any]] = {}
    for key, n in counts.items():
        # Rates that are already 0/1 averages; max metrics use max via special keys
        if key.endswith("_max") or key == "peak_rss_bytes_observed":
            vals = [
                float(r[key])
                for r in rows
                if isinstance(r.get(key), (int, float))
            ]
            out[key] = {"value": max(vals) if vals else 0.0, "n": len(vals)}
        elif key.endswith("_p50") or key.endswith("_p95") or key in (
            "latency_ms",
            "latency_ms_wall",
        ):
            # Recomputed in perf.aggregate_perf; placeholder.
            out[key] = {"value": sums[key] / n if n else 0.0, "n": n}
        else:
            out[key] = {"value": (sums[key] / n) if n else 0.0, "n": n}
    for key, n in skipped.items():
        if key not in out:
            out[key] = {"value": None, "n": 0, "status": "skipped (no judge)"}
    out.update(tools.aggregate_schema(rows))
    return out
