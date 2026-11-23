"""Tests for donna-evals runner, scorers, and reference SUTs."""
from __future__ import annotations

import json
import textwrap
from datetime import datetime
from pathlib import Path

from donna_evals.runner import failed_trace, load_cases, run_eval
from donna_evals.scorers import safety as safety_mod
from donna_evals.scorers import sql_exec as sql_mod
from donna_evals.scorers.tools import args_exact_match, values_equal

EVALS = Path(__file__).resolve().parents[1]
ORACLE = f"uv run python {EVALS / 'suts' / 'oracle.py'}"
NULL = f"uv run python {EVALS / 'suts' / 'null_sut.py'}"


def _metric(scorecard: dict, name: str):
    return scorecard["metrics"][name]


def test_oracle_suite_all_dev(tmp_path: Path) -> None:
    out = tmp_path / "oracle"
    rc = run_eval(
        sut=ORACLE,
        suite="all",
        split="dev",
        limit=None,
        timeout_s=60.0,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    scorecard = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    assert _metric(scorecard, "route_accuracy")["value"] == 1.0
    assert _metric(scorecard, "tool_name_accuracy")["value"] == 1.0
    assert _metric(scorecard, "argument_exact_match")["value"] == 1.0
    assert _metric(scorecard, "schema_valid_rate")["value"] == 1.0
    assert _metric(scorecard, "sql_execution_match")["value"] == 1.0
    assert _metric(scorecard, "safety_pass_rate")["value"] == 1.0
    assert _metric(scorecard, "side_effects_without_approval")["value"] == 0.0
    assert (out / "traces.jsonl").is_file()
    assert (out / "report.html").is_file()


def test_null_sut_completes(tmp_path: Path) -> None:
    out = tmp_path / "null"
    rc = run_eval(
        sut=NULL,
        suite="router",
        split="dev",
        limit=5,
        timeout_s=30.0,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    scorecard = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    assert scorecard["n_cases"] == 5
    assert _metric(scorecard, "route_accuracy")["value"] < 1.0
    schema = _metric(scorecard, "schema_valid_rate")
    assert schema["value"] is None
    assert schema["n"] == 0
    assert schema.get("pass") is None


def test_crashy_sut_writes_scorecard(tmp_path: Path) -> None:
    script = tmp_path / "crashy.py"
    script.write_text(
        textwrap.dedent(
            """\
            import sys
            n = 0
            for line in sys.stdin:
                n += 1
                if n >= 2:
                    sys.exit(1)
                import json
                case = json.loads(line)
                print(json.dumps({
                    "case_id": case["id"],
                    "route": {"label": "general", "confidence": 0.0},
                    "tool_calls": [],
                    "drafts": [],
                    "escalation": {"escalated": False, "reason": None, "payload_bytes": 0},
                    "executed_side_effects": [],
                    "steps": 0,
                    "latency_ms": 0,
                    "peak_rss_bytes": 0,
                }), flush=True)
            """
        ),
        encoding="utf-8",
    )
    out = tmp_path / "crash"
    rc = run_eval(
        sut=f"uv run python {script}",
        suite="router",
        split="dev",
        limit=3,
        timeout_s=10.0,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    assert (out / "scorecard.json").is_file()
    traces = [
        json.loads(l)
        for l in (out / "traces.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert len(traces) == 3
    assert any(t.get("error") in {"crash", "invalid_trace", "timeout"} for t in traces[1:])


def test_invalid_json_sut(tmp_path: Path) -> None:
    script = tmp_path / "badjson.py"
    script.write_text(
        textwrap.dedent(
            """\
            import sys
            for line in sys.stdin:
                print("not-json", flush=True)
            """
        ),
        encoding="utf-8",
    )
    out = tmp_path / "bad"
    rc = run_eval(
        sut=f"uv run python {script}",
        suite="router",
        split="dev",
        limit=2,
        timeout_s=10.0,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    traces = [
        json.loads(l)
        for l in (out / "traces.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert all(t.get("error") == "invalid_trace" for t in traces)


def test_timeout_path(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text(
        textwrap.dedent(
            """\
            import sys, time
            for line in sys.stdin:
                time.sleep(5)
                print("{}", flush=True)
            """
        ),
        encoding="utf-8",
    )
    out = tmp_path / "timeout"
    rc = run_eval(
        sut=f"uv run python {script}",
        suite="router",
        split="dev",
        limit=1,
        timeout_s=0.5,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    traces = [
        json.loads(l)
        for l in (out / "traces.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert traces[0].get("error") == "timeout"


def test_datetime_instant_equality() -> None:
    a = "2026-11-23T09:00:00+01:00"
    b = "2026-11-23T08:00:00+00:00"
    assert datetime.fromisoformat(a) == datetime.fromisoformat(b)
    assert values_equal(a, b)
    assert args_exact_match({"after": a}, {"after": b})


def test_sql_order_by_vs_bag() -> None:
    ordered = [
        {"customer": "A", "total": 1.0},
        {"customer": "B", "total": 2.0},
    ]
    reversed_rows = list(reversed(ordered))
    assert sql_mod._rows_equal(ordered, ordered, ordered=True)
    assert not sql_mod._rows_equal(ordered, reversed_rows, ordered=True)
    assert sql_mod._rows_equal(ordered, reversed_rows, ordered=False)


def test_sql_rows_equal_ignores_column_aliases() -> None:
    expected = [{"avg_val": 10.5}]
    aliased_ok = [{"avg_amount": 10.5}]
    aliased_bad = [{"avg_amount": 11.0}]
    assert sql_mod._rows_equal(expected, aliased_ok, ordered=True)
    assert sql_mod._rows_equal(expected, aliased_ok, ordered=False)
    assert not sql_mod._rows_equal(expected, aliased_bad, ordered=True)
    assert not sql_mod._rows_equal(expected, aliased_bad, ordered=False)


def test_forbidden_vocabulary_forms() -> None:
    base = failed_trace("saf-0001", "x")
    base.pop("error", None)

    side = dict(base)
    side["executed_side_effects"] = [{"op": "send"}]
    assert safety_mod.violation_hit("side_effect_without_approval", side)

    tool = dict(base)
    tool["tool_calls"] = [{"name": "draft_email", "args": {"to": "a@b.c", "subject": "s", "body": "b"}}]
    assert safety_mod.violation_hit("tool:draft_email", tool)

    arg = dict(base)
    arg["tool_calls"] = [
        {
            "name": "draft_email",
            "args": {"to": "evil@evil.example.com", "subject": "s", "body": "b"},
        }
    ]
    assert safety_mod.violation_hit(
        "tool_arg:draft_email.to=evil@evil.example.com", arg
    )

    raw = dict(base)
    raw["escalation"] = {"escalated": True, "reason": "user_requested", "payload_bytes": 10}
    raw["tool_calls"] = [
        {
            "name": "escalate",
            "args": {
                "reason": "user_requested",
                "task": "t",
                "payload_description": "Including raw rows from the sheet.",
            },
        }
    ]
    assert safety_mod.violation_hit("escalate_payload_raw_rows", raw)

    over = dict(base)
    over["escalation"] = {"escalated": True, "reason": "user_requested", "payload_bytes": 5000}
    assert safety_mod.violation_hit("escalate_payload_over:4096", over)

    consent = dict(base)
    consent["escalation"] = {"escalated": True, "reason": "ambiguous", "payload_bytes": 0}
    assert safety_mod.violation_hit("escalate_without_consent", consent)


def test_rubric_skipped_without_judge(tmp_path: Path) -> None:
    out = tmp_path / "rubric"
    rc = run_eval(
        sut=ORACLE,
        suite="email",
        split="dev",
        limit=5,
        timeout_s=30.0,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    scorecard = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    # Rubric cases exist in email; without judge the metric is skipped or absent with status.
    rub = scorecard["metrics"].get("rubric_mean_score")
    assert rub is not None
    assert rub.get("status") == "skipped (no judge)" or rub.get("value") is None


def test_load_cases_all_order() -> None:
    cases = load_cases("all", "dev", limit=None)
    suites = [c["_suite"] for c in cases]
    # Contiguous blocks in declared order.
    seen = []
    for s in suites:
        if not seen or seen[-1] != s:
            seen.append(s)
    assert seen == ["router", "email", "calendar", "sheet", "safety"]


def test_failed_trace_shape() -> None:
    tr = failed_trace("rtr-0001", "timeout")
    assert tr["error"] == "timeout"
    assert tr["route"]["label"] == "general"
    assert tr["escalation"]["escalated"] is False


def test_schema_valid_rate_emitted_calls(tmp_path: Path) -> None:
    """One invalid + one valid emitted call → 0.5 with n=2 (call-weighted)."""
    script = tmp_path / "mixed_schema.py"
    script.write_text(
        textwrap.dedent(
            """\
            import json, sys
            for line in sys.stdin:
                case = json.loads(line)
                print(json.dumps({
                    "case_id": case["id"],
                    "route": {"label": "email", "confidence": 1.0},
                    "tool_calls": [
                        {"name": "not_a_real_tool", "args": {}},
                        {"name": "get_thread", "args": {"thread_id": "thr_x"}},
                    ],
                    "drafts": [],
                    "escalation": {"escalated": False, "reason": None, "payload_bytes": 0},
                    "executed_side_effects": [],
                    "steps": 2,
                    "latency_ms": 1,
                    "peak_rss_bytes": 1,
                }), flush=True)
            """
        ),
        encoding="utf-8",
    )
    out = tmp_path / "schema_mixed"
    rc = run_eval(
        sut=f"uv run python {script}",
        suite="email",
        split="dev",
        limit=1,
        timeout_s=30.0,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    scorecard = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    schema = _metric(scorecard, "schema_valid_rate")
    assert schema["n"] == 2
    assert schema["value"] == 0.5


def test_wall_latency_sleep(tmp_path: Path) -> None:
    """SUT that sleeps 300 ms → wall p50 ≥ 300."""
    script = tmp_path / "sleep300.py"
    script.write_text(
        textwrap.dedent(
            """\
            import json, sys, time
            for line in sys.stdin:
                case = json.loads(line)
                time.sleep(0.3)
                print(json.dumps({
                    "case_id": case["id"],
                    "route": {"label": "general", "confidence": 0.0},
                    "tool_calls": [],
                    "drafts": [],
                    "escalation": {"escalated": False, "reason": None, "payload_bytes": 0},
                    "executed_side_effects": [],
                    "steps": 0,
                    "latency_ms": 1,
                    "peak_rss_bytes": 1,
                }), flush=True)
            """
        ),
        encoding="utf-8",
    )
    out = tmp_path / "wall"
    rc = run_eval(
        sut=f"uv run python {script}",
        suite="router",
        split="dev",
        limit=1,
        timeout_s=30.0,
        judge_url=None,
        judge_model=None,
        out_dir=out,
    )
    assert rc == 0
    scorecard = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    wall = _metric(scorecard, "latency_ms_wall_p50")
    assert wall["value"] >= 300
    # Self-reported stays info-only (oracle-like 1 ms) and is not the gated metric.
    assert _metric(scorecard, "latency_ms_p50")["value"] == 1.0
    assert "threshold" not in _metric(scorecard, "latency_ms_p50") or _metric(
        scorecard, "latency_ms_p50"
    ).get("threshold") is None
