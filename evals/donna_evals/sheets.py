"""Load clean sheet workbooks into an in-memory DuckDB connection."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import openpyxl

from donna_evals.fixtures import load as load_fixture


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_float(v: Any) -> bool:
    return isinstance(v, float) or isinstance(v, Decimal)


def _infer_duck_type(values: list[Any]) -> str:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "VARCHAR"
    if all(isinstance(v, bool) for v in non_null):
        return "BOOLEAN"
    if all(_is_int(v) for v in non_null):
        return "BIGINT"
    if all(_is_int(v) or _is_float(v) for v in non_null):
        return "DOUBLE"
    if all(isinstance(v, datetime) for v in non_null):
        return "TIMESTAMP"
    if all(isinstance(v, (date, datetime)) for v in non_null):
        return "DATE"
    return "VARCHAR"


def _cell_value(v: Any, duck_type: str) -> Any:
    if v is None:
        return None
    if duck_type == "DOUBLE":
        return float(v)
    if duck_type == "BIGINT":
        return int(v)
    if duck_type == "BOOLEAN":
        return bool(v)
    if duck_type in ("DATE", "TIMESTAMP"):
        if isinstance(v, datetime):
            return v
        if isinstance(v, date):
            return v
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def load_workbook_to_duckdb(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Load an .xlsx into DuckDB: one table per worksheet named after the sheet.

    Row 1 is headers. Remaining rows are data. An integer ``_row`` column holds
    the Excel row number (header is 1; first data row is 2).
    ``path`` may be a filesystem Path or a fixture ref such as ``sheets/clean-01``.
    """
    if isinstance(path, str) and not Path(path).is_file():
        resolved = load_fixture(path)
        if not isinstance(resolved, Path):
            raise TypeError(f"expected sheet Path for {path!r}, got {type(resolved)}")
        path = resolved
    else:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"sheet workbook missing: {path}")

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    con = duckdb.connect(":memory:")
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
            data_rows = rows[1:]
            col_types: list[str] = []
            for i, _h in enumerate(headers):
                col_types.append(_infer_duck_type([r[i] if i < len(r) else None for r in data_rows]))

            cols_sql = ", ".join(
                f'"{h}" {t}' for h, t in zip(headers, col_types, strict=True)
            )
            con.execute(
                f'CREATE TABLE "{sheet_name}" (_row BIGINT, {cols_sql})'
            )

            insert_cols = ", ".join(["_row"] + [f'"{h}"' for h in headers])
            placeholders = ", ".join(["?"] * (1 + len(headers)))
            insert_sql = (
                f'INSERT INTO "{sheet_name}" ({insert_cols}) VALUES ({placeholders})'
            )
            for excel_row, row in enumerate(data_rows, start=2):
                values: list[Any] = [excel_row]
                for i, duck_type in enumerate(col_types):
                    raw = row[i] if i < len(row) else None
                    values.append(_cell_value(raw, duck_type))
                con.execute(insert_sql, values)
    finally:
        wb.close()
    return con


def normalize_sql_value(v: Any) -> Any:
    """Stable JSON-serializable value (floats rounded to 6 decimal places)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        return round(float(v), 6)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (int, str)):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def normalize_sql_result(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {str(k): normalize_sql_value(v) for k, v in row.items()}
        for row in rows
    ]


def run_sql(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    rel = con.execute(sql)
    cols = [d[0] for d in rel.description]
    out: list[dict[str, Any]] = []
    for tup in rel.fetchall():
        out.append({cols[i]: tup[i] for i in range(len(cols))})
    return normalize_sql_result(out)
