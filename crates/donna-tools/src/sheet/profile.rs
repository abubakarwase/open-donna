//! Sheet profile JSON: tables, columns+types, row counts, sample rows, messy.

use chrono::{DateTime, NaiveDate, NaiveDateTime};
use duckdb::Connection;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::loader::{quote_ident, LoadedWorkbook};
use crate::ToolError;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TableProfile {
    pub name: String,
    pub columns: Vec<ColumnProfile>,
    pub row_count: i64,
    pub sample_rows: Vec<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ColumnProfile {
    pub name: String,
    pub data_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SheetProfile {
    pub tables: Vec<TableProfile>,
    pub messy: bool,
}

impl SheetProfile {
    pub fn to_json(&self) -> Value {
        serde_json::to_value(self).unwrap_or(json!({}))
    }
}

/// Build a profile from an already-loaded workbook.
pub fn build_profile(loaded: &LoadedWorkbook) -> Result<SheetProfile, ToolError> {
    profile_from_conn(&loaded.conn, &loaded.table_names, loaded.messy)
}

pub fn profile_from_conn(
    conn: &Connection,
    table_names: &[String],
    messy: bool,
) -> Result<SheetProfile, ToolError> {
    let mut tables = Vec::new();
    for name in table_names {
        let columns = column_profiles(conn, name)?;
        let row_count: i64 = conn
            .query_row(
                &format!("SELECT COUNT(*) FROM {}", quote_ident(name)),
                [],
                |r| r.get(0),
            )
            .map_err(|e| ToolError::Other(format!("count {name}: {e}")))?;
        let sample_rows = sample_rows(conn, name, 3)?;
        tables.push(TableProfile {
            name: name.clone(),
            columns,
            row_count,
            sample_rows,
        });
    }
    Ok(SheetProfile { tables, messy })
}

fn column_profiles(conn: &Connection, table: &str) -> Result<Vec<ColumnProfile>, ToolError> {
    let mut stmt = conn
        .prepare(
            "SELECT column_name, data_type FROM information_schema.columns \
             WHERE table_schema = 'main' AND table_name = ? \
             ORDER BY ordinal_position",
        )
        .map_err(|e| ToolError::Other(e.to_string()))?;
    let rows = stmt
        .query_map([table], |r| {
            Ok(ColumnProfile {
                name: r.get::<_, String>(0)?,
                data_type: r.get::<_, String>(1)?,
            })
        })
        .map_err(|e| ToolError::Other(e.to_string()))?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| ToolError::Other(e.to_string()))?);
    }
    Ok(out)
}

fn sample_rows(conn: &Connection, table: &str, n: usize) -> Result<Vec<Value>, ToolError> {
    let sql = format!("SELECT * FROM {} LIMIT {n}", quote_ident(table));
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| ToolError::Other(e.to_string()))?;
    let mut rows = stmt
        .query([])
        .map_err(|e| ToolError::Other(e.to_string()))?;
    let names = rows.as_ref().map(|s| s.column_names()).unwrap_or_default();
    let mut out = Vec::new();
    while let Some(row) = rows.next().map_err(|e| ToolError::Other(e.to_string()))? {
        let mut map = serde_json::Map::new();
        for (i, name) in names.iter().enumerate() {
            map.insert(name.clone(), duck_value_to_json(row, i)?);
        }
        out.push(Value::Object(map));
    }
    Ok(out)
}

pub(crate) fn duck_value_to_json(row: &duckdb::Row<'_>, idx: usize) -> Result<Value, ToolError> {
    let v: duckdb::types::Value = row
        .get_ref(idx)
        .map_err(|e| ToolError::Other(e.to_string()))?
        .to_owned();
    Ok(duckdb_value_json(v))
}

pub(crate) fn duckdb_value_json(v: duckdb::types::Value) -> Value {
    use duckdb::types::Value as DV;
    match v {
        DV::Null => Value::Null,
        DV::Boolean(b) => Value::Bool(b),
        DV::TinyInt(i) => json!(i),
        DV::SmallInt(i) => json!(i),
        DV::Int(i) => json!(i),
        DV::BigInt(i) => json!(i),
        DV::HugeInt(i) => {
            // Prefer JSON number when it fits i64 (SUM of BIGINT often yields HugeInt).
            if i >= i128::from(i64::MIN) && i <= i128::from(i64::MAX) {
                json!(i as i64)
            } else {
                json!(i.to_string())
            }
        }
        DV::UTinyInt(i) => json!(i),
        DV::USmallInt(i) => json!(i),
        DV::UInt(i) => json!(i),
        DV::UBigInt(i) => json!(i),
        DV::Float(f) => json!(f as f64),
        DV::Double(f) => json!(f),
        DV::Decimal(d) => json!(d.to_string()),
        DV::Text(s) => Value::String(s),
        DV::Blob(b) => Value::String(format!("blob:{}b", b.len())),
        DV::Date32(d) => {
            let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap();
            Value::String((epoch + chrono::Duration::days(d as i64)).to_string())
        }
        DV::Timestamp(_, us) => {
            let secs = us / 1_000_000;
            let nanos = ((us % 1_000_000) * 1000) as u32;
            let dt = DateTime::from_timestamp(secs, nanos)
                .map(|d| d.naive_utc())
                .unwrap_or_else(NaiveDateTime::default);
            Value::String(dt.format("%Y-%m-%dT%H:%M:%S%.f").to_string())
        }
        other => Value::String(format!("{other:?}")),
    }
}
