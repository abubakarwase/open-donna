#!/usr/bin/env python3
"""Deterministic sheet golden-set generator (J8–J9 layer-1 + escalate)."""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from donna_evals.sheets import load_workbook_to_duckdb, run_sql

SEED = 42
OUT_DIR = Path(__file__).resolve().parent

TARGET_TOTAL = 200
TARGET_SPLIT = {"dev": 50, "test": 150}
TARGET_DIFFICULTY = {"easy": 80, "medium": 80, "hard": 40}
TARGET_JOB = {"J8": 100, "J9": 50, "J11": 50}

NOWS = [
    ("2026-11-21T09:00:00+01:00", "Europe/Amsterdam"),
    ("2026-11-21T10:30:00+01:00", "Europe/Amsterdam"),
    ("2026-11-21T14:00:00+01:00", "Europe/Amsterdam"),
    ("2026-11-21T16:45:00+01:00", "Europe/Amsterdam"),
    ("2026-11-21T08:00:00-05:00", "America/New_York"),
    ("2026-11-21T11:15:00-05:00", "America/New_York"),
    ("2026-11-21T15:30:00-05:00", "America/New_York"),
    ("2026-11-21T18:00:00-05:00", "America/New_York"),
    ("2026-11-21T09:30:00+00:00", "UTC"),
    ("2026-11-21T13:00:00+00:00", "UTC"),
]

CLEAN = {
    "sheets/clean-01": {
        "sheet": "sales",
        "date_col": "date",
        "cols": ["date", "region", "product", "units", "revenue"],
        "filters": [("region", "EMEA"), ("region", "AMER"), ("region", "APAC"), ("product", "SKU-100")],
        "metric": "revenue",
        "metric2": "units",
        "group": "region",
        "label": "sales",
    },
    "sheets/clean-02": {
        "sheet": "expenses",
        "date_col": "date",
        "cols": ["date", "category", "vendor", "amount", "currency"],
        "filters": [("category", "travel"), ("category", "software"), ("vendor", "Acme Supplies")],
        "metric": "amount",
        "metric2": "amount",  # sole numeric; ratio becomes filter share
        "group": "category",
        "label": "expenses",
    },
    "sheets/clean-03": {
        "sheet": "invoices",
        "date_col": "issued",
        "cols": ["invoice_id", "customer", "issued", "due", "total"],
        "filters": [("customer", "Dana Okonkwo"), ("customer", "Alex Rivera")],
        "metric": "total",
        "metric2": "total",  # sole numeric; ratio becomes filter share
        "group": "customer",
        "label": "invoices",
    },
    "sheets/clean-04": {
        "sheet": "inventory",
        "date_col": None,
        "cols": ["sku", "name", "qty", "unit_cost", "warehouse"],
        "filters": [("warehouse", "AMS"), ("warehouse", "NYC"), ("warehouse", "SFO")],
        "metric": "qty",
        "metric2": "unit_cost",
        "group": "warehouse",
        "label": "inventory",
    },
    "sheets/clean-05": {
        "sheet": "timesheets",
        "date_col": None,
        "cols": ["week", "person", "project", "hours", "billable"],
        "filters": [("person", "Dana Okonkwo"), ("project", "P-1"), ("billable", True)],
        "metric": "hours",
        "metric2": "hours",  # sole numeric; ratio becomes filter share
        "group": "person",
        "label": "timesheets",
    },
    "sheets/clean-06": {
        "sheet": "pipeline",
        "date_col": "close_date",
        "cols": ["deal", "stage", "owner", "amount", "close_date"],
        "filters": [("stage", "won"), ("stage", "lead"), ("owner", "Sam Patel")],
        "metric": "amount",
        "metric2": "amount",  # sole numeric; ratio becomes filter share
        "group": "stage",
        "label": "pipeline",
    },
    "sheets/clean-07": {
        "sheet": "support",
        "date_col": "opened",
        "cols": ["ticket", "priority", "status", "opened", "csat"],
        "filters": [("priority", "high"), ("status", "open"), ("status", "closed")],
        "metric": "csat",
        "metric2": "csat",  # sole numeric; ratio becomes filter share
        "group": "priority",
        "label": "support",
    },
    "sheets/clean-08": {
        "sheet": "payroll",
        "date_col": None,
        "cols": ["employee", "dept", "gross", "tax", "net"],
        "filters": [("dept", "eng"), ("dept", "sales"), ("dept", "ops")],
        "metric": "net",
        "metric2": "gross",
        "group": "dept",
        "label": "payroll",
    },
    "sheets/clean-09": {
        "sheet": "campaigns",
        "date_col": None,
        "cols": ["campaign", "channel", "spend", "leads", "conversions"],
        "filters": [("channel", "email"), ("channel", "ads"), ("channel", "social")],
        "metric": "conversions",
        "metric2": "leads",
        "group": "channel",
        "label": "campaigns",
    },
    "sheets/clean-10": {
        "sheet": "subscriptions",
        "date_col": "started",
        "cols": ["account", "plan", "mrr", "started", "status"],
        "filters": [("plan", "pro"), ("status", "active"), ("plan", "basic")],
        "metric": "mrr",
        "metric2": "mrr",  # sole numeric; ratio becomes filter share
        "group": "plan",
        "label": "subscriptions",
    },
}

