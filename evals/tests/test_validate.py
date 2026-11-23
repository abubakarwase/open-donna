"""Tests for donna-evals-validate (cases + traces)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from donna_evals.validate import main, validate_cases, validate_traces

SPEC = Path(__file__).resolve().parents[1] / "spec"
EXAMPLES = SPEC / "examples"
CASES = EXAMPLES / "cases.jsonl"
TRACES = EXAMPLES / "traces.jsonl"


def test_example_cases_pass() -> None:
    assert validate_cases([CASES]) == 0


def test_example_traces_pass() -> None:
    assert validate_traces([TRACES]) == 0


def test_cli_cases_and_traces_exit_zero() -> None:
    assert main(["cases", str(CASES)]) == 0
    assert main(["traces", str(TRACES)]) == 0


def test_bad_tool_args_fail(tmp_path: Path) -> None:
    line = json.loads(CASES.read_text(encoding="utf-8").splitlines()[0])
    line["id"] = "email-9999"
    line["expected"]["tool_calls"] = [{"name": "get_thread", "args": {}}]
    path = tmp_path / "bad_args.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert validate_cases([path]) == 1


def test_duplicate_id_fail(tmp_path: Path) -> None:
    first = CASES.read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "dup.jsonl"
    path.write_text(first + "\n" + first + "\n", encoding="utf-8")
    assert validate_cases([path]) == 1


def test_unknown_route_fail(tmp_path: Path) -> None:
    line = json.loads(CASES.read_text(encoding="utf-8").splitlines()[0])
    line["id"] = "email-9998"
    line["expected"]["route"] = "multi_step"
    path = tmp_path / "bad_route.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert validate_cases([path]) == 1


def test_escalate_without_reason_fail(tmp_path: Path) -> None:
    line = json.loads(CASES.read_text(encoding="utf-8").splitlines()[0])
    line["id"] = "email-9997"
    line["expected"]["escalation"] = "escalate"
    line["expected"].pop("escalate_reason", None)
    path = tmp_path / "esc_noreason.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert validate_cases([path]) == 1


def test_trace_soft_tool_args_do_not_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line = json.loads(TRACES.read_text(encoding="utf-8").splitlines()[0])
    line["tool_calls"] = [{"name": "find_free_slot", "args": {"duration_minutes": 30}}]
    path = tmp_path / "soft.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert validate_traces([path]) == 0
    out = capsys.readouterr().out
    assert "schema_valid: false=1" in out
