//! Workbook → in-memory DuckDB, matching `evals/donna_evals/sheets.py`.

use calamine::{open_workbook, Data, Reader, Xlsx};
use chrono::{NaiveDate, NaiveDateTime, Timelike};
use duckdb::{params_from_iter, Connection};
use std::io::BufReader;
use std::path::Path;

use crate::ToolError;

#[derive(Debug)]
pub struct LoadedWorkbook {
    pub conn: Connection,
    pub table_names: Vec<String>,
    pub messy: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DuckType {
    Boolean,
    Bigint,
    Double,
    Timestamp,
    Date,
    Varchar,
}

impl DuckType {
    fn sql(self) -> &'static str {
        match self {
            Self::Boolean => "BOOLEAN",
            Self::Bigint => "BIGINT",
            Self::Double => "DOUBLE",
            Self::Timestamp => "TIMESTAMP",
            Self::Date => "DATE",
            Self::Varchar => "VARCHAR",
        }
    }
}

#[derive(Debug, Clone)]
enum CellVal {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Timestamp(NaiveDateTime),
    Date(NaiveDate),
    Text(String),
}

fn data_to_cell(d: &Data) -> CellVal {
    match d {
        Data::Empty => CellVal::Null,
        Data::Bool(b) => CellVal::Bool(*b),
        Data::Int(i) => CellVal::Int(*i),
        Data::Float(f) => CellVal::Float(*f),
        Data::String(s) => CellVal::Text(s.clone()),
        Data::DateTime(dt) => {
            if let Some(ndt) = dt.as_datetime() {
                if ndt.time().num_seconds_from_midnight() == 0 && ndt.nanosecond() == 0 {
                    CellVal::Date(ndt.date())
                } else {
                    CellVal::Timestamp(ndt)
                }
            } else {
                CellVal::Text(format!("{dt:?}"))
            }
        }
        Data::DateTimeIso(s) => {
            if let Ok(ndt) = NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f") {
                CellVal::Timestamp(ndt)
            } else if let Ok(ndt) = NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f") {
                CellVal::Timestamp(ndt)
            } else if let Ok(d) = NaiveDate::parse_from_str(s, "%Y-%m-%d") {
                CellVal::Date(d)
            } else {
                CellVal::Text(s.clone())
            }
        }
        Data::DurationIso(s) => CellVal::Text(s.clone()),
        Data::Error(e) => CellVal::Text(format!("{e:?}")),
    }
}

fn infer_duck_type(values: &[CellVal]) -> DuckType {
    let non_null: Vec<&CellVal> = values
        .iter()
        .filter(|v| !matches!(v, CellVal::Null))
        .collect();
    if non_null.is_empty() {
        return DuckType::Varchar;
    }
    if non_null.iter().all(|v| matches!(v, CellVal::Bool(_))) {
        return DuckType::Boolean;
    }
    // Calamine usually emits Float for Excel numbers; treat whole floats as BIGINT
    // so we match openpyxl int columns in sheets.py.
    if non_null.iter().all(|v| match v {
        CellVal::Int(_) => true,
        CellVal::Float(f) => f.fract() == 0.0,
        _ => false,
    }) {
        return DuckType::Bigint;
    }
    if non_null
        .iter()
        .all(|v| matches!(v, CellVal::Int(_) | CellVal::Float(_)))
    {
        return DuckType::Double;
    }
    if non_null.iter().all(|v| matches!(v, CellVal::Timestamp(_))) {
        return DuckType::Timestamp;
    }
    if non_null
        .iter()
        .all(|v| matches!(v, CellVal::Date(_) | CellVal::Timestamp(_)))
    {
        return DuckType::Date;
    }
    DuckType::Varchar
}

fn format_cell(v: &CellVal) -> String {
    match v {
        CellVal::Null => String::new(),
        CellVal::Bool(b) => b.to_string(),
        CellVal::Int(i) => i.to_string(),
        CellVal::Float(f) => {
            if f.fract() == 0.0 {
                format!("{f:.0}")
            } else {
                f.to_string()
            }
        }
        CellVal::Timestamp(t) => t.format("%Y-%m-%dT%H:%M:%S%.f").to_string(),
        CellVal::Date(d) => d.to_string(),
        CellVal::Text(s) => s.clone(),
    }
}

