"""Coverage + ground-truth tests for the calendar golden set (E-07)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

EVALS = Path(__file__).resolve().parents[1]
CAL = EVALS / "golden" / "calendar"
DEV = CAL / "dev.jsonl"
TEST = CAL / "test.jsonl"
BUILD = CAL / "build.py"
SLOT = CAL / "slot_truth.json"
INDEX = EVALS / "fixtures" / "index.json"
CALENDAR = EVALS / "fixtures" / "calendar"

TARGET_SPLIT = {"dev": 50, "test": 150}
TARGET_DIFFICULTY = {"easy": 80, "medium": 80, "hard": 40}
TARGET_JOB = {"J5": 55, "J6": 60, "J7": 65, "J11": 20}
MUTATE_TOOLS = {"create_event", "update_event", "cancel_event"}


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
def slot_truth() -> dict:
    return json.loads(SLOT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def calendar_events() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in CALENDAR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[f"calendar/{path.stem}"] = {e["event_id"] for e in data["events"]}
    return out


@pytest.fixture(scope="module")
def cal_build():
    sys.path.insert(0, str(CAL))
    try:
        import build as calendar_build  # type: ignore

        return calendar_build
    finally:
        sys.path.pop(0)


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
    assert len(esc) == 20
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
        assert c["id"].startswith("cal-")
        assert "tool_calls" in c["expected"]
        assert c["expected"]["match"] == "structured"


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


def test_event_ids_exist_in_fixture(
    cases: list[dict], calendar_events: dict[str, set[str]]
) -> None:
    for c in cases:
        fixtures = c["context"]["fixtures"]
        cal_refs = [f for f in fixtures if f.startswith("calendar/")]
        if not cal_refs:
            continue
        known: set[str] = set()
        for ref in cal_refs:
            known |= calendar_events[ref]
        for call in c["expected"].get("tool_calls") or []:
            if call["name"] not in MUTATE_TOOLS:
                continue
            eid = call.get("args", {}).get("event_id")
            if eid:
                assert eid in known, (c["id"], eid, cal_refs)


def test_datetime_offsets_match_case_tz(cases: list[dict]) -> None:
    for c in cases:
        tz = ZoneInfo(c["context"]["timezone"])
        for call in c["expected"].get("tool_calls") or []:
            args = call.get("args") or {}
            for key in ("start", "end", "after", "before"):
                if key not in args or args[key] is None:
                    continue
                raw = args[key]
                dt = datetime.fromisoformat(raw)
                assert dt.tzinfo is not None, (c["id"], key, raw)
                local = dt.astimezone(tz)
                expected_off = local.utcoffset()
                actual_off = dt.utcoffset()
                assert actual_off == expected_off, (c["id"], key, raw, actual_off, expected_off)
                # Wall date in case TZ should reproduce the same offset.
                wall = datetime(
                    local.year,
                    local.month,
                    local.day,
                    local.hour,
                    local.minute,
                    local.second,
                    tzinfo=tz,
                )
                assert wall.utcoffset() == actual_off, (c["id"], key, raw)


def test_j6_slot_truth(
    cases: list[dict], slot_truth: dict, cal_build
) -> None:
    j6_ids = {c["id"] for c in cases if c["job"] == "J6"}
    assert j6_ids
    assert j6_ids <= set(slot_truth.keys())
    assert set(slot_truth.keys()) == j6_ids

    for c in cases:
        if c["job"] != "J6":
            continue
        assert "slot_truth:slot_truth.json" in c["tags"]
        assert c["expected"]["match"] == "structured"
        call = c["expected"]["tool_calls"][0]
        assert call["name"] == "find_free_slot"
        args = call["args"]
        truth = slot_truth[c["id"]]
        if "no_slot" in c["tags"]:
            assert truth is None or truth == {} or truth == []
            continue
        assert isinstance(truth, dict), c["id"]
        assert "start" in truth and "end" in truth, c["id"]
        start = datetime.fromisoformat(truth["start"])
        end = datetime.fromisoformat(truth["end"])
        assert end - start == timedelta(minutes=args["duration_min"]), c["id"]

        persona = c["context"]["fixtures"][0].split("/", 1)[1]
        tz = c["context"]["timezone"]
        after = datetime.fromisoformat(args["after"])
        before = (
            datetime.fromisoformat(args["before"])
            if args.get("before")
            else after + timedelta(days=7)
        )
        busy = cal_build.combined_busy(
            persona, after, before, tz, args.get("attendees")
        )
        for bs, be in busy:
            overlap = start < be and end > bs
            assert not overlap, (c["id"], truth, bs, be)

        # Recompute earliest and compare.
        recomputed = cal_build.compute_slot(
            persona,
            args["duration_min"],
            args["after"],
            args.get("before"),
            tz,
            args.get("attendees"),
        )
        assert recomputed == truth, (c["id"], recomputed, truth)


def test_j5_list_events(cases: list[dict]) -> None:
    for c in cases:
        if c["job"] != "J5":
            continue
        calls = c["expected"]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["name"] == "list_events"
        args = calls[0]["args"]
        assert datetime.fromisoformat(args["start"]) < datetime.fromisoformat(args["end"])


def test_j7_drafts_only(cases: list[dict]) -> None:
    for c in cases:
        if c["job"] != "J7":
            continue
        for call in c["expected"]["tool_calls"]:
            assert call["name"] in MUTATE_TOOLS, c["id"]
            assert call["name"] != "send_email"


def test_build_twice_identical_bytes(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    sys.path.insert(0, str(CAL))
    try:
        import build as calendar_build  # type: ignore

        cases_a, truth_a = calendar_build.build_cases()
        calendar_build.write_outputs(cases_a, truth_a, out_a)
        cases_b, truth_b = calendar_build.build_cases()
        calendar_build.write_outputs(cases_b, truth_b, out_b)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    for name in ("dev.jsonl", "test.jsonl", "slot_truth.json"):
        a = (out_a / name).read_bytes()
        b = (out_b / name).read_bytes()
        assert a == b
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_no_placeholder_input_text(cases: list[dict]) -> None:
    bad = re.compile(r"\[cal-|#\d|\b(meeting|slot|event|hold) \d+\b|target day", re.I)
    for c in cases:
        assert not bad.search(c["input"]["text"]), (c["id"], c["input"]["text"])


def test_dst_edge_coverage(cases: list[dict]) -> None:
    dst = [c for c in cases if "calendar/dst-edge" in c["context"]["fixtures"]]
    assert len(dst) >= 15
    jobs = {c["job"] for c in dst}
    assert "J5" in jobs and "J6" in jobs and "J7" in jobs


def test_no_dst_token_in_inputs(cases: list[dict]) -> None:
    dst_token = re.compile(r"\bDST\b")
    for c in cases:
        assert not dst_token.search(c["input"]["text"]), (c["id"], c["input"]["text"])


def test_dst_spanning_week_contains_transition(cases: list[dict]) -> None:
    transitions = {
        "Europe/Amsterdam": datetime.fromisoformat("2026-10-25T01:00:00+00:00"),
        "America/New_York": datetime.fromisoformat("2026-11-01T06:00:00+00:00"),
    }
    span_re = re.compile(r"span|across|cross|clock change week|spanning.*clock change", re.I)
    matched = 0
    for c in cases:
        if "calendar/dst-edge" not in c["context"]["fixtures"]:
            continue
        if not span_re.search(c["input"]["text"]):
            continue
        matched += 1
        instant = transitions[c["context"]["timezone"]]
        for call in c["expected"]["tool_calls"]:
            if call["name"] != "list_events":
                continue
            start = datetime.fromisoformat(call["args"]["start"])
            end = datetime.fromisoformat(call["args"]["end"])
            assert start <= instant < end, (c["id"], call["args"], instant)
    assert matched >= 2


def test_now_time_of_day_spread(cases: list[dict]) -> None:
    times = Counter(c["context"]["now"][11:16] for c in cases)
    top_n = max(times.values())
    assert top_n / len(cases) <= 0.30, times.most_common(5)
    # Some weekend days present
    weekend = sum(
        1
        for c in cases
        if datetime.fromisoformat(c["context"]["now"]).astimezone(
            ZoneInfo(c["context"]["timezone"])
        ).weekday()
        >= 5
    )
    assert weekend >= 10


def test_checked_in_files_match_builder(tmp_path: Path) -> None:
    sys.path.insert(0, str(CAL))
    try:
        import build as calendar_build  # type: ignore

        cases, truth = calendar_build.build_cases()
        calendar_build.write_outputs(cases, truth, tmp_path)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    assert (tmp_path / "dev.jsonl").read_bytes() == DEV.read_bytes()
    assert (tmp_path / "test.jsonl").read_bytes() == TEST.read_bytes()
    assert (tmp_path / "slot_truth.json").read_bytes() == SLOT.read_bytes()
