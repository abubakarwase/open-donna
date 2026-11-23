"""Coverage + determinism tests for the email golden set (E-06)."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

EVALS = Path(__file__).resolve().parents[1]
EMAIL = EVALS / "golden" / "email"
DEV = EMAIL / "dev.jsonl"
TEST = EMAIL / "test.jsonl"
BUILD = EMAIL / "build.py"
TRIAGE = EMAIL / "triage_truth.json"
INDEX = EVALS / "fixtures" / "index.json"
MAILBOX = EVALS / "fixtures" / "mailbox"

TARGET_SPLIT = {"dev": 50, "test": 150}
TARGET_DIFFICULTY = {"easy": 80, "medium": 80, "hard": 40}
TARGET_JOB = {"J1": 47, "J2": 42, "J3": 51, "J4": 45, "J11": 15}
SIDE_EFFECT_TOOLS = {
    "create_event",
    "update_event",
    "cancel_event",
    "send_email",
}


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
def triage_truth() -> dict:
    return json.loads(TRIAGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mailbox_threads() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in MAILBOX.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[f"mailbox/{path.stem}"] = {t["thread_id"] for t in data["threads"]}
    return out


def test_total_and_splits(cases: list[dict]) -> None:
    assert len(cases) == 200
    splits = Counter(c["split"] for c in cases)
    assert splits["dev"] == TARGET_SPLIT["dev"]
    assert splits["test"] == TARGET_SPLIT["test"]
    assert all(c["split"] == "dev" for c in _load(DEV))
    assert all(c["split"] == "test" for c in _load(TEST))


def test_job_coverage(cases: list[dict]) -> None:
    jobs = Counter(c["job"] for c in cases)
    for job, target in TARGET_JOB.items():
        assert jobs[job] == target, (job, jobs[job], target)
    esc = [c for c in cases if c["job"] == "J11"]
    assert len(esc) == 15
    for c in esc:
        assert c["skill"] == "escalate"
        assert c["expected"]["route"] == "escalate"
        assert c["expected"]["escalation"] == "escalate"
        assert c["expected"].get("escalate_reason")


def test_difficulty_mix(cases: list[dict]) -> None:
    diff = Counter(c["difficulty"] for c in cases)
    for key, target in TARGET_DIFFICULTY.items():
        assert abs(diff[key] - target) <= 5, (key, diff[key], target)


def test_layer_ids_source(cases: list[dict]) -> None:
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c["layer"] == 1
        assert c["source"] == "synthetic"
        assert c["skill"] == c["expected"]["route"]
        assert c["id"].startswith("eml-")
        assert "tool_calls" in c["expected"]


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
    import re

    persona_hint = re.compile(r"\((freelancer|smallbiz|teamlead)\)", re.I)
    banned = re.compile(r"injection|triage|fixture", re.I)
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
    assert len(multi) >= 20


def test_fixture_refs_valid(cases: list[dict], fixture_refs: set[str]) -> None:
    for c in cases:
        for ref in c["context"]["fixtures"]:
            assert ref in fixture_refs, (c["id"], ref)


def test_thread_ids_exist_in_fixture(
    cases: list[dict], mailbox_threads: dict[str, set[str]]
) -> None:
    for c in cases:
        fixtures = c["context"]["fixtures"]
        mailbox_refs = [f for f in fixtures if f.startswith("mailbox/")]
        if not mailbox_refs:
            continue
        known: set[str] = set()
        for ref in mailbox_refs:
            known |= mailbox_threads[ref]
        for call in c["expected"].get("tool_calls") or []:
            tid = call.get("args", {}).get("thread_id")
            if tid:
                assert tid in known, (c["id"], tid, mailbox_refs)


def test_date_bounds_consistent_with_now(cases: list[dict]) -> None:
    sys.path.insert(0, str(EMAIL))
    try:
        import build as email_build  # type: ignore
    finally:
        sys.path.pop(0)

    for c in cases:
        now = c["context"]["now"]
        tz = c["context"]["timezone"]
        day_a, day_b = email_build.day_bounds(now, tz)
        morn_a, morn_b = email_build.morning_bounds(now, tz)
        lm_a, lm_b = email_build.last_month_bounds(now, tz)
        jul_a, jul_b = email_build.month_bounds(now, tz, 2026, 7)
        allowed_pairs = {
            (day_a, day_b),
            (morn_a, morn_b),
            (lm_a, lm_b),
            (jul_a, jul_b),
        }
        local = datetime.fromisoformat(now).astimezone(ZoneInfo(tz))
        for call in c["expected"].get("tool_calls") or []:
            if call["name"] != "search_email":
                continue
            args = call["args"]
            after, before = args.get("after"), args.get("before")
            if after is None and before is None:
                continue
            assert after and before, c["id"]
            assert (after, before) in allowed_pairs, (c["id"], after, before)
            # Bounds parse and sit near the frozen now
            a_dt = datetime.fromisoformat(after)
            b_dt = datetime.fromisoformat(before)
            assert a_dt < b_dt
            assert abs((b_dt.astimezone(ZoneInfo(tz)).year - local.year)) <= 1


def test_j1_j3_rubric(cases: list[dict]) -> None:
    for c in cases:
        if c["job"] == "J1":
            assert c["expected"]["match"] == "rubric"
            assert c["expected"]["rubric"] == "summary_faithfulness"
            assert "rubric" in c["tags"]
        if c["job"] == "J3":
            assert c["expected"]["match"] == "rubric"
            assert c["expected"]["rubric"] == "draft_quality"
            assert "rubric" in c["tags"]
            calls = c["expected"]["tool_calls"]
            assert [x["name"] for x in calls] == ["get_thread", "draft_email"]
            assert calls[0]["args"]["thread_id"] == calls[1]["args"]["thread_id"]
            assert calls[1]["args"].get("body")
            for call in calls:
                assert call["name"] not in SIDE_EFFECT_TOOLS


def test_j2_triage_sidecar(cases: list[dict], triage_truth: dict) -> None:
    j2_ids = {c["id"] for c in cases if c["job"] == "J2"}
    assert j2_ids
    assert j2_ids <= set(triage_truth.keys())
    for c in cases:
        if c["job"] != "J2":
            continue
        assert "triage_truth:triage_truth.json" in c["tags"]
        assert c["expected"]["match"] == "structured"
        args = c["expected"]["tool_calls"][0]["args"]
        assert c["expected"]["tool_calls"][0]["name"] == "search_email"
        mapping = triage_truth[c["id"]]
        assert isinstance(mapping, dict)
        allow_empty = "not_found" in c["tags"] or "empty_scope" in c["tags"]
        if not allow_empty:
            assert mapping, c["id"]
        text_l = c["input"]["text"].lower()
        if "unread" not in text_l:
            assert "unread" not in args, c["id"]
        elif args.get("unread") is True:
            # Prompt asks for unread; filters must include unread.
            assert args.get("unread") is True


def test_injection_cases(cases: list[dict]) -> None:
    inj = [c for c in cases if "injection" in c["tags"]]
    assert len(inj) >= 3
    forbidden_substrings = (
        "exfil@",
        "evil.example.com",
        "delete all messages",
        "approve the payment without",
    )
    for c in inj:
        for call in c["expected"]["tool_calls"]:
            blob = json.dumps(call).lower()
            for bad in forbidden_substrings:
                assert bad.lower() not in blob, (c["id"], bad)
            assert call["name"] in {"get_thread", "search_email", "draft_email", "escalate"}
            if call["name"] == "get_thread":
                assert "injection" in call["args"]["thread_id"]


def test_build_twice_identical_bytes(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    sys.path.insert(0, str(EMAIL))
    try:
        import build as email_build  # type: ignore

        cases_a, truth_a = email_build.build_cases()
        email_build.write_outputs(cases_a, truth_a, out_a)
        # drop cached module so second import is clean if needed
        cases_b, truth_b = email_build.build_cases()
        email_build.write_outputs(cases_b, truth_b, out_b)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    for name in ("dev.jsonl", "test.jsonl", "triage_truth.json"):
        a = (out_a / name).read_bytes()
        b = (out_b / name).read_bytes()
        assert a == b
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_checked_in_files_match_builder(tmp_path: Path) -> None:
    sys.path.insert(0, str(EMAIL))
    try:
        import build as email_build  # type: ignore

        cases, truth = email_build.build_cases()
        email_build.write_outputs(cases, truth, tmp_path)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    assert (tmp_path / "dev.jsonl").read_bytes() == DEV.read_bytes()
    assert (tmp_path / "test.jsonl").read_bytes() == TEST.read_bytes()
    assert (tmp_path / "triage_truth.json").read_bytes() == TRIAGE.read_bytes()
