"""Latency / RSS performance scorers."""
from __future__ import annotations

from typing import Any


def score_case(case: dict[str, Any], trace: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    del case, ctx
    out: dict[str, Any] = {}
    # Self-reported (info only; no threshold).
    if "latency_ms" in trace:
        out["latency_ms"] = float(trace["latency_ms"])
    if "peak_rss_bytes" in trace:
        out["peak_rss_bytes_max"] = int(trace["peak_rss_bytes"])
    # Runner-observed.
    if isinstance(trace.get("_latency_ms_wall"), (int, float)):
        out["latency_ms_wall"] = float(trace["_latency_ms_wall"])
    if isinstance(trace.get("_peak_rss_bytes_observed"), (int, float)):
        out["peak_rss_bytes_observed"] = int(trace["_peak_rss_bytes_observed"])
    return out


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def aggregate_perf(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    self_lat = [
        float(r["latency_ms"])
        for r in rows
        if isinstance(r.get("latency_ms"), (int, float))
    ]
    self_rss = [
        int(r["peak_rss_bytes_max"])
        for r in rows
        if isinstance(r.get("peak_rss_bytes_max"), (int, float))
    ]
    if self_lat:
        out["latency_ms_p50"] = {"value": percentile(self_lat, 50), "n": len(self_lat)}
        out["latency_ms_p95"] = {"value": percentile(self_lat, 95), "n": len(self_lat)}
    if self_rss:
        out["peak_rss_bytes_max"] = {"value": float(max(self_rss)), "n": len(self_rss)}

    wall = [
        float(r["latency_ms_wall"])
        for r in rows
        if isinstance(r.get("latency_ms_wall"), (int, float))
    ]
    obs_rss = [
        int(r["peak_rss_bytes_observed"])
        for r in rows
        if isinstance(r.get("peak_rss_bytes_observed"), (int, float))
    ]
    if wall:
        out["latency_ms_wall_p50"] = {"value": percentile(wall, 50), "n": len(wall)}
        out["latency_ms_wall_p95"] = {"value": percentile(wall, 95), "n": len(wall)}
    if obs_rss:
        out["peak_rss_bytes_observed"] = {"value": float(max(obs_rss)), "n": len(obs_rss)}
    return out
