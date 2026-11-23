//! `query_sheet` execution: load → optional range → SQL guard → rows.

use donna_core::Untrusted;
use serde::Serialize;
use serde_json::{json, Value};
use std::path::Path;

use super::guard::assert_readonly_select;
use super::loader::{apply_row_range, load_workbook};
use super::profile::duckdb_value_json;
use super::range::parse_a1_row_bounds;
use crate::ToolError;

const MAX_ROWS: usize = 200;

#[derive(Debug, Clone, Serialize)]
pub struct QuerySheetResult {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Untrusted<String>>>,
    pub row_count: usize,
    pub truncated: bool,
}

impl QuerySheetResult {
    pub fn to_json(&self) -> Value {
        // Untrusted serializes transparently; also expose plain cell values.
        json!({
            "columns": self.columns,
            "rows": self.rows.iter().map(|r| {
                r.iter()
                    .map(|c| Value::String(c.as_display().clone()))
                    .collect::<Vec<_>>()
            }).collect::<Vec<_>>(),
            "row_count": self.row_count,
            "truncated": self.truncated,
        })
    }
}

/// Execute a read-only SQL query against a workbook path.
pub fn execute_query(
    path: &Path,
    sql: &str,
    range: Option<&str>,
) -> Result<QuerySheetResult, ToolError> {
    assert_readonly_select(sql)?;
    let loaded = load_workbook(path)?;
    if let Some(r) = range {
        let (lo, hi) = parse_a1_row_bounds(r)?;
        apply_row_range(&loaded.conn, &loaded.table_names, lo, hi)?;
    }

    let mut stmt = loaded
        .conn
        .prepare(sql)
        .map_err(|e| ToolError::Other(format!("duckdb prepare: {e}")))?;
    let mut rows_iter = stmt
        .query([])
        .map_err(|e| ToolError::Other(format!("duckdb execute: {e}")))?;
    let columns = rows_iter
        .as_ref()
        .map(|s| s.column_names())
        .unwrap_or_default();
    let col_count = columns.len();

    let mut rows: Vec<Vec<Untrusted<String>>> = Vec::new();
    let mut truncated = false;
    while let Some(row) = rows_iter
        .next()
        .map_err(|e| ToolError::Other(format!("duckdb row: {e}")))?
    {
        if rows.len() >= MAX_ROWS {
            truncated = true;
            break;
        }
        let mut cells = Vec::with_capacity(col_count);
        for i in 0..col_count {
            let v: duckdb::types::Value = row
                .get_ref(i)
                .map_err(|e| ToolError::Other(e.to_string()))?
                .to_owned();
            let json_v = duckdb_value_json(v);
            let s = match json_v {
                Value::Null => String::new(),
                Value::String(s) => s,
                other => other.to_string(),
            };
            cells.push(Untrusted::new(s));
        }
        rows.push(cells);
    }

    let row_count = rows.len();
    Ok(QuerySheetResult {
        columns,
        rows,
        row_count,
        truncated,
    })
}

/// Convenience: run query and return JSON tool output value.
pub fn query_sheet(path: &Path, sql: &str, range: Option<&str>) -> Result<Value, ToolError> {
    Ok(execute_query(path, sql, range)?.to_json())
}

/// Run SQL and return typed JSON row objects (for parity / golden tests).
pub fn run_sql_rows(path: &Path, sql: &str, range: Option<&str>) -> Result<Vec<Value>, ToolError> {
    assert_readonly_select(sql)?;
    let loaded = load_workbook(path)?;
    if let Some(r) = range {
        let (lo, hi) = parse_a1_row_bounds(r)?;
        apply_row_range(&loaded.conn, &loaded.table_names, lo, hi)?;
    }
    let mut stmt = loaded
        .conn
        .prepare(sql)
        .map_err(|e| ToolError::Other(format!("duckdb prepare: {e}")))?;
    let mut rows_iter = stmt
        .query([])
        .map_err(|e| ToolError::Other(format!("duckdb execute: {e}")))?;
    let columns = rows_iter
        .as_ref()
        .map(|s| s.column_names())
        .unwrap_or_default();
    let mut out = Vec::new();
    while let Some(row) = rows_iter
        .next()
        .map_err(|e| ToolError::Other(format!("duckdb row: {e}")))?
    {
        let mut map = serde_json::Map::new();
        for (i, name) in columns.iter().enumerate() {
            let v: duckdb::types::Value = row
                .get_ref(i)
                .map_err(|e| ToolError::Other(e.to_string()))?
                .to_owned();
            map.insert(name.clone(), duckdb_value_json(v));
        }
        out.push(Value::Object(map));
    }
    Ok(out)
}
