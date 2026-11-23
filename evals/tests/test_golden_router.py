"""Coverage + determinism tests for the router golden set (E-05)."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1]
ROUTER = EVALS / "golden" / "router"
DEV = ROUTER / "dev.jsonl"
TEST = ROUTER / "test.jsonl"
BUILD = ROUTER / "build.py"
INDEX = EVALS / "fixtures" / "index.json"

TARGET_ROUTE = {
    "email": 45,
    "calendar": 45,
    "sheet": 35,
    "general": 25,
    "escalate": 50,
}
TARGET_SPLIT = {"dev": 50, "test": 150}
TARGET_DIFFICULTY = {"easy": 80, "medium": 80, "hard": 40}
ESCALATE_REASONS = {
    "user_requested",
    "multi_step",
    "low_confidence",
    "why_or_forecast",
    "cross_sheet_join",
    "messy_sheet",
    "ambiguous",
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


def test_total_and_splits(cases: list[dict]) -> None:
    assert len(cases) == 200
    splits = Counter(c["split"] for c in cases)
    assert splits["dev"] == TARGET_SPLIT["dev"]
    assert splits["test"] == TARGET_SPLIT["test"]
    assert all(c["split"] == "dev" for c in _load(DEV))
    assert all(c["split"] == "test" for c in _load(TEST))


def test_route_counts(cases: list[dict]) -> None:
    routes = Counter(c["expected"]["route"] for c in cases)
    for route, target in TARGET_ROUTE.items():
        assert abs(routes[route] - target) <= 2, (route, routes[route], target)
    assert sum(routes.values()) == 200


def test_difficulty_mix(cases: list[dict]) -> None:
    diff = Counter(c["difficulty"] for c in cases)
    for key, target in TARGET_DIFFICULTY.items():
        assert abs(diff[key] - target) <= 5, (key, diff[key], target)


def test_escalate_reasons(cases: list[dict]) -> None:
    reasons = {
        c["expected"]["escalate_reason"]
        for c in cases
        if c["expected"]["escalation"] == "escalate"
    }
    assert reasons == ESCALATE_REASONS
    assert "schema_failed" not in reasons
    for c in cases:
        if c["expected"]["escalation"] == "escalate":
            assert c["expected"].get("escalate_reason") in ESCALATE_REASONS
            assert c["skill"] == "escalate"
            assert c["job"] == "J11"
            assert c["expected"]["route"] == "escalate"


def test_layer_match_ids_skill(cases: list[dict]) -> None:
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c["layer"] == 1
        assert c["expected"]["match"] == "exact"
        assert c["source"] == "synthetic"
        assert c["skill"] == c["expected"]["route"]
        assert c["id"].startswith("rtr-")
        assert "tool_calls" not in c["expected"]


def test_test_split_needs_human_review(cases: list[dict]) -> None:
    for c in cases:
        if c["split"] == "test":
            assert "needs_human_review" in c["tags"]
        else:
            assert "needs_human_review" not in c["tags"]


def test_no_duplicate_input_text(cases: list[dict]) -> None:
    texts = [c["input"]["text"] for c in cases]
    assert len(texts) == len(set(texts))


def _scenario_tag(case: dict) -> str:
    tags = [t for t in case["tags"] if t.startswith("scenario:")]
    assert len(tags) == 1, case["id"]
    return tags[0]


def test_scenario_paraphrases_stay_in_one_split(cases: list[dict]) -> None:
    """Near-duplicate paraphrases share scenario:* and must not cross splits."""
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
    assert max(counts.values()) >= 2


def test_builder_families_group_paraphrases() -> None:
    sys.path.insert(0, str(ROUTER))
    try:
        import build as router_build  # type: ignore

        families = (
            router_build._email_families()
            + router_build._calendar_families()
            + router_build._sheet_families()
            + router_build._general_families()
            + router_build._escalate_families()
        )
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    assert any(len(f.texts) >= 2 for f in families)
    assert all(len(f.texts) >= 1 for f in families)
    # Email Dana/Q3 paraphrases are one family, not split across families
    dana = [f for f in families if f.key.startswith("email-J1-00")]
    assert len(dana) == 1
    assert len(dana[0].texts) >= 2


def test_fixture_refs_valid(cases: list[dict], fixture_refs: set[str]) -> None:
    for c in cases:
        for ref in c["context"]["fixtures"]:
            assert ref in fixture_refs, (c["id"], ref)


def test_sheet_selection_cases(cases: list[dict]) -> None:
    j9 = [c for c in cases if c["job"] == "J9"]
    assert len(j9) >= 10
    for c in j9:
        sel = c["input"].get("selection")
        assert sel and sel.get("kind") == "sheet_range"
        assert sel.get("sheet") and sel.get("range")


def test_build_twice_identical_bytes(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    sys.path.insert(0, str(ROUTER))
    try:
        import build as router_build  # type: ignore

        cases_a = router_build.build_cases()
        router_build.write_outputs(cases_a, out_a)
        cases_b = router_build.build_cases()
        router_build.write_outputs(cases_b, out_b)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    for name in ("dev.jsonl", "test.jsonl"):
        a = (out_a / name).read_bytes()
        b = (out_b / name).read_bytes()
        assert a == b
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_checked_in_files_match_builder(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROUTER))
    try:
        import build as router_build  # type: ignore

        cases = router_build.build_cases()
        router_build.write_outputs(cases, tmp_path)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    assert (tmp_path / "dev.jsonl").read_bytes() == DEV.read_bytes()
    assert (tmp_path / "test.jsonl").read_bytes() == TEST.read_bytes()