fn cell_for_insert(v: &CellVal, duck_type: DuckType) -> CellVal {
    match (v, duck_type) {
        (CellVal::Null, _) => CellVal::Null,
        (_, DuckType::Double) => match v {
            CellVal::Int(i) => CellVal::Float(*i as f64),
            CellVal::Float(f) => CellVal::Float(*f),
            other => CellVal::Text(format_cell(other)),
        },
        (_, DuckType::Bigint) => match v {
            CellVal::Int(i) => CellVal::Int(*i),
            CellVal::Float(f) => CellVal::Int(*f as i64),
            CellVal::Bool(b) => CellVal::Int(i64::from(*b)),
            other => CellVal::Text(format_cell(other)),
        },
        (_, DuckType::Boolean) => match v {
            CellVal::Bool(b) => CellVal::Bool(*b),
            other => CellVal::Text(format_cell(other)),
        },
        (_, DuckType::Timestamp) => match v {
            CellVal::Timestamp(t) => CellVal::Timestamp(*t),
            CellVal::Date(d) => CellVal::Timestamp(d.and_hms_opt(0, 0, 0).unwrap()),
            other => CellVal::Text(format_cell(other)),
        },
        (_, DuckType::Date) => match v {
            CellVal::Date(d) => CellVal::Date(*d),
            CellVal::Timestamp(t) => CellVal::Date(t.date()),
            other => CellVal::Text(format_cell(other)),
        },
        (_, DuckType::Varchar) => match v {
            CellVal::Text(s) => CellVal::Text(s.clone()),
            CellVal::Date(d) => CellVal::Text(d.to_string()),
            CellVal::Timestamp(t) => CellVal::Text(t.format("%Y-%m-%dT%H:%M:%S%.f").to_string()),
            other => CellVal::Text(format_cell(other)),
        },
    }
}

pub(crate) fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn header_name(cell: &CellVal, idx: usize) -> String {
    match cell {
        CellVal::Null => format!("col_{idx}"),
        CellVal::Text(s) if s.is_empty() => format!("col_{idx}"),
        other => {
            let s = format_cell(other);
            if s.is_empty() {
                format!("col_{idx}")
            } else {
                s
            }
        }
    }
}

fn empty_or_placeholder_headers(headers: &[String]) -> bool {
    // Empty header cells become col_{i}; aligns with multi_header / blank header traits.
    headers.iter().any(|h| h.starts_with("col_"))
}

