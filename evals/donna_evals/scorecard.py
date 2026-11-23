"""Aggregate scorer rows into scorecard.json + report.html."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import yaml

from donna_evals.scorers import SCORERS, aggregate
from donna_evals.scorers import perf as perf_mod

SPEC_ROOT = Path(__file__).resolve().parents[1] / "spec"


def load_thresholds() -> dict[str, Any]:
    path = SPEC_ROOT / "thresholds.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("metrics") or data


def _check_threshold(value: float | None, spec: dict[str, Any]) -> bool | None:
    if value is None:
        return None
    op = spec.get("op", ">=")
    target = spec["target"]
    if op == ">=":
        return value >= target
    if op == "<=":
        return value <= target
    if op == "==":
        return value == target
    return None


def score_pair(
    case: dict[str, Any],
    trace: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "_case_id": case["id"],
        "_suite": case.get("_suite"),
        "_job": case.get("job"),
    }
    for _name, fn in SCORERS:
        try:
            part = fn(case, trace, ctx) or {}
        except Exception:
            part = {}
        row.update(part)
    return row


def build_scorecard(
    cases: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    thresholds = load_thresholds()
    rows: list[dict[str, Any]] = []
    n = min(len(cases), len(traces))
    for i in range(n):
        rows.append(score_pair(cases[i], traces[i], ctx))

    metrics = aggregate(rows)
    # Replace latency averages with true percentiles; drop raw per-case keys.
    metrics.pop("latency_ms", None)
    metrics.pop("latency_ms_wall", None)
    metrics.update(perf_mod.aggregate_perf(rows))

    scored: dict[str, Any] = {}
    for name, agg in metrics.items():
        entry = dict(agg)
        th = thresholds.get(name)
        if th and entry.get("value") is not None and entry.get("n", 0) > 0:
            entry["threshold"] = {"target": th["target"], "op": th["op"], "status": th.get("status")}
            entry["pass"] = _check_threshold(entry["value"], th)
        elif entry.get("status"):
            entry["pass"] = None
        else:
            entry["threshold"] = None
            entry["pass"] = None
        scored[name] = entry

    by_suite: dict[str, dict[str, Any]] = {}
    by_job: dict[str, dict[str, Any]] = {}
    suites = sorted({r.get("_suite") for r in rows if r.get("_suite")})
    jobs = sorted({r.get("_job") for r in rows if r.get("_job")})
    for suite in suites:
        sub = [r for r in rows if r.get("_suite") == suite]
        by_suite[str(suite)] = _subset_metrics(sub, thresholds)
    for job in jobs:
        sub = [r for r in rows if r.get("_job") == job]
        by_job[str(job)] = _subset_metrics(sub, thresholds)

    return {
        "n_cases": n,
        "metrics": scored,
        "by_suite": by_suite,
        "by_job": by_job,
    }


def _subset_metrics(
    rows: list[dict[str, Any]],
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    metrics = aggregate(rows)
    metrics.pop("latency_ms", None)
    metrics.pop("latency_ms_wall", None)
    metrics.update(perf_mod.aggregate_perf(rows))
    out: dict[str, Any] = {}
    for name, agg in metrics.items():
        entry = dict(agg)
        th = thresholds.get(name)
        if th and entry.get("value") is not None and entry.get("n", 0) > 0:
            entry["pass"] = _check_threshold(entry["value"], th)
        out[name] = entry
    return out


def write_scorecard(out_dir: Path, scorecard: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scorecard.json").write_text(
        json.dumps(scorecard, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "report.html").write_text(_html_report(scorecard), encoding="utf-8")


def _html_report(scorecard: dict[str, Any]) -> str:
    rows_html = []
    for name, entry in sorted((scorecard.get("metrics") or {}).items()):
        value = entry.get("value")
        n = entry.get("n", 0)
        th = entry.get("threshold")
        if th:
            thr_s = f"{th.get('op', '')} {th.get('target', '')}"
        elif entry.get("status"):
            thr_s = str(entry["status"])
        else:
            thr_s = ""
        passed = entry.get("pass")
        if passed is True:
            pass_s = "pass"
        elif passed is False:
            pass_s = "fail"
        else:
            pass_s = ""
        val_s = "" if value is None else f"{value:.6g}" if isinstance(value, float) else str(value)
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(val_s)}</td>"
            f"<td>{n}</td>"
            f"<td>{html.escape(thr_s)}</td>"
            f"<td>{pass_s}</td>"
            "</tr>"
        )

    sections = []
    for suite, metrics in sorted((scorecard.get("by_suite") or {}).items()):
        body = []
        for name, entry in sorted(metrics.items()):
            value = entry.get("value")
            val_s = "" if value is None else f"{value:.6g}" if isinstance(value, float) else str(value)
            body.append(
                f"<tr><td>{html.escape(name)}</td><td>{html.escape(val_s)}</td>"
                f"<td>{entry.get('n', 0)}</td></tr>"
            )
        sections.append(
            f"<h2>Suite {html.escape(suite)}</h2>"
            "<table><thead><tr><th>metric</th><th>value</th><th>n</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>"
        )

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>donna-evals scorecard</title>"
        "<style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse}"
        "th,td{border:1px solid #ccc;padding:4px 8px;text-align:left}</style>"
        "</head><body>"
        f"<h1>Scorecard ({scorecard.get('n_cases', 0)} cases)</h1>"
        "<table><thead><tr><th>metric</th><th>value</th><th>n</th>"
        "<th>threshold</th><th>pass</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table>"
        + "".join(sections)
        + "</body></html>\n"
    )
