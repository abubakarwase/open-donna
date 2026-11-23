"""Tests for E-04 fixture generator and loader."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from donna_evals.fixtures import load

EVALS = Path(__file__).resolve().parents[1]
FIXTURES = EVALS / "fixtures"
CASES = EVALS / "spec" / "examples" / "cases.jsonl"
MANIFEST = FIXTURES / "sheets" / "manifest.json"
BUILD_PATH = FIXTURES / "build.py"


def _build_mod():
    name = "evals_fixtures_build"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BUILD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _file_map(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


def test_build_twice_byte_identical(tmp_path: Path) -> None:
    mod = _build_mod()
    a = tmp_path / "a"
    b = tmp_path / "b"
    mod.build_all(a, seed=mod.SEED)
    mod.build_all(b, seed=mod.SEED)
    assert _file_map(a) == _file_map(b)


def test_every_index_ref_loads() -> None:
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    for ref, entry in index.items():
        loaded = load(ref)
        if entry["kind"] == "sheet":
            assert isinstance(loaded, Path)
            assert loaded.is_file()
        else:
            assert isinstance(loaded, dict)


def test_example_case_fixtures_and_ids() -> None:
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    for line in CASES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        for ref in case["context"]["fixtures"]:
            assert ref in index, f"{case['id']}: missing fixture {ref}"
            assert not ref.startswith("style/")
            loaded = load(ref)
            kind = index[ref]["kind"]
            for call in case["expected"].get("tool_calls") or []:
                args = call.get("args") or {}
                if "thread_id" in args and kind == "mailbox":
                    assert isinstance(loaded, dict)
                    ids = {t["thread_id"] for t in loaded["threads"]}
                    assert args["thread_id"] in ids, (
                        f"{case['id']}: thread {args['thread_id']} not in {ref}"
                    )
                if "event_id" in args and kind == "calendar":
                    assert isinstance(loaded, dict)
                    ids = {e["event_id"] for e in loaded["events"]}
                    assert args["event_id"] in ids, (
                        f"{case['id']}: event {args['event_id']} not in {ref}"
                    )
                if "sheet" in args and kind == "sheet":
                    assert isinstance(loaded, Path)
                    wb = load_workbook(loaded, read_only=True)
                    assert args["sheet"] in wb.sheetnames
                    wb.close()


def test_counts() -> None:
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    threads = 0
    injection = 0
    for ref, entry in index.items():
        if entry["kind"] != "mailbox":
            continue
        data = load(ref)
        assert isinstance(data, dict)
        threads += len(data["threads"])
        for t in data["threads"]:
            for m in t["messages"]:
                if "fixture:injection" in m.get("labels", []):
                    injection += 1
    assert threads >= 200
    assert injection >= 10

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    clean = [m for m in manifest if not m["messy"]]
    messy = [m for m in manifest if m["messy"]]
    assert len(clean) == 10
    assert len(messy) == 10


def test_datetimes_have_offsets() -> None:
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    for ref, entry in index.items():
        if entry["kind"] == "mailbox":
            data = load(ref)
            assert isinstance(data, dict)
            for t in data["threads"]:
                for m in t["messages"]:
                    dt = datetime.fromisoformat(m["date"])
                    assert dt.tzinfo is not None
        elif entry["kind"] == "calendar":
            data = load(ref)
            assert isinstance(data, dict)
            for ev in data["events"]:
                if ev.get("all_day"):
                    date.fromisoformat(ev["start"])
                    if "end" in ev:
                        date.fromisoformat(ev["end"])
                else:
                    s = datetime.fromisoformat(ev["start"])
                    e = datetime.fromisoformat(ev["end"])
                    assert s.tzinfo is not None and e.tzinfo is not None
            for slots in data["freebusy"].values():
                for slot in slots:
                    assert datetime.fromisoformat(slot["start"]).tzinfo is not None
                    assert datetime.fromisoformat(slot["end"]).tzinfo is not None


def test_dst_edges_both_sides() -> None:
    cal = load("calendar/dst-edge")
    assert isinstance(cal, dict)
    starts = []
    for ev in cal["events"]:
        if ev.get("all_day"):
            continue
        starts.append(datetime.fromisoformat(ev["start"]))
    ams_before = any(d.date() < date(2026, 10, 25) for d in starts)
    ams_after = any(d.date() > date(2026, 10, 25) for d in starts)
    ny_before = any(d.date() < date(2026, 11, 1) for d in starts)
    ny_after = any(d.date() > date(2026, 11, 1) for d in starts)
    assert ams_before and ams_after
    assert ny_before and ny_after


def test_unknown_ref_raises() -> None:
    with pytest.raises(KeyError):
        load("mailbox/does-not-exist")


def _triage_labels(thread: dict) -> set[str]:
    labs: set[str] = set()
    for m in thread["messages"]:
        labs.update(m.get("labels") or [])
    return {lb for lb in labs if lb.startswith("triage:")}


def _subject_pattern(subject: str) -> str:
    import re

    return re.sub(r"\d+", "", subject)


def test_mailbox_quality_per_persona() -> None:
    import re

    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    total_injection = 0
    for ref, entry in index.items():
        if entry["kind"] != "mailbox":
            continue
        data = load(ref)
        assert isinstance(data, dict)
        threads = data["threads"]
        n = len(threads)
        owner = data["owner"]["email"]

        patterns = {_subject_pattern(t["subject"]) for t in threads}
        assert len(patterns) >= 30, f"{ref}: only {len(patterns)} subject patterns"

        for t in threads:
            assert not re.search(r"\b(mail|thread)\s+\d+\b", t["subject"], re.I), t["subject"]
            for m in t["messages"]:
                sentences = [s for s in re.split(r"[.!?]+", m["body"]) if s.strip()]
                assert 2 <= len(sentences) <= 8, (t["thread_id"], len(sentences), m["body"][:80])

        senders = {m["from"] for t in threads for m in t["messages"]}
        assert len(senders) >= 15, f"{ref}: senders={len(senders)}"

        multi = sum(1 for t in threads if len(t["messages"]) >= 2)
        assert multi / n >= 0.30, f"{ref}: multi={multi}/{n}"

        long = [t for t in threads if len(t["messages"]) >= 5]
        assert len(long) >= 8, f"{ref}: long={len(long)}"
        for t in long:
            assert any(m["from"] == owner for m in t["messages"]), t["thread_id"]

        vendors: set[str] = set()
        months: set[str] = set()
        inj_threads = 0
        triage_counts = {"triage:needs_reply": 0, "triage:fyi": 0, "triage:ignore": 0}
        for t in threads:
            tl = _triage_labels(t)
            assert len(tl) == 1, (ref, t["thread_id"], tl)
            triage_counts[next(iter(tl))] += 1
            labs = {lb for m in t["messages"] for lb in (m.get("labels") or [])}
            if "fixture:injection" in labs:
                inj_threads += 1
            if "invoice" in labs:
                # vendor name before " invoice" or leading title words
                subj = t["subject"]
                if " invoice " in subj.lower() or "invoice" in subj.lower():
                    vendors.add(_subject_pattern(subj.split(" invoice")[0].split(" Invoice")[0]))
                for m in t["messages"]:
                    for mo in ("August", "September", "October", "November"):
                        if mo in m["body"] or mo in subj:
                            months.add(mo)
        assert inj_threads >= 3, f"{ref}: injection threads={inj_threads}"
        total_injection += inj_threads
        assert len(vendors) >= 4, f"{ref}: vendors={vendors}"
        assert len(months) >= 3, f"{ref}: months={months}"
        for label, count in triage_counts.items():
            assert count / n >= 0.15, f"{ref}: {label}={count}/{n}"

    assert total_injection >= 10


def test_messy_sheets_unique_domains() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    messy = [m for m in manifest if m["messy"]]
    assert len(messy) == 10
    title_sets = []
    header_sets = []
    for entry in messy:
        path = FIXTURES / entry["path"]
        wb = load_workbook(path, read_only=True)
        title_sets.append(tuple(wb.sheetnames))
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(max_row=8, values_only=True))
        # primary header row: first row whose cells are mostly strings and not a title merge alone
        header = None
        for row in rows:
            vals = [c for c in row if c is not None]
            if len(vals) >= 3 and all(isinstance(c, str) for c in vals):
                header = tuple(vals)
                break
        assert header is not None, entry["ref"]
        header_sets.append(header)
        # row count band 20-200 data-ish rows
        nrows = sum(1 for _ in ws.iter_rows(min_row=1, values_only=True))
        assert 20 <= nrows <= 220, (entry["ref"], nrows)
        wb.close()
        assert entry["traits"], entry["ref"]
    assert len(set(title_sets)) == 10
    assert len(set(header_sets)) == 10
