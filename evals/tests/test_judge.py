"""Tests for rubrics, LLM judge client, agreement, and calibration shape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from donna_evals import judge, judge_agreement
from donna_evals.judge_agreement import agreement, format_summary, intended_to_grades

EVALS = Path(__file__).resolve().parents[1]
RUBRICS = EVALS / "spec" / "rubrics"
CALIBRATION = EVALS / "golden" / "judge_calibration" / "cases.jsonl"

RUBRIC_IDS = (
    "summary_faithfulness",
    "draft_quality",
    "style_match",
    "plan_quality",
)


def test_all_rubrics_load_with_anchors() -> None:
    for rid in RUBRIC_IDS:
        data = judge.load_rubric(rid)
        assert data["id"] == rid
        assert data.get("invented_fact", {}).get("description")
        assert data["criteria"], rid
        for crit in data["criteria"]:
            assert crit["name"]
            assert crit["score_1"].strip()
            assert crit["score_3"].strip()
            assert crit["score_5"].strip()


def test_prompt_render_substitutes() -> None:
    text = judge.render_prompt(
        "score",
        rubric_text="RUB",
        source="SRC",
        output="OUT",
    )
    assert "RUB" in text and "SRC" in text and "OUT" in text
    assert "{rubric_text}" not in text

    pair = judge.render_prompt(
        "pairwise",
        rubric_text="R",
        source="S",
        a="A1",
        b="B1",
    )
    assert "A1" in pair and "B1" in pair


def test_score_good_json_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def transport(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        calls.append({"url": url, "headers": headers, "body": body})
        n = len(calls)
        if n == 1:
            content = json.dumps(
                {
                    "coverage": 5,
                    "precision": 5,
                    "invented_fact": False,
                    "rationale": "ok",
                }
            )
            return {"choices": [{"message": {"content": content}}]}
        raise AssertionError("unexpected extra call")

    out = judge.score(
        "summary_faithfulness",
        "source thread",
        "good summary",
        judge_url="http://judge.test/v1",
        judge_model="test-model",
        transport=transport,
    )
    assert out["coverage"] == 5
    assert out["invented_fact"] is False
    assert calls[0]["url"].endswith("/chat/completions")
    assert calls[0]["body"]["temperature"] == 0
    assert calls[0]["body"]["stream"] is False

    # bad then good → one retry
    calls.clear()

    def flaky(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "not-json"}}]}
        content = "```json\n" + json.dumps(
            {
                "coverage": 3,
                "precision": 3,
                "invented_fact": True,
                "rationale": "retry",
            }
        ) + "\n```"
        return {"choices": [{"message": {"content": content}}]}

    out2 = judge.score(
        "summary_faithfulness",
        "s",
        "o",
        judge_url="http://judge.test/v1",
        judge_model="m",
        transport=flaky,
    )
    assert out2["coverage"] == 3
    assert out2["invented_fact"] is True
    assert len(calls) == 2

    # two bad → raises
    calls.clear()

    def always_bad(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        return {"choices": [{"message": {"content": "still-bad"}}]}

    with pytest.raises(ValueError, match="valid JSON"):
        judge.score(
            "summary_faithfulness",
            "s",
            "o",
            judge_url="http://x",
            judge_model="m",
            transport=always_bad,
        )
    assert len(calls) == 2


def test_pairwise_order_swap_stable() -> None:
    # Biased toward whichever is labeled "a" → conflict → tie after invert.
    call_n = {"n": 0}

    def bias_a(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        call_n["n"] += 1
        return {"choices": [{"message": {"content": json.dumps({"winner": "a", "rationale": "bias"})}}]}

    assert (
        judge.pairwise(
            "draft_quality",
            "src",
            "draft-good",
            "draft-bad",
            judge_url="http://j",
            judge_model="m",
            transport=bias_a,
        )
        == "tie"
    )
    assert call_n["n"] == 2

    # Consistent preference for original a: first a, second (swapped) b → inverted a.
    seq = ["a", "b"]

    def consistent(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        w = seq.pop(0)
        return {"choices": [{"message": {"content": json.dumps({"winner": w, "rationale": "ok"})}}]}

    assert (
        judge.pairwise(
            "draft_quality",
            "src",
            "A",
            "B",
            judge_url="http://j",
            judge_model="m",
            transport=consistent,
        )
        == "a"
    )


def test_agreement_math_and_provisional() -> None:
    mapped = intended_to_grades("model_plain", ["coverage", "precision"])
    assert mapped["coverage"] == 5 and mapped["invented_fact"] is False
    assert intended_to_grades("good", ["coverage"])["coverage"] == 5  # alias
    assert intended_to_grades("invented_fact", ["precision", "coverage"])["invented_fact"] is True
    assert intended_to_grades("wrong_tone", ["tone", "clarity"])["tone"] == 1
    assert intended_to_grades("missing_ask", ["completeness", "tone"])["completeness"] == 1
    assert intended_to_grades("vague", ["coverage", "clarity"])["coverage"] == 1

    rows = [
        {
            "judge": {"coverage": 5, "precision": 5, "invented_fact": False},
            "human_grades": {"coverage": 5, "precision": 4, "invented_fact": False},
        },
        {
            "judge": {"coverage": 1, "precision": 3, "invented_fact": True},
            "human_grades": {"coverage": 1, "precision": 3, "invented_fact": True},
        },
        {
            "judge": {"coverage": 5, "precision": 1, "invented_fact": False},
            "human_grades": {"coverage": 5, "precision": 5, "invented_fact": True},
        },
    ]
    report = agreement(rows)
    assert report["provisional"] is False
    # coverage: |5-5|,|1-1|,|5-5| all agree
    assert report["per_criterion"]["coverage"] == 1.0
    # precision: |5-4|=1 ok, |3-3|=0 ok, |1-5|=4 fail → 2/3
    assert abs(report["per_criterion"]["precision"] - (2 / 3)) < 1e-9
    # invented_fact: tp=1 (row2), fp=0, fn=1 (row3) → P=1, R=0.5
    assert report["invented_fact"]["precision"] == 1.0
    assert report["invented_fact"]["recall"] == 0.5
    assert "judge vs human" in report["label"]

    prov_rows = [
        {
            "judge": {"coverage": 5, "precision": 5, "invented_fact": False},
            "human_grades": None,
            "intended_quality": "model_plain",
            "criteria": ["coverage", "precision"],
        },
        {
            "judge": {"coverage": 1, "precision": 3, "invented_fact": True},
            "human_grades": None,
            "intended_quality": "invented_fact",
            "criteria": ["coverage", "precision"],
        },
    ]
    prov = agreement(prov_rows)
    assert prov["provisional"] is True
    assert "provisional" in prov["label"]
    summary = format_summary(prov)
    assert "PROVISIONAL" in summary


def _char_overlap(candidate: str, source: str) -> float:
    """Fraction of candidate characters covered by matching blocks in source."""
    from difflib import SequenceMatcher

    if not candidate:
        return 0.0
    matcher = SequenceMatcher(None, candidate, source)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(candidate)


def test_calibration_cases_shape() -> None:
    import re

    assert CALIBRATION.is_file()
    lines = [ln for ln in CALIBRATION.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 50
    required = {
        "id",
        "kind",
        "fixture_ref",
        "thread_id",
        "source",
        "candidate",
        "intended_quality",
        "human_grades",
        "rubric",
    }
    id_re = re.compile(r"^jcal-\d{4}$")
    kinds = {"summary": 0, "draft": 0}
    for i, raw in enumerate(lines, start=1):
        row = json.loads(raw)
        assert required <= set(row)
        assert id_re.match(row["id"]), row["id"]
        assert row["id"] == f"jcal-{i:04d}"
        assert row["human_grades"] is None
        assert row["kind"] in kinds
        kinds[row["kind"]] += 1
        if row["kind"] == "summary":
            assert row["rubric"] == "summary_faithfulness"
        else:
            assert row["rubric"] == "draft_quality"
        assert _char_overlap(row["candidate"], row["source"]) <= 0.80, row["id"]
        if row["intended_quality"] == "invented_fact":
            note = row.get("defect_note")
            assert isinstance(note, str) and note.strip()
            tokens = _defect_key_tokens(note)
            assert tokens, f"{row['id']}: no key tokens in defect_note"
            low = row["candidate"].lower()
            missing = [t for t in tokens if t.lower() not in low]
            assert not missing, f"{row['id']}: candidate missing tokens {missing}"
    assert kinds == {"summary": 25, "draft": 25}


def _defect_key_tokens(note: str) -> list[str]:
    import re

    tokens: list[str] = []
    tokens += re.findall(r"\$[\d,.]+(?:[MmKk])?", note)
    tokens += re.findall(r"\b\d{1,2}:\d{2}\b", note)
    tokens += re.findall(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2}\b",
        note,
        flags=re.I,
    )
    tokens += re.findall(r"\b\d+-\d+\b", note)
    tokens += re.findall(r"\b(?:CFO|Legal|HR|finance|board)\b", note, flags=re.I)
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        key = tok.lower()
        if key not in seen:
            seen.add(key)
            out.append(tok)
    return out