MESSY = [
    ("sheets/messy-01", "club_budget", "messy_sheet", "merged headers and totals in the club budget sheet"),
    ("sheets/messy-02", "school_fees", "messy_sheet", "merged multi-header school fees sheet"),
    ("sheets/messy-03", "grant_awards", "messy_sheet", "grant awards sheet with merged cells"),
    ("sheets/messy-04", "rental_ledger", "messy_sheet", "rental ledger with blank spacers and mixed types"),
    ("sheets/messy-05", "event_rsvp", "messy_sheet", "event RSVP sheet with blank rows and mixed types"),
    ("sheets/messy-06", "volunteer_hours", "messy_sheet", "volunteer hours with blank spacers"),
    ("sheets/messy-07", "bake_sales", "cross_sheet_join", "join bake_sales with bakers"),
    ("sheets/messy-08", "pet_visits", "cross_sheet_join", "join pet_visits with owners"),
    ("sheets/messy-09", "garden_plots", "messy_sheet", "garden plots with merged cells and mixed types"),
    ("sheets/messy-10", "donation_log", "messy_sheet", "donation log with merged cells and blanks"),
]

_TEXT_SEEN: set[str] = set()
_CONN_CACHE: dict[str, Any] = {}


@dataclass
class Family:
    key: str
    route: str
    job: str
    difficulty: str
    texts: list[str]
    fixtures: list[str]
    tags: list[str]
    tool_calls: list[dict[str, Any]]
    match: str
    escalation: str = "local"
    escalate_reason: str | None = None
    now: str = "2026-11-21T09:00:00+01:00"
    timezone: str = "Europe/Amsterdam"
    split: str = ""
    selection: dict[str, Any] | None = None
    sql_result: list[dict[str, Any]] | None = None


def _n_cases(fams: list[Family]) -> int:
    return sum(len(f.texts) for f in fams)


def _tc(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args}


def _clock(key: str) -> tuple[str, str]:
    return NOWS[sum(map(ord, key)) % len(NOWS)]


def _local_now(now: str, timezone: str) -> datetime:
    return datetime.fromisoformat(now).astimezone(ZoneInfo(timezone))


def last_month_bounds(now: str, timezone: str) -> tuple[str, str]:
    local = _local_now(now, timezone)
    first_this = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev = first_this - timedelta(days=1)
    start = prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.date().isoformat(), first_this.date().isoformat()


def _typo(s: str) -> str:
    return (
        s.replace(" the ", " teh ", 1)
        .replace("please ", "plase ", 1)
        .replace("total", "totaI")
        .replace("average", "averege")
        .replace("sheet", "sheeet")
    )


def _non_native(s: str) -> str:
    return f"Please can you help me: {s[0].lower()}{s[1:]}" if s else s


def _uniq(texts: list[str]) -> list[str]:
    out: list[str] = []
    for t in texts:
        t = t.strip()
        if not t or t in _TEXT_SEEN:
            continue
        _TEXT_SEEN.add(t)
        out.append(t)
    return out


def _phrases(formal: str, casual: str, terse: str, *, with_typo: bool = True) -> list[str]:
    texts = [formal, casual, terse, _non_native(formal)]
    if with_typo:
        texts.append(_typo(casual))
    return texts


