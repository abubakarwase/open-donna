"""Coverage + determinism tests for the safety golden set (E-11)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

EVALS = Path(__file__).resolve().parents[1]
SAFETY = EVALS / "golden" / "safety"
DEV = SAFETY / "dev.jsonl"
TEST = SAFETY / "test.jsonl"
BUILD = SAFETY / "build.py"
README = SAFETY / "README.md"
INDEX = EVALS / "fixtures" / "index.json"
MAILBOX = EVALS / "fixtures" / "mailbox"

TARGET_SPLIT = {"dev": 25, "test": 75}
TARGET_DIFFICULTY = {"easy": 40, "medium": 40, "hard": 20}
TARGET_CAT = {
    "mail_inj": 35,
    "cal_invite": 15,
    "sheet_inj": 15,
    "no_approval": 20,
    "exfil_esc": 15,
}

FORBIDDEN_EXACT = {
    "side_effect_without_approval",
    "escalate_payload_raw_rows",
    "escalate_without_consent",
}
FORBIDDEN_PREFIXES = ("tool:", "tool_arg:", "escalate_payload_over:")


def _load(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    assert DEV.is_file() and TEST.is_file()
    return _load(DEV) + _load(TEST)


@pytest.fixture(scope="module")
def fixture_refs() -> set[str]:
    return set(json.loads(INDEX.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def injection_threads() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in MAILBOX.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        tids: set[str] = set()
        for t in data["threads"]:
            if any(
                "fixture:injection" in (m.get("labels") or [])
                for m in t["messages"]
            ):
                tids.add(t["thread_id"])
        out[f"mailbox/{path.stem}"] = tids
    return out


def test_total_and_splits(cases: list[dict]) -> None:
    assert len(cases) == 100
    splits = Counter(c["split"] for c in cases)
    assert splits["dev"] == TARGET_SPLIT["dev"]
    assert splits["test"] == TARGET_SPLIT["test"]
    assert all(c["split"] == "dev" for c in _load(DEV))
    assert all(c["split"] == "test" for c in _load(TEST))


def test_category_coverage(cases: list[dict]) -> None:
    cats = Counter()
    for c in cases:
        hit = [t for t in TARGET_CAT if t in c["tags"]]
        assert len(hit) == 1, (c["id"], hit)
        cats[hit[0]] += 1
    for cat, target in TARGET_CAT.items():
        assert cats[cat] == target, (cat, cats[cat], target)


def test_difficulty_mix(cases: list[dict]) -> None:
    diff = Counter(c["difficulty"] for c in cases)
    for key, target in TARGET_DIFFICULTY.items():
        assert abs(diff[key] - target) <= 5, (key, diff[key], target)


def test_layer_ids_source_match(cases: list[dict]) -> None:
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c["layer"] == 4
        assert c["source"] == "synthetic"
        assert c["skill"] == c["expected"]["route"]
        assert c["skill"] in {"email", "calendar", "sheet", "escalate", "general"}
        assert c["skill"] != "safety"
        assert c["id"].startswith("saf-")
        assert c["expected"]["match"] == "safety"
        assert "tool_calls" in c["expected"]


def test_forbidden_vocab(cases: list[dict]) -> None:
    over_re = re.compile(r"^escalate_payload_over:\d+$")
    tool_re = re.compile(r"^tool:[a-z_]+$")
    tool_arg_re = re.compile(r"^tool_arg:[a-z_]+\.[a-z_]+=.+$")
    for c in cases:
        forbidden = c["expected"].get("forbidden") or []
        assert forbidden, c["id"]
        for tok in forbidden:
            if tok in FORBIDDEN_EXACT:
                continue
            if over_re.match(tok) or tool_re.match(tok) or tool_arg_re.match(tok):
                continue
            assert False, (c["id"], tok)


def test_test_split_needs_human_review(cases: list[dict]) -> None:
    for c in cases:
        if c["split"] == "test":
            assert "needs_human_review" in c["tags"]
        else:
            assert "needs_human_review" not in c["tags"]


def test_no_duplicate_input_text(cases: list[dict]) -> None:
    texts = [c["input"]["text"] for c in cases]
    assert len(texts) == len(set(texts))


def test_input_text_no_persona_or_fixture_labels(cases: list[dict]) -> None:
    persona_hint = re.compile(r"\((freelancer|smallbiz|teamlead)\)", re.I)
    banned = re.compile(r"injection|triage|fixture|clean-\d|messy-\d", re.I)
    for c in cases:
        text = c["input"]["text"]
        assert not persona_hint.search(text), (c["id"], text)
        assert not banned.search(text), (c["id"], text)


def test_now_local_working_hours(cases: list[dict]) -> None:
    for c in cases:
        now = c["context"]["now"]
        tz = c["context"]["timezone"]
        local = datetime.fromisoformat(now).astimezone(ZoneInfo(tz))
        assert 7 <= local.hour <= 20, (c["id"], now, local.hour)


def _scenario_tag(case: dict) -> str:
    tags = [t for t in case["tags"] if t.startswith("scenario:")]
    assert len(tags) == 1, case["id"]
    return tags[0]


def test_scenario_paraphrases_stay_in_one_split(cases: list[dict]) -> None:
    by_scenario: dict[str, set[str]] = {}
    counts: Counter[str] = Counter()
    for c in cases:
        scen = _scenario_tag(c)
        by_scenario.setdefault(scen, set()).add(c["split"])
        counts[scen] += 1
    for scen, splits in by_scenario.items():
        assert len(splits) == 1, (scen, splits)
    multi = [s for s, n in counts.items() if n >= 2]
    assert len(multi) >= 10


def test_fixture_refs_valid(cases: list[dict], fixture_refs: set[str]) -> None:
    for c in cases:
        for ref in c["context"]["fixtures"]:
            assert ref in fixture_refs, (c["id"], ref)


def test_injection_thread_refs(
    cases: list[dict], injection_threads: dict[str, set[str]]
) -> None:
    """Injection-tagged cases (not request-text) must use fixture:injection threads."""
    for c in cases:
        if "injection" not in c["tags"]:
            continue
        if "inj-request-text" in c["tags"]:
            continue
        mailbox_refs = [
            f for f in c["context"]["fixtures"] if f.startswith("mailbox/")
        ]
        assert mailbox_refs, c["id"]
        known: set[str] = set()
        for ref in mailbox_refs:
            known |= injection_threads[ref]
        for call in c["expected"].get("tool_calls") or []:
            tid = call.get("args", {}).get("thread_id")
            if tid:
                assert tid in known, (c["id"], tid, mailbox_refs)


def test_readme_vocab_documents_over_n() -> None:
    text = README.read_text(encoding="utf-8")
    assert "side_effect_without_approval" in text
    assert "escalate_payload_over:4096" in text
    assert "fixture gaps" in text.lower() or "Fixture gaps" in text


def test_build_twice_identical_bytes(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    sys.path.insert(0, str(SAFETY))
    try:
        import build as safety_build  # type: ignore

        cases_a = safety_build.build_cases()
        safety_build.write_outputs(cases_a, out_a)
        cases_b = safety_build.build_cases()
        safety_build.write_outputs(cases_b, out_b)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    for name in ("dev.jsonl", "test.jsonl"):
        a = (out_a / name).read_bytes()
        b = (out_b / name).read_bytes()
        assert a == b
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_checked_in_files_match_builder(tmp_path: Path) -> None:
    sys.path.insert(0, str(SAFETY))
    try:
        import build as safety_build  # type: ignore

        cases = safety_build.build_cases()
        safety_build.write_outputs(cases, tmp_path)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    assert (tmp_path / "dev.jsonl").read_bytes() == DEV.read_bytes()
    assert (tmp_path / "test.jsonl").read_bytes() == TEST.read_bytes()
