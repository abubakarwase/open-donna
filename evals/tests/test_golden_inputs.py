"""Repo-wide guard: golden input.text must look like real user prompts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1]
GOLDEN = EVALS / "golden"

ID_OR_NUM_BRACKET = re.compile(r"\[(saf|sht|cal|eml|rtr)-|\[\d|#\d+\b")
FIXTURE_OR_LABEL = re.compile(
    r"clean-\d|messy-\d|mailbox/|calendar/|sheets/|fixture:|triage:|needs_human_review",
    re.I,
)


def _all_cases() -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    for suite in sorted(GOLDEN.iterdir()):
        if not suite.is_dir():
            continue
        for path in sorted(suite.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                case = json.loads(line)
                rows.append((f"{suite.name}/{path.name}", case))
    return rows


@pytest.fixture(scope="module")
def cases() -> list[tuple[str, dict]]:
    return _all_cases()


def test_input_text_no_ids_fixtures_or_labels(cases: list[tuple[str, dict]]) -> None:
    hits: list[str] = []
    for where, case in cases:
        inp = case.get("input")
        if not isinstance(inp, dict) or "text" not in inp:
            # Non-SUT rows (e.g. judge_calibration) have no user prompt text.
            continue
        text = inp["text"]
        if ID_OR_NUM_BRACKET.search(text) or FIXTURE_OR_LABEL.search(text):
            hits.append(f"{where}:{case['id']}: {text[:100]}")
    assert not hits, "bad input.text:\n" + "\n".join(hits[:20])
