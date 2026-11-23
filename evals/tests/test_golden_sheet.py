"""Coverage + SQL re-exec tests for the sheet golden set (E-08)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from donna_evals.sheets import load_workbook_to_duckdb, normalize_sql_result, run_sql

EVALS = Path(__file__).resolve().parents[1]
SHEET = EVALS / "golden" / "sheet"
DEV = SHEET / "dev.jsonl"
TEST = SHEET / "test.jsonl"
BUILD = SHEET / "build.py"
INDEX = EVALS / "fixtures" / "index.json"

TARGET_SPLIT = {"dev": 50, "test": 150}
TARGET_DIFFICULTY = {"easy": 80, "medium": 80, "hard": 40}
TARGET_JOB = {"J8": 100, "J9": 50, "J11": 50}
ESCALATE_REASONS = {
    "low_confidence",
    "multi_step",
    "schema_failed",
    "messy_sheet",
    "cross_sheet_join",
    "why_or_forecast",
    "ambiguous",
    "user_requested",
}
BANNED_FIXTURE = re.compile(r"clean-\d|messy-\d|sheets/", re.I)
PRIVACY_BAD = re.compile(r"full sheet dump|dump all rows|include raw rows", re.I)


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


def test_job_coverage(cases: list[dict]) -> None:
    jobs = Counter(c["job"] for c in cases)
    for job, target in TARGET_JOB.items():
        assert jobs[job] == target, (job, jobs[job], target)
    esc = [c for c in cases if c["job"] == "J11"]
    assert len(esc) == 50
    for c in esc:
        assert c["skill"] == "escalate"
        assert c["expected"]["route"] == "escalate"
        assert c["expected"]["escalation"] == "escalate"
        assert c["expected"].get("escalate_reason") in ESCALATE_REASONS


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
        assert c["id"].startswith("sht-")
        assert "tool_calls" in c["expected"]


def test_local_sql_exec_shape(cases: list[dict]) -> None:
    for c in cases:
        if c["expected"]["escalation"] != "local":
            continue
        assert c["job"] in {"J8", "J9"}
        assert c["expected"]["match"] == "sql_exec"
        assert "sql_result" in c["expected"]
        calls = c["expected"]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["name"] == "query_sheet"
        args = calls[0]["args"]
        assert "sql" in args and "sheet" in args
        assert "FROM sheet" not in args["sql"]
        if c["job"] == "J9":
            assert "range" in args
            sel = c["input"].get("selection")
            assert sel and sel["kind"] == "sheet_range"
            assert sel["range"] == args["range"]
            assert sel["sheet"] == args["sheet"]


def _j8_op_from_scenario(tag: str) -> str:
    rest = tag.removeprefix("scenario:j8-")
    for op in (
        "filtercount",
        "lastmonth",
        "minmax",
        "lookup",
        "filter",
        "group",
        "count",
        "ratio",
        "topn",
        "sum",
        "avg",
    ):
        if rest == op or rest.startswith(op + "-"):
            return op
    return rest.split("-", 1)[0]


def test_j8_required_op_coverage(cases: list[dict]) -> None:
    """Plan §4: lookup, filter, SUM/AVG/COUNT, GROUP BY, top-N, MIN/MAX, last month, percentage."""
    required = {
        "sum",
        "avg",
        "count",
        "minmax",
        "group",
        "topn",
        "filter",
        "ratio",
        "lastmonth",
        "lookup",
    }
    ops: Counter[str] = Counter()
    for c in cases:
        if c["job"] != "J8":
            continue
        for tag in c["tags"]:
            if tag.startswith("scenario:j8-"):
                ops[_j8_op_from_scenario(tag)] += 1
    missing = required - set(ops)
    assert not missing, (missing, dict(ops))
    for op in required:
        assert ops[op] >= 1, (op, dict(ops))


def test_test_split_needs_human_review(cases: list[dict]) -> None:
    for c in cases:
        if c["split"] == "test":
            assert "needs_human_review" in c["tags"]
        else:
            assert "needs_human_review" not in c["tags"]


def test_no_duplicate_input_text(cases: list[dict]) -> None:
    texts = [c["input"]["text"] for c in cases]
    assert len(texts) == len(set(texts))


def test_input_text_bans_fixture_names(cases: list[dict]) -> None:
    for c in cases:
        text = c["input"]["text"]
        assert not BANNED_FIXTURE.search(text), (c["id"], text)


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


def test_messy_and_escalate_reasons(cases: list[dict]) -> None:
    esc = [c for c in cases if c["expected"]["escalation"] == "escalate"]
    assert len(esc) == 50
    reasons = Counter(c["expected"]["escalate_reason"] for c in esc)
    assert reasons["messy_sheet"] >= 8
    assert reasons["cross_sheet_join"] >= 8
    assert reasons["why_or_forecast"] >= 5
    assert reasons["ambiguous"] >= 5
    messy_refs = {f"sheets/messy-{i:02d}" for i in range(1, 11)}
    seen = {c["context"]["fixtures"][0] for c in esc if c["context"]["fixtures"][0].startswith("sheets/messy-")}
    assert messy_refs <= seen


def test_no_trivial_same_column_ratio(cases: list[dict]) -> None:
    """Gap 1: no X/X ratio prompts or trivial 1.0 ratio results from same column."""
    same_col = re.compile(
        r"ratio of total (\w+) to total \1\b|"
        r"\b(\w+) over \2 for\b|"
        r"\b(\w+)/\3 ratio\b",
        re.I,
    )
    for c in cases:
        if c["job"] != "J8":
            continue
        text = c["input"]["text"]
        assert not same_col.search(text), (c["id"], text)
        tags = c["tags"]
        if not any(t.startswith("scenario:j8-ratio-") for t in tags):
            continue
        sql = c["expected"]["tool_calls"][0]["args"]["sql"]
        sums = re.findall(r'SUM\("([^"]+)"\)', sql)
        if len(sums) >= 2:
            assert sums[0] != sums[1], (c["id"], sql)
            for row in c["expected"].get("sql_result") or []:
                if row.get("ratio") is not None and sums[0] == sums[1]:
                    assert abs(float(row["ratio"]) - 1.0) > 1e-9, (c["id"], row)


def test_escalate_inputs_not_suffix_variants(cases: list[dict]) -> None:
    """Gap 2: escalate texts are real paraphrases, not base+suffix padding."""
    texts = [c["input"]["text"] for c in cases if c["expected"]["escalation"] == "escalate"]
    assert len(texts) >= 40
    for i, a in enumerate(texts):
        for j, b in enumerate(texts):
            if i == j:
                continue
            if len(b) > len(a) and b.startswith(a):
                raise AssertionError(f"suffix variant: {a!r} -> {b!r}")


def test_j9_mostly_avoids_a1_in_text(cases: list[dict]) -> None:
    """Gap 4: ≤20% of J9 inputs mention an A1 cell range."""
    a1 = re.compile(r"\b[A-Z]{1,3}\d+:[A-Z]{1,3}\d+\b")
    j9 = [c for c in cases if c["job"] == "J9"]
    assert j9
    mentioned = [c for c in j9 if a1.search(c["input"]["text"])]
    assert len(mentioned) / len(j9) <= 0.20, (len(mentioned), len(j9))
    for c in j9:
        if a1.search(c["input"]["text"]):
            continue
        low = c["input"]["text"].lower()
        assert any(w in low for w in ("selected", "highlighted", "these")), c["id"]


def test_question_template_diversity(cases: list[dict]) -> None:
    """Gap 5: ≤3 cases share the same skeleton after blanking sheet/column/value names."""
    labels = {
        "sales",
        "expenses",
        "invoices",
        "inventory",
        "timesheets",
        "pipeline",
        "support",
        "payroll",
        "campaigns",
        "subscriptions",
    }
    cols = {
        "date",
        "region",
        "product",
        "units",
        "revenue",
        "category",
        "vendor",
        "amount",
        "currency",
        "invoice_id",
        "customer",
        "issued",
        "due",
        "total",
        "sku",
        "name",
        "qty",
        "unit_cost",
        "warehouse",
        "week",
        "person",
        "project",
        "hours",
        "billable",
        "deal",
        "stage",
        "owner",
        "close_date",
        "ticket",
        "priority",
        "status",
        "opened",
        "csat",
        "employee",
        "dept",
        "gross",
        "tax",
        "net",
        "campaign",
        "channel",
        "spend",
        "leads",
        "conversions",
        "account",
        "plan",
        "mrr",
        "started",
    }

    def skeleton(text: str) -> str:
        t = text
        for lab in sorted(labels, key=len, reverse=True):
            t = re.sub(rf"\b{re.escape(lab)}\b", "<L>", t, flags=re.I)
        for col in sorted(cols, key=len, reverse=True):
            t = re.sub(rf"\b{re.escape(col)}\b", "<C>", t, flags=re.I)
        t = re.sub(r"\bSKU-\d+\b", "<V>", t)
        t = re.sub(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+\b", "<V>", t)  # person/vendor names
        t = re.sub(r"\b(?:EMEA|AMER|APAC|AMS|NYC|SFO|P-\d+|eng|sales|ops|email|ads|social|pro|basic|active|won|lead|high|open|closed|travel|software)\b", "<V>", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip().lower()
        return t

    counts = Counter(skeleton(c["input"]["text"]) for c in cases if c["job"] == "J8")
    offenders = {k: n for k, n in counts.items() if n > 3}
    assert not offenders, offenders


def test_escalate_payload_privacy(cases: list[dict]) -> None:
    for c in cases:
        if c["expected"]["escalation"] != "escalate":
            continue
        for call in c["expected"]["tool_calls"]:
            if call["name"] != "escalate":
                continue
            payload = call["args"].get("payload_description", "")
            assert payload
            assert not PRIVACY_BAD.search(payload), (c["id"], payload)
            assert "raw rows" not in payload.lower() or "omitted" in payload.lower()


def test_sql_exec_matches_loader(cases: list[dict]) -> None:
    cache: dict[str, object] = {}
    for c in cases:
        if c["expected"].get("match") != "sql_exec":
            continue
        ref = c["context"]["fixtures"][0]
        if ref not in cache:
            cache[ref] = load_workbook_to_duckdb(ref)
        con = cache[ref]
        sql = c["expected"]["tool_calls"][0]["args"]["sql"]
        got = run_sql(con, sql)  # type: ignore[arg-type]
        expected = normalize_sql_result(c["expected"]["sql_result"])
        assert got == expected, (c["id"], sql, got, expected)


def test_build_twice_identical_bytes(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    sys.path.insert(0, str(SHEET))
    try:
        import build as sheet_build  # type: ignore

        cases_a = sheet_build.build_cases()
        sheet_build.write_outputs(cases_a, out_a)
        cases_b = sheet_build.build_cases()
        sheet_build.write_outputs(cases_b, out_b)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    for name in ("dev.jsonl", "test.jsonl"):
        a = (out_a / name).read_bytes()
        b = (out_b / name).read_bytes()
        assert a == b
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_checked_in_files_match_builder(tmp_path: Path) -> None:
    sys.path.insert(0, str(SHEET))
    try:
        import build as sheet_build  # type: ignore

        cases = sheet_build.build_cases()
        sheet_build.write_outputs(cases, tmp_path)
    finally:
        sys.path.pop(0)
        sys.modules.pop("build", None)

    assert (tmp_path / "dev.jsonl").read_bytes() == DEV.read_bytes()
    assert (tmp_path / "test.jsonl").read_bytes() == TEST.read_bytes()