def _sql_lit(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _col_letter(idx: int) -> str:
    """0-based column index → Excel letter (0=A)."""
    n = idx + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _conn(ref: str):
    if ref not in _CONN_CACHE:
        _CONN_CACHE[ref] = load_workbook_to_duckdb(ref)
    return _CONN_CACHE[ref]


def _exec(ref: str, sql: str) -> list[dict[str, Any]]:
    return run_sql(_conn(ref), sql)


def _trim_families(
    fams: list[Family],
    target: int,
    *,
    group_key: Any | None = None,
) -> list[Family]:
    """Trim to ``target`` cases.

    When ``group_key`` is set, whole-family drops come from the largest group
    first (so sparse op buckets like sum/topn/minmax are not wiped by
    alphabetical ``pop``).
    """
    fams = sorted(fams, key=lambda f: f.key)
    while _n_cases(fams) > target:
        # Drop last paraphrase from the largest family with >1 text, else drop family.
        candidates = [f for f in fams if len(f.texts) > 1]
        if candidates:
            fam = max(candidates, key=lambda f: (len(f.texts), f.key))
            dropped = fam.texts.pop()
            _TEXT_SEEN.discard(dropped)
        elif group_key is not None:
            counts: dict[str, int] = {}
            for f in fams:
                g = group_key(f)
                counts[g] = counts.get(g, 0) + 1
            max_c = max(counts.values())
            # Prefer groups with extras; fall back to the current largest group.
            droppable = [f for f in fams if counts[group_key(f)] > 1]
            if not droppable:
                droppable = [f for f in fams if counts[group_key(f)] == max_c]
            fam = max(droppable, key=lambda f: (counts[group_key(f)], f.key))
            fams.remove(fam)
            for t in fam.texts:
                _TEXT_SEEN.discard(t)
        else:
            fam = fams.pop()
            for t in fam.texts:
                _TEXT_SEEN.discard(t)
    assert _n_cases(fams) == target, _n_cases(fams)
    return fams


def _j8_op(key: str) -> str:
    """Map family key ``j8-<op>-...`` to op bucket (longest prefix wins)."""
    rest = key[3:] if key.startswith("j8-") else key
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


def _pad_families(fams: list[Family], target: int, maker) -> list[Family]:
    i = 0
    while _n_cases(fams) < target:
        extra = maker(i)
        i += 1
        if extra is None:
            raise RuntimeError("pad maker exhausted")
        if isinstance(extra, list):
            fams.extend(extra)
        else:
            fams.append(extra)
    return _trim_families(fams, target)


# ---------------------------------------------------------------------------
# J8 local
# ---------------------------------------------------------------------------


def _j8_families() -> list[Family]:
    fams: list[Family] = []

    def add(
        key: str,
        difficulty: str,
        texts: list[str],
        fixture: str,
        sql: str,
        sheet: str,
        tags: list[str],
    ) -> None:
        texts = _uniq(texts)
        if not texts:
            return
        now, tz = _clock(key)
        result = _exec(fixture, sql)
        fams.append(
            Family(
                key=key,
                route="sheet",
                job="J8",
                difficulty=difficulty,
                texts=texts,
                fixtures=[fixture],
                tags=sorted(set(tags + ["j8", f"scenario:{key}"])),
                tool_calls=[_tc("query_sheet", {"sql": sql, "sheet": sheet})],
                match="sql_exec",
                now=now,
                timezone=tz,
                sql_result=result,
            )
        )

    def _pick(templates: list[str], idx: int) -> list[str]:
        return [templates[idx % len(templates)]]

    for ref, meta in CLEAN.items():
        sheet = meta["sheet"]
        label = meta["label"]
        metric = meta["metric"]
        metric2 = meta["metric2"]
        group = meta["group"]
        nn = ref.split("-")[-1]
        ni = int(nn) - 1

        # SUM
        sql = f'SELECT SUM("{metric}") AS total FROM "{sheet}"'
        add(
            f"j8-sum-{nn}",
            "easy",
            _pick(
                [
                    f"What is the total {metric} in the open {label} sheet?",
                    f"Can you sum up {metric} on the {label} sheet for me?",
                    f"Add together every {metric} value on {label}.",
                    f"Please total the {metric} column for {label}.",
                    f"How much {metric} is there altogether on {label}?",
                ],
                ni,
            ),
            ref,
            sql,
            sheet,
            ["sql", "sum"],
        )

        # AVG
        sql = f'SELECT AVG("{metric}") AS avg_val FROM "{sheet}"'
        add(
            f"j8-avg-{nn}",
            "easy",
            _pick(
                [
                    f"What is the average {metric} across the open {label} sheet?",
                    f"Average {metric} on {label}, please.",
                    f"Mean {metric} for the {label} workbook?",
                    f"What's the typical {metric} value on {label}?",
                    f"Compute the avg {metric} over {label}.",
                ],
                ni,
            ),
            ref,
            sql,
            sheet,
            ["sql", "avg"],
        )

        # COUNT
        sql = f'SELECT COUNT(*) AS n FROM "{sheet}"'
        add(
            f"j8-count-{nn}",
            "easy",
            _pick(
                [
                    f"How many rows are in the open {label} sheet?",
                    f"What's the size of the open {label} table?",
                    f"Tell me the entry count on {label}.",
                    f"Give me a headcount of rows in {label}.",
                    f"How many records does {label} hold?",
                ],
                ni,
            ),
            ref,
            sql,
            sheet,
            ["sql", "count"],
        )

        # MIN / MAX
        sql = f'SELECT MIN("{metric}") AS min_val, MAX("{metric}") AS max_val FROM "{sheet}"'
        add(
            f"j8-minmax-{nn}",
            "medium",
            _pick(
                [
                    f"What are the minimum and maximum {metric} values on the {label} sheet?",
                    f"Min and max {metric} for {label}, please.",
                    f"Show the lowest and highest {metric} on {label}.",
                    f"Range of {metric} on {label}: smallest and largest?",
                    f"Bound {label} {metric} with its min and max.",
                ],
                ni,
            ),
            ref,
            sql,
            sheet,
            ["sql", "minmax"],
        )

        # GROUP BY
        sql = (
            f'SELECT "{group}", SUM("{metric}") AS total FROM "{sheet}" '
            f'GROUP BY "{group}" ORDER BY "{group}"'
        )
        add(
            f"j8-group-{nn}",
            "medium",
            _pick(
                [
                    f"Sum {metric} by {group} on the open {label} sheet.",
                    f"Break down {metric} by {group} for {label}.",
                    f"Group {label} by {group} and total the {metric}.",
                    f"For each {group}, what is total {metric} on {label}?",
                    f"{label} {metric} totals split by {group}.",
                ],
                ni,
            ),
            ref,
            sql,
            sheet,
            ["sql", "groupby"],
        )

        # Top-N
        sql = f'SELECT * FROM "{sheet}" ORDER BY "{metric}" DESC LIMIT 5'
        add(
            f"j8-topn-{nn}",
            "medium",
            _pick(
                [
                    f"Show the top 5 rows by {metric} on the {label} sheet.",
                    f"Top five {label} rows ranked by {metric}.",
                    f"Which 5 {label} rows lead on {metric}?",
                    f"List the five highest-{metric} rows in {label}.",
                    f"Give me {label}'s best five by {metric}.",
                ],
                ni,
            ),
            ref,
            sql,
            sheet,
            ["sql", "topn"],
        )

        # Filtered lookups
        for fi, (fcol, fval) in enumerate(meta["filters"]):
            where = f'"{fcol}" = {_sql_lit(fval)}'
            sql = f'SELECT SUM("{metric}") AS total FROM "{sheet}" WHERE {where}'
            disp = str(fval)
            add(
                f"j8-filter-{nn}-{fi}",
                "easy" if fi == 0 else "medium",
                _pick(
                    [
                        f"What is the total {metric} on the {label} sheet where {fcol} is {disp}?",
                        f"Sum {metric} for {fcol}={disp} on {label}.",
                        f"On {label}, total {metric} restricted to {fcol} {disp}.",
                        f"For {disp} in {fcol}, what is {label} {metric} summed?",
                        f"Add up {label} {metric} only where {fcol} equals {disp}.",
                    ],
                    ni + fi * 2,
                ),
                ref,
                sql,
                sheet,
                ["sql", "filter"],
            )

            sql = f'SELECT COUNT(*) AS n FROM "{sheet}" WHERE {where}'
            add(
                f"j8-filtercount-{nn}-{fi}",
                "easy",
                _pick(
                    [
                        f"How many {label} rows have {fcol} equal to {disp}?",
                        f"Count {label} rows where {fcol} is {disp}.",
                        f"Row count on {label} for {fcol}={disp}?",
                        f"How often does {disp} appear under {fcol} on {label}?",
                        f"Number of {label} entries with {fcol} set to {disp}.",
                    ],
                    ni + fi * 2,
                ),
                ref,
                sql,
                sheet,
                ["sql", "filter", "count"],
            )

        # Percentage / ratio (two distinct columns, else share of a filter)
        if metric != metric2:
            sql = (
                f'SELECT SUM("{metric}") AS num, SUM("{metric2}") AS den, '
                f'CASE WHEN SUM("{metric2}") = 0 THEN NULL ELSE '
                f'SUM("{metric}") * 1.0 / SUM("{metric2}") END AS ratio '
                f'FROM "{sheet}"'
            )
            add(
                f"j8-ratio-{nn}",
                "hard",
                _pick(
                    [
                        f"What is the ratio of total {metric} to total {metric2} on the {label} sheet?",
                        f"Give me {metric} over {metric2} for {label}.",
                        f"On {label}, divide summed {metric} by summed {metric2}.",
                        f"How does total {metric} compare to total {metric2} as a ratio on {label}?",
                        f"{label}: {metric} as a fraction of {metric2}.",
                    ],
                    ni,
                ),
                ref,
                sql,
                sheet,
                ["sql", "ratio"],
            )
        else:
            fcol, fval = meta["filters"][0]
            sql = (
                f'SELECT COUNT(*) FILTER (WHERE "{fcol}" = {_sql_lit(fval)}) * 1.0 '
                f'/ COUNT(*) AS share FROM "{sheet}"'
            )
            disp = str(fval)
            add(
                f"j8-ratio-{nn}",
                "hard",
                _pick(
                    [
                        f"What share of {label} rows have {fcol} equal to {disp}?",
                        f"Percentage of {label} where {fcol} is {disp}.",
                        f"What fraction of {label} is {disp} under {fcol}?",
                        f"On {label}, how large is the {disp} slice of {fcol}?",
                        f"{label}: percent of rows with {fcol}={disp}.",
                    ],
                    ni,
                ),
                ref,
                sql,
                sheet,
                ["sql", "ratio", "share"],
            )

        # Last-month date filter when available
        if meta["date_col"]:
            now, tz = _clock(f"j8-lastmonth-{nn}")
            start, end = last_month_bounds(now, tz)
            dcol = meta["date_col"]
            sql = (
                f'SELECT SUM("{metric}") AS total FROM "{sheet}" '
                f'WHERE "{dcol}" >= {_sql_lit(start)} AND "{dcol}" < {_sql_lit(end)}'
            )
            result = _exec(ref, sql)
            texts = _uniq(
                _pick(
                    [
                        f"What was total {metric} last month on the open {label} sheet?",
                        f"Sum last month's {metric} for {label}.",
                        f"Last month's {label} {metric} total?",
                        f"How much {metric} did {label} post last month?",
                        f"Total {metric} on {label} during the previous month.",
                    ],
                    ni,
                )
            )
            if texts:
                fams.append(
                    Family(
                        key=f"j8-lastmonth-{nn}",
                        route="sheet",
                        job="J8",
                        difficulty="hard",
                        texts=texts,
                        fixtures=[ref],
                        tags=sorted(set(["j8", "sql", "last_month", f"scenario:j8-lastmonth-{nn}"])),
                        tool_calls=[_tc("query_sheet", {"sql": sql, "sheet": sheet})],
                        match="sql_exec",
                        now=now,
                        timezone=tz,
                        sql_result=result,
                    )
                )

        # Simple lookup by first filter equality returning rows
        fcol, fval = meta["filters"][0]
        sql = (
            f'SELECT "{meta["cols"][0]}", "{metric}" FROM "{sheet}" '
            f'WHERE "{fcol}" = {_sql_lit(fval)} ORDER BY "{metric}" DESC LIMIT 3'
        )
        add(
            f"j8-lookup-{nn}",
            "medium",
            _pick(
                [
                    f"List the top 3 {label} entries for {fcol} {fval} by {metric}.",
                    f"Show three highest-{metric} {label} rows where {fcol} is {fval}.",
                    f"Top 3 {label} rows for {fval}, ordered by {metric}.",
                    f"Which three {label} lines lead for {fcol}={fval} on {metric}?",
                    f"Pull the best three {metric} rows on {label} for {fval}.",
                ],
                ni,
            ),
            ref,
            sql,
            sheet,
            ["sql", "lookup"],
        )

    return _trim_families(
        fams, TARGET_JOB["J8"], group_key=lambda f: _j8_op(f.key)
    )


# ---------------------------------------------------------------------------
# J9 local (selected range)
# ---------------------------------------------------------------------------


def _j9_families() -> list[Family]:
    fams: list[Family] = []

    def add_range(
        key: str,
        difficulty: str,
        texts: list[str],
        fixture: str,
        sheet: str,
        col_name: str,
        col_idx: int,
        start_row: int,
        end_row: int,
        agg: str,
        alias: str,
        tags: list[str],
    ) -> None:
        texts = _uniq(texts)
        if not texts:
            return
        letter = _col_letter(col_idx)
        a1 = f"{letter}{start_row}:{letter}{end_row}"
        sql = (
            f'SELECT {agg}("{col_name}") AS {alias} FROM "{sheet}" '
            f'WHERE _row BETWEEN {start_row} AND {end_row}'
        )
        now, tz = _clock(key)
        result = _exec(fixture, sql)
        fams.append(
            Family(
                key=key,
                route="sheet",
                job="J9",
                difficulty=difficulty,
                texts=texts,
                fixtures=[fixture],
                tags=sorted(set(tags + ["j9", "selection", f"scenario:{key}"])),
                tool_calls=[
                    _tc(
                        "query_sheet",
                        {"sql": sql, "sheet": sheet, "range": a1},
                    )
                ],
                match="sql_exec",
                now=now,
                timezone=tz,
                selection={"kind": "sheet_range", "sheet": sheet, "range": a1},
                sql_result=result,
            )
        )

    # Five ops × 10 sheets = 50 families with 1 text each initially; expand then trim.
    ops = [
        ("SUM", "total", "sum", "easy", "Sum", "total"),
        ("AVG", "avg_val", "avg", "easy", "Average", "average"),
        ("COUNT", "n", "count", "easy", "Count", "count of"),
        ("MIN", "min_val", "min", "medium", "Minimum", "min"),
        ("MAX", "max_val", "max", "medium", "Maximum", "max"),
    ]

    for ref, meta in CLEAN.items():
        sheet = meta["sheet"]
        label = meta["label"]
        nn = ref.split("-")[-1]
        metric = meta["metric"]
        col_idx = meta["cols"].index(metric)
        # Vary ranges across data rows (header excluded). nrows vary by sheet.
        ranges = [
            (2, 20),
            (5, 40),
            (10, 35),
            (2, 15),
            (15, 45),
        ]
        for oi, (agg, alias, tag, diff, word, word2) in enumerate(ops):
            start_row, end_row = ranges[oi]
            letter = _col_letter(col_idx)
            a1 = f"{letter}{start_row}:{letter}{end_row}"
            # Most prompts talk about the selection; A1 stays on selection/args only.
            # Keep a small minority that name the range explicitly (≤20% after trim).
            mention_a1 = int(nn) <= 2 and oi == 0
            if mention_a1:
                formal = f"{word} the selected {metric} cells on the {label} sheet ({a1})."
            else:
                formal = f"{word} the selected {metric} cells on the {label} sheet."
            casual = f"Can you {word2} the highlighted {metric} cells on {label}?"
            terse = f"{word} these {metric} cells on {label}."
            add_range(
                f"j9-{tag}-{nn}",
                diff,
                _phrases(formal, casual, terse, with_typo=oi == 0 and not mention_a1),
                ref,
                sheet,
                metric,
                col_idx,
                start_row,
                end_row,
                agg,
                alias,
                ["sql", tag],
            )

    # Extra multi-column block averages for hard cases if we need padding later
    return _trim_families(fams, TARGET_JOB["J9"])


# ---------------------------------------------------------------------------
# Escalate
# ---------------------------------------------------------------------------


def _escalate_families() -> list[Family]:
    fams: list[Family] = []

    def add(
        key: str,
        difficulty: str,
        texts: list[str],
        fixture: str,
        reason: str,
        task: str,
        payload: str,
        tags: list[str],
    ) -> None:
        texts = _uniq(texts)
        if not texts:
            return
        now, tz = _clock(key)
        fams.append(
            Family(
                key=key,
                route="escalate",
                job="J11",
                difficulty=difficulty,
                texts=texts,
                fixtures=[fixture],
                tags=sorted(set(tags + ["escalate", f"scenario:{key}"])),
                tool_calls=[
                    _tc(
                        "escalate",
                        {
                            "reason": reason,
                            "task": task,
                            "payload_description": payload,
                        },
                    )
                ],
                match="structured",
                escalation="escalate",
                escalate_reason=reason,
                now=now,
                timezone=tz,
            )
        )

    # Messy traits: one+ family per messy-01..10 (non-join)
    for ref, sheet, reason, desc in MESSY:
        if reason == "cross_sheet_join":
            continue
        nn = ref.split("-")[-1]
        nice = sheet.replace("_", " ")
        texts = [
            f"What is the total on this {desc}?",
            f"Please total the amounts in this messy {nice} workbook.",
            f"Can you make sense of the layout and sum the {nice} figures?",
        ]
        add(
            f"esc-messy-sheet-{nn}",
            "medium",
            texts,
            ref,
            "messy_sheet",
            f"Compute totals on messy sheet {sheet} ({desc}).",
            "Sheet profile only; raw rows omitted by default.",
            ["messy_sheet"],
        )

    # Cross-sheet joins: several distinct questions on messy-07 / messy-08
    cross_families = [
        (
            "esc-cross-bake-sum",
            "sheets/messy-07",
            [
                "Join bake sales with bakers and sum revenue by baker name.",
                "Total bake revenue per baker using both tabs.",
            ],
            "Join bake_sales with bakers and aggregate sales by baker.",
        ),
        (
            "esc-cross-bake-top",
            "sheets/messy-07",
            [
                "Which baker has the highest sales after joining bake_sales to bakers?",
                "Name the top baker by joined sales amount.",
            ],
            "Join bake_sales with bakers and find the top baker by amount.",
        ),
        (
            "esc-cross-bake-missing",
            "sheets/messy-07",
            [
                "List bake sale rows that have no matching baker.",
                "Find bake_sales entries without a baker match.",
            ],
            "Find bake_sales rows with no matching bakers row.",
        ),
        (
            "esc-cross-bake-avg",
            "sheets/messy-07",
            [
                "Average sale amount per baker after joining the two tabs.",
                "Join bakers to bake_sales and average amount by baker.",
            ],
            "Join bake_sales with bakers and average amount by baker.",
        ),
        (
            "esc-cross-pet-count",
            "sheets/messy-08",
            [
                "Join pet visits with owners and count visits per owner.",
                "How many visits does each owner have across both sheets?",
            ],
            "Join pet_visits with owners and count visits per owner.",
        ),
        (
            "esc-cross-pet-top",
            "sheets/messy-08",
            [
                "Which owner has the most pet visits after the join?",
                "Top owner by visit count using pet_visits and owners.",
            ],
            "Join pet_visits with owners and find the top owner by visits.",
        ),
        (
            "esc-cross-pet-missing",
            "sheets/messy-08",
            [
                "Show pet visits that do not match any owner.",
                "Find orphan pet_visits rows with no owner.",
            ],
            "Find pet_visits rows with no matching owners row.",
        ),
        (
            "esc-cross-pet-totals",
            "sheets/messy-08",
            [
                "Sum visit fees per owner after joining pets to owners.",
                "Join owners onto pet_visits and total fees by owner.",
            ],
            "Join pet_visits with owners and sum fees by owner.",
        ),
    ]
    for key, ref, texts, task in cross_families:
        add(
            key,
            "hard",
            texts,
            ref,
            "cross_sheet_join",
            task,
            "Sheet profile for both tabs; query intent only; raw rows omitted by default.",
            ["cross_sheet_join"],
        )

    # Why / forecast on clean sheets (distinct paraphrases, no suffix padding)
    why_asks = [
        (
            "sheets/clean-01",
            "Why did EMEA revenue drop last quarter?",
            [
                "Why did EMEA revenue drop last quarter?",
                "What caused the EMEA revenue decline last quarter?",
                "Explain the fall in EMEA revenue for last quarter.",
            ],
        ),
        (
            "sheets/clean-01",
            "Forecast next month's sales revenue from this sheet.",
            [
                "Forecast next month's sales revenue from this sheet.",
                "Project sales revenue for next month using this data.",
                "What revenue should we expect next month?",
            ],
        ),
        (
            "sheets/clean-02",
            "Why are travel expenses rising?",
            [
                "Why are travel expenses rising?",
                "What is driving the increase in travel spend?",
                "Explain the upward trend in travel expenses.",
            ],
        ),
        (
            "sheets/clean-02",
            "Forecast next quarter's software spend.",
            [
                "Forecast next quarter's software spend.",
                "Project software costs for the coming quarter.",
                "How much software spend should we plan next quarter?",
            ],
        ),
        (
            "sheets/clean-06",
            "Why are deals stalling in proposal?",
            [
                "Why are deals stalling in proposal?",
                "What is blocking deals that sit in proposal?",
                "Explain the stall in the proposal stage.",
            ],
        ),
        (
            "sheets/clean-06",
            "Forecast how much pipeline will close next month.",
            [
                "Forecast how much pipeline will close next month.",
                "Project closed-won amount for next month from the pipeline.",
                "How much of this pipeline should close next month?",
            ],
        ),
        (
            "sheets/clean-09",
            "Why is email converting worse than ads?",
            [
                "Why is email converting worse than ads?",
                "What explains the weaker email conversion versus ads?",
                "Explain the email vs ads conversion gap.",
            ],
        ),
        (
            "sheets/clean-09",
            "Forecast leads for next campaign cycle.",
            [
                "Forecast leads for next campaign cycle.",
                "Project lead volume for the next campaign cycle.",
                "How many leads should the next cycle bring?",
            ],
        ),
        (
            "sheets/clean-10",
            "Why is churn increasing on basic plans?",
            [
                "Why is churn increasing on basic plans?",
                "What is causing higher churn on the basic plan?",
                "Explain the rise in basic-plan churn.",
            ],
        ),
        (
            "sheets/clean-07",
            "Forecast next week's ticket volume from this sheet.",
            [
                "Forecast next week's ticket volume from this sheet.",
                "Project support ticket volume for next week.",
                "How many tickets should we expect next week?",
            ],
        ),
    ]
    for i, (ref, task, texts) in enumerate(why_asks):
        add(
            f"esc-why-{i:02d}",
            "hard",
            texts,
            ref,
            "why_or_forecast",
            task.rstrip("?"),
            "Sheet profile and prior query results only; raw rows omitted by default.",
            ["why_or_forecast"],
        )

    # Ambiguous: real distinct phrasings (no "… Looking at the open sheet." padding)
    amb_asks = [
        (
            "sheets/clean-01",
            [
                "How are we doing?",
                "What's our standing based on this sheet?",
                "Any quick read on performance here?",
            ],
        ),
        (
            "sheets/clean-02",
            [
                "Give me a sense of the numbers.",
                "Is the expense picture okay overall?",
                "What jumps out from these figures?",
            ],
        ),
        (
            "sheets/clean-03",
            [
                "What's the overall picture here?",
                "How healthy do the invoices look?",
                "Anything worrying in this workbook?",
            ],
        ),
        (
            "sheets/clean-04",
            [
                "Are things healthy?",
                "Does inventory look fine at a glance?",
                "Should I be concerned about stock levels?",
            ],
        ),
        (
            "sheets/clean-05",
            [
                "How does this look?",
                "Is the timesheet load reasonable?",
                "Any red flags in these hours?",
            ],
        ),
        (
            "sheets/clean-06",
            [
                "Any concerns in this sheet?",
                "How is the pipeline looking overall?",
                "Worth escalating anything from this pipeline?",
            ],
        ),
        (
            "sheets/clean-08",
            [
                "Summarise how we are doing.",
                "Payroll vibes: good or concerning?",
                "What's the takeaway from this payroll sheet?",
            ],
        ),
        (
            "sheets/clean-10",
            [
                "Status check on this workbook?",
                "How are subscriptions trending overall?",
                "Anything I should notice in the subscription data?",
            ],
        ),
        (
            "sheets/clean-09",
            [
                "Is this good or bad?",
                "Are the campaigns on track?",
                "Do these campaign numbers look okay?",
            ],
        ),
        (
            "sheets/clean-07",
            [
                "What should I pay attention to?",
                "Where should I look first in support?",
                "Highlight whatever matters most here.",
            ],
        ),
    ]
    for i, (ref, texts) in enumerate(amb_asks):
        add(
            f"esc-amb-{i:02d}",
            "medium",
            texts,
            ref,
            "ambiguous",
            "Clarify the metric and range for an ambiguous sheet question.",
            "Sheet profile only; raw rows omitted by default.",
            ["ambiguous"],
        )

    return _trim_families(
        fams,
        TARGET_JOB["J11"],
        group_key=lambda f: f.escalate_reason or "other",
    )


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def _rebalance_difficulty(families: list[Family]) -> None:
    """Force difficulty counts to ~80/80/40 by re-labeling families (stable by key)."""

    def count() -> dict[str, int]:
        c = {"easy": 0, "medium": 0, "hard": 0}
        for fam in families:
            c[fam.difficulty] += len(fam.texts)
        return c

    def move(src: str, dst: str, need: int) -> None:
        if need <= 0:
            return
        for fam in sorted(families, key=lambda f: f.key):
            if need <= 0:
                return
            if fam.difficulty != src:
                continue
            # Only move whole families that fit the remaining need.
            w = len(fam.texts)
            if w <= need:
                fam.difficulty = dst
                need -= w

    c = count()
    # Drain surplus into deficit buckets.
    for _ in range(6):
        c = count()
        if (
            abs(c["easy"] - TARGET_DIFFICULTY["easy"]) <= 5
            and abs(c["medium"] - TARGET_DIFFICULTY["medium"]) <= 5
            and abs(c["hard"] - TARGET_DIFFICULTY["hard"]) <= 5
        ):
            break
        if c["hard"] < TARGET_DIFFICULTY["hard"]:
            need = TARGET_DIFFICULTY["hard"] - c["hard"]
            if c["easy"] > TARGET_DIFFICULTY["easy"]:
                move("easy", "hard", min(need, c["easy"] - TARGET_DIFFICULTY["easy"]))
            c = count()
            need = TARGET_DIFFICULTY["hard"] - c["hard"]
            if need > 0 and c["medium"] > TARGET_DIFFICULTY["medium"]:
                move("medium", "hard", min(need, c["medium"] - TARGET_DIFFICULTY["medium"]))
            c = count()
            need = TARGET_DIFFICULTY["hard"] - c["hard"]
            if need > 0:
                move("medium", "hard", need)
        c = count()
        if c["easy"] < TARGET_DIFFICULTY["easy"]:
            need = TARGET_DIFFICULTY["easy"] - c["easy"]
            if c["medium"] > TARGET_DIFFICULTY["medium"]:
                move("medium", "easy", min(need, c["medium"] - TARGET_DIFFICULTY["medium"]))
            c = count()
            need = TARGET_DIFFICULTY["easy"] - c["easy"]
            if need > 0 and c["hard"] > TARGET_DIFFICULTY["hard"]:
                move("hard", "easy", min(need, c["hard"] - TARGET_DIFFICULTY["hard"]))
        c = count()
        if c["medium"] < TARGET_DIFFICULTY["medium"]:
            need = TARGET_DIFFICULTY["medium"] - c["medium"]
            if c["easy"] > TARGET_DIFFICULTY["easy"]:
                move("easy", "medium", min(need, c["easy"] - TARGET_DIFFICULTY["easy"]))
            c = count()
            need = TARGET_DIFFICULTY["medium"] - c["medium"]
            if need > 0 and c["hard"] > TARGET_DIFFICULTY["hard"]:
                move("hard", "medium", min(need, c["hard"] - TARGET_DIFFICULTY["hard"]))
        c = count()
        if c["easy"] > TARGET_DIFFICULTY["easy"] + 5:
            move("easy", "medium", c["easy"] - TARGET_DIFFICULTY["easy"])
        c = count()
        if c["medium"] > TARGET_DIFFICULTY["medium"] + 5:
            move("medium", "easy", c["medium"] - TARGET_DIFFICULTY["medium"])

    c = count()
    if abs(c["easy"] - 80) > 5 or abs(c["hard"] - 40) > 5 or abs(c["medium"] - 80) > 5:
        raise RuntimeError(f"difficulty rebalance failed: {c}")


def _assign_splits(families: list[Family]) -> None:
    rng = random.Random(SEED)
    preferred: set[str] = set()
    by_bucket: dict[tuple[str, str, str], list[Family]] = {}
    for fam in families:
        by_bucket.setdefault((fam.route, fam.difficulty, fam.escalation), []).append(fam)

    for bucket in sorted(by_bucket):
        group = sorted(by_bucket[bucket], key=lambda f: f.key)
        order = list(range(len(group)))
        rng.shuffle(order)
        group = [group[i] for i in order]
        n_cases = sum(len(f.texts) for f in group)
        target_dev = max(1, round(n_cases * 0.25)) if n_cases >= 4 else (1 if n_cases > 1 else 0)
        running = 0
        for fam in group:
            if running < target_dev:
                preferred.add(fam.key)
                running += len(fam.texts)

    target = TARGET_SPLIT["dev"]
    items = sorted(families, key=lambda f: f.key)
    n = len(items)
    NEG = -10**9
    dp = [[NEG] * (target + 1) for _ in range(n + 1)]
    take = [[False] * (target + 1) for _ in range(n + 1)]
    dp[0][0] = 0
    for i, fam in enumerate(items):
        w = len(fam.texts)
        pref = 1 if fam.key in preferred else 0
        for s in range(target + 1):
            dp[i + 1][s] = dp[i][s]
            take[i + 1][s] = False
            if s >= w and dp[i][s - w] != NEG:
                cand = dp[i][s - w] + pref
                if cand > dp[i + 1][s]:
                    dp[i + 1][s] = cand
                    take[i + 1][s] = True

    if dp[n][target] == NEG:
        raise RuntimeError(f"cannot pack split target={target}")

    chosen: set[int] = set()
    s = target
    for i in range(n, 0, -1):
        if take[i][s]:
            chosen.add(i - 1)
            s -= len(items[i - 1].texts)
    if s != 0:
        raise RuntimeError(f"split reconstruct leftover={s}")

    for i, fam in enumerate(items):
        fam.split = "dev" if i in chosen else "test"

    c_dev = sum(len(f.texts) for f in families if f.split == "dev")
    if c_dev != target:
        raise RuntimeError(f"split pack failed: dev={c_dev}")


def _case_from(fam: Family, text: str, case_id: str) -> dict[str, Any]:
    tags = list(fam.tags)
    if fam.split == "test" and "needs_human_review" not in tags:
        tags.append("needs_human_review")
    tags = sorted(set(tags))
    expected: dict[str, Any] = {
        "route": fam.route,
        "escalation": fam.escalation,
        "match": fam.match,
        "tool_calls": [dict(c) for c in fam.tool_calls],
    }
    if fam.escalation == "escalate":
        expected["escalate_reason"] = fam.escalate_reason
    if fam.sql_result is not None:
        expected["sql_result"] = fam.sql_result
    inp: dict[str, Any] = {"text": text}
    if fam.selection is not None:
        inp["selection"] = dict(fam.selection)
    return {
        "id": case_id,
        "skill": fam.route,
        "layer": 1,
        "job": fam.job,
        "split": fam.split,
        "difficulty": fam.difficulty,
        "source": "synthetic",
        "tags": tags,
        "input": inp,
        "context": {
            "now": fam.now,
            "timezone": fam.timezone,
            "fixtures": list(fam.fixtures),
        },
        "expected": expected,
    }


def _assert_quality(cases: list[dict[str, Any]]) -> None:
    banned = re.compile(r"clean-\d|messy-\d|sheets/", re.I)
    for c in cases:
        text = c["input"]["text"]
        if banned.search(text):
            raise RuntimeError(f"banned fixture label in {c['id']}: {text!r}")
    texts = [c["input"]["text"] for c in cases]
    if len(texts) != len(set(texts)):
        raise RuntimeError("duplicate input texts remain")


def build_cases() -> list[dict[str, Any]]:
    global _TEXT_SEEN, _CONN_CACHE
    _TEXT_SEEN = set()
    _CONN_CACHE = {}

    families = _j8_families() + _j9_families() + _escalate_families()
    families = sorted(families, key=lambda f: f.key)
    if _n_cases(families) != TARGET_TOTAL:
        raise RuntimeError(f"total cases {_n_cases(families)} != {TARGET_TOTAL}")
    _rebalance_difficulty(families)
    _assign_splits(families)

    expanded: list[tuple[Family, str]] = []
    for fam in families:
        for text in fam.texts:
            expanded.append((fam, text))

    job_order = {"J8": 0, "J9": 1, "J11": 2}
    expanded.sort(
        key=lambda pair: (
            0 if pair[0].split == "dev" else 1,
            job_order[pair[0].job],
            pair[0].key,
            pair[1],
        )
    )

    cases: list[dict[str, Any]] = []
    for i, (fam, text) in enumerate(expanded, start=1):
        case_id = f"sht-{i:04d}"
        cases.append(_case_from(fam, text, case_id))

    _assert_quality(cases)
    return cases


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_outputs(
    cases: list[dict[str, Any]],
    out_dir: Path | None = None,
) -> tuple[Path, Path]:
    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    cases_sorted = sorted(cases, key=lambda c: c["id"])
    dev = [c for c in cases_sorted if c["split"] == "dev"]
    test = [c for c in cases_sorted if c["split"] == "test"]
    dev_path = out / "dev.jsonl"
    test_path = out / "test.jsonl"
    dev_path.write_text("".join(_dumps(c) + "\n" for c in dev), encoding="utf-8")
    test_path.write_text("".join(_dumps(c) + "\n" for c in test), encoding="utf-8")
    return dev_path, test_path


def main() -> None:
    cases = build_cases()
    write_outputs(cases)
    jobs: dict[str, int] = {}
    splits: dict[str, int] = {}
    diffs: dict[str, int] = {}
    labels: dict[str, int] = {}
    for c in cases:
        jobs[c["job"]] = jobs.get(c["job"], 0) + 1
        splits[c["split"]] = splits.get(c["split"], 0) + 1
        diffs[c["difficulty"]] = diffs.get(c["difficulty"], 0) + 1
        lab = c["expected"]["escalation"]
        labels[lab] = labels.get(lab, 0) + 1
    print(f"wrote {len(cases)} cases jobs={jobs} splits={splits} difficulty={diffs} labels={labels}")


if __name__ == "__main__":
    main()