/// Load an `.xlsx` into an in-memory DuckDB connection (one table per sheet).
pub fn load_workbook(path: &Path) -> Result<LoadedWorkbook, ToolError> {
    let mut workbook: Xlsx<BufReader<std::fs::File>> = open_workbook(path)
        .map_err(|e| ToolError::Other(format!("open workbook {}: {e}", path.display())))?;

    let conn =
        Connection::open_in_memory().map_err(|e| ToolError::Other(format!("duckdb open: {e}")))?;

    let sheet_names = workbook.sheet_names().to_vec();
    let mut table_names = Vec::new();
    let mut messy = false;

    for name in &sheet_names {
        if let Ok(merges) = workbook.merge_cells_by_sheet_name(name) {
            if !merges.is_empty() {
                messy = true;
                break;
            }
        }
    }

    for sheet_name in &sheet_names {
        let range = workbook
            .worksheet_range(sheet_name)
            .map_err(|e| ToolError::Other(format!("read sheet {sheet_name}: {e}")))?;
        if range.height() == 0 {
            continue;
        }

        let mut rows: Vec<Vec<CellVal>> = Vec::new();
        for row in range.rows() {
            rows.push(row.iter().map(data_to_cell).collect());
        }
        if rows.is_empty() {
            continue;
        }

        let header_row = &rows[0];
        let width = header_row.len();
        let headers: Vec<String> = (0..width)
            .map(|i| {
                let cell = header_row.get(i).cloned().unwrap_or(CellVal::Null);
                header_name(&cell, i)
            })
            .collect();

        let data_rows: Vec<Vec<CellVal>> = rows[1..]
            .iter()
            .map(|r| {
                (0..width)
                    .map(|i| r.get(i).cloned().unwrap_or(CellVal::Null))
                    .collect()
            })
            .collect();

        if empty_or_placeholder_headers(&headers) {
            messy = true;
        }

        let mut col_types = Vec::with_capacity(width);
        for i in 0..width {
            let col_vals: Vec<CellVal> = data_rows
                .iter()
                .map(|r| r.get(i).cloned().unwrap_or(CellVal::Null))
                .collect();
            col_types.push(infer_duck_type(&col_vals));
        }

        let cols_sql: Vec<String> = headers
            .iter()
            .zip(col_types.iter())
            .map(|(h, t)| format!("{} {}", quote_ident(h), t.sql()))
            .collect();
        let create_sql = format!(
            "CREATE TABLE {} (_row BIGINT, {})",
            quote_ident(sheet_name),
            cols_sql.join(", ")
        );
        conn.execute(&create_sql, [])
            .map_err(|e| ToolError::Other(format!("create table {sheet_name}: {e}")))?;

        let insert_cols = std::iter::once("_row".to_string())
            .chain(headers.iter().map(|h| quote_ident(h)))
            .collect::<Vec<_>>()
            .join(", ");
        let placeholders = (0..=headers.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let insert_sql = format!(
            "INSERT INTO {} ({insert_cols}) VALUES ({placeholders})",
            quote_ident(sheet_name)
        );

        for (excel_row, row) in data_rows.iter().enumerate() {
            let excel_row_num = (excel_row + 2) as i64;
            let mut bind: Vec<CellVal> = vec![CellVal::Int(excel_row_num)];
            for (i, duck_type) in col_types.iter().enumerate() {
                let raw = row.get(i).cloned().unwrap_or(CellVal::Null);
                bind.push(cell_for_insert(&raw, *duck_type));
            }
            let mut stmt = conn
                .prepare(&insert_sql)
                .map_err(|e| ToolError::Other(format!("prepare insert: {e}")))?;
            bind_and_execute(&mut stmt, &bind)?;
        }

        table_names.push(sheet_name.clone());
    }

    Ok(LoadedWorkbook {
        conn,
        table_names,
        messy,
    })
}

fn bind_and_execute(stmt: &mut duckdb::Statement<'_>, bind: &[CellVal]) -> Result<(), ToolError> {
    use duckdb::types::ToSql;
    let values: Vec<duckdb::types::Value> = bind
        .iter()
        .map(|v| match v {
            CellVal::Null => duckdb::types::Value::Null,
            CellVal::Bool(b) => duckdb::types::Value::Boolean(*b),
            CellVal::Int(i) => duckdb::types::Value::BigInt(*i),
            CellVal::Float(f) => duckdb::types::Value::Double(*f),
            CellVal::Text(s) => duckdb::types::Value::Text(s.clone()),
            CellVal::Date(d) => duckdb::types::Value::Date32(date_to_days(*d)),
            CellVal::Timestamp(t) => duckdb::types::Value::Timestamp(
                duckdb::types::TimeUnit::Microsecond,
                timestamp_to_micros(*t),
            ),
        })
        .collect();
    let refs: Vec<&dyn ToSql> = values.iter().map(|v| v as &dyn ToSql).collect();
    stmt.execute(params_from_iter(refs.iter()))
        .map_err(|e| ToolError::Other(format!("insert row: {e}")))?;
    Ok(())
}

fn date_to_days(d: NaiveDate) -> i32 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap();
    (d - epoch).num_days() as i32
}

fn timestamp_to_micros(t: NaiveDateTime) -> i64 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1)
        .unwrap()
        .and_hms_opt(0, 0, 0)
        .unwrap();
    (t - epoch).num_microseconds().unwrap_or(0)
}

/// Apply `_row` filter for an optional A1 range (inclusive Excel row bounds).
pub fn apply_row_range(
    conn: &Connection,
    table_names: &[String],
    start_row: i64,
    end_row: i64,
) -> Result<(), ToolError> {
    for name in table_names {
        let sql = format!(
            "DELETE FROM {} WHERE _row < ? OR _row > ?",
            quote_ident(name)
        );
        conn.execute(&sql, duckdb::params![start_row, end_row])
            .map_err(|e| ToolError::Other(format!("range filter: {e}")))?;
    }
    Ok(())
}
