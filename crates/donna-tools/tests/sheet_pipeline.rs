//! C-07: loader parity vs sheets.py, golden SQL results, SQL guard, range filter.

use donna_tools::{
    assert_readonly_select, load_workbook, parse_a1_row_bounds, query_sheet, run_sql_rows,
    SheetBackend, ToolName, ToolRegistry,
};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

fn fixtures_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../evals/fixtures")
}

fn set_fixtures_env() {
    std::env::set_var("DONNA_FIXTURES_DIR", fixtures_dir());
}

fn evals_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../evals")
}

fn python_schema(ref_name: &str) -> Value {
    let code = format!(
        r#"
import json
from donna_evals.sheets import load_workbook_to_duckdb, run_sql
con = load_workbook_to_duckdb({ref_name:?})
tables = [r["table_name"] for r in run_sql(con, "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1")]
out = {{}}
for t in tables:
    cols = run_sql(con, f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name='{{t}}' ORDER BY ordinal_position")
    n = run_sql(con, f'SELECT COUNT(*) AS n FROM "{{t}}"')[0]["n"]
    out[t] = {{"columns": cols, "row_count": n}}
print(json.dumps({{"tables": tables, "detail": out}}))
con.close()
"#
    );
    let out = Command::new("uv")
        .args(["run", "python", "-c", &code])
        .current_dir(evals_dir())
        .output()
        .expect("uv run python");
    assert!(
        out.status.success(),
        "python schema failed for {ref_name}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("parse python schema")
}

fn rust_schema(path: &Path) -> Value {
    let loaded = load_workbook(path).unwrap_or_else(|e| panic!("load {}: {e}", path.display()));
    let mut detail = serde_json::Map::new();
    for t in &loaded.table_names {
        let cols: Vec<Value> = loaded
            .conn
            .prepare(
                "SELECT column_name, data_type FROM information_schema.columns \
                 WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position",
            )
            .unwrap()
            .query_map([t.as_str()], |r| {
                Ok(json!({
                    "column_name": r.get::<_, String>(0)?,
                    "data_type": r.get::<_, String>(1)?,
                }))
            })
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        let n: i64 = loaded
            .conn
            .query_row(&format!("SELECT COUNT(*) FROM \"{t}\""), [], |r| r.get(0))
            .unwrap();
        detail.insert(t.clone(), json!({ "columns": cols, "row_count": n }));
    }
    json!({ "tables": loaded.table_names, "detail": detail })
}

#[test]
fn loader_parity_all_20_fixtures() {
    set_fixtures_env();
    let manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(fixtures_dir().join("sheets/manifest.json")).unwrap(),
    )
    .unwrap();
    let entries = manifest.as_array().unwrap();
    assert_eq!(entries.len(), 20);

    for entry in entries {
        let ref_name = entry["ref"].as_str().unwrap();
        let path = fixtures_dir().join(entry["path"].as_str().unwrap());
        let py = python_schema(ref_name);
        let rs = rust_schema(&path);
        let mut py_tables: Vec<&str> = py["tables"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t.as_str().unwrap())
            .collect();
        let mut rs_tables: Vec<&str> = rs["tables"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t.as_str().unwrap())
            .collect();
        py_tables.sort();
        rs_tables.sort();
        assert_eq!(py_tables, rs_tables, "{ref_name} table names mismatch");
        for t in py["tables"].as_array().unwrap() {
            let name = t.as_str().unwrap();
            let py_d = &py["detail"][name];
            let rs_d = &rs["detail"][name];
            assert_eq!(
                py_d["row_count"], rs_d["row_count"],
                "{ref_name}/{name} row_count"
            );
            let py_cols: Vec<&str> = py_d["columns"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c["column_name"].as_str().unwrap())
                .collect();
            let rs_cols: Vec<&str> = rs_d["columns"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c["column_name"].as_str().unwrap())
                .collect();
            assert_eq!(py_cols, rs_cols, "{ref_name}/{name} column names");
            let py_types: Vec<&str> = py_d["columns"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c["data_type"].as_str().unwrap())
                .collect();
            let rs_types: Vec<&str> = rs_d["columns"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c["data_type"].as_str().unwrap())
                .collect();
            assert_eq!(
                py_types, rs_types,
                "{ref_name}/{name} column types py={py_types:?} rs={rs_types:?}"
            );
        }
    }
}

fn floats_close(a: &Value, b: &Value) -> bool {
    match (a.as_f64(), b.as_f64()) {
        (Some(af), Some(bf)) => {
            if af == bf {
                return true;
            }
            let denom = af.abs().max(bf.abs()).max(1e-12);
            (af - bf).abs() / denom <= 1e-6
        }
        _ => a == b,
    }
}

fn normalize_val(v: &Value) -> Value {
    match v {
        Value::Number(n) => {
            if let Some(f) = n.as_f64() {
                let r = (f * 1_000_000.0).round() / 1_000_000.0;
                json!(r)
            } else {
                v.clone()
            }
        }
        other => other.clone(),
    }
}

fn rows_equal(a: &[Value], b: &[Value]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    for (ra, rb) in a.iter().zip(b.iter()) {
        let va: Vec<Value> = ra
            .as_object()
            .unwrap()
            .values()
            .map(normalize_val)
            .collect();
        let vb: Vec<Value> = rb
            .as_object()
            .unwrap()
            .values()
            .map(normalize_val)
            .collect();
        if va.len() != vb.len() {
            return false;
        }
        for (x, y) in va.iter().zip(vb.iter()) {
            if !floats_close(x, y) && x != y {
                return false;
            }
        }
    }
    true
}

#[test]
fn golden_dev_sql_results_match() {
    set_fixtures_env();
    let path = evals_dir().join("golden/sheet/dev.jsonl");
    let text = std::fs::read_to_string(&path).unwrap();
    let mut checked = 0usize;
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let case: Value = serde_json::from_str(line).unwrap();
        let expected = &case["expected"];
        let sql_result = match expected.get("sql_result") {
            Some(v) if !v.is_null() => v,
            _ => continue,
        };
        let call = expected["tool_calls"]
            .as_array()
            .and_then(|a| a.iter().find(|c| c["name"] == "query_sheet"))
            .expect("query_sheet call");
        let sql = call["args"]["sql"].as_str().unwrap();
        let range = call["args"].get("range").and_then(|v| v.as_str());
        let fix = case["context"]["fixtures"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|v| v.as_str())
            .find(|s| s.starts_with("sheets/"))
            .unwrap();
        let wb = donna_tools::MockSheet.path(fix).unwrap();
        let got = run_sql_rows(&wb, sql, range).unwrap_or_else(|e| panic!("{}: {e}", case["id"]));
        let exp = sql_result.as_array().unwrap();
        assert!(
            rows_equal(exp, &got),
            "{} sql mismatch\nexpected={exp:?}\ngot={got:?}",
            case["id"]
        );
        checked += 1;
    }
    assert!(
        checked >= 20,
        "expected many sql_result cases, got {checked}"
    );
}

#[test]
fn sql_guard_rejects_non_select() {
    assert!(assert_readonly_select("SELECT 1").is_ok());
    assert!(assert_readonly_select("INSERT INTO t VALUES (1)").is_err());
    assert!(assert_readonly_select("DELETE FROM t").is_err());
    assert!(assert_readonly_select("SELECT 1; DROP TABLE t").is_err());
}

#[test]
fn range_filter_limits_rows() {
    set_fixtures_env();
    let path = donna_tools::MockSheet.path("sheets/clean-01").unwrap();
    let (lo, hi) = parse_a1_row_bounds("E5:E40").unwrap();
    assert_eq!((lo, hi), (5, 40));

    let all = run_sql_rows(&path, "SELECT COUNT(*) AS n FROM \"sales\"", None).unwrap();
    let ranged =
        run_sql_rows(&path, "SELECT COUNT(*) AS n FROM \"sales\"", Some("E5:E40")).unwrap();
    let n_all = all[0]["n"].as_i64().unwrap();
    let n_range = ranged[0]["n"].as_i64().unwrap();
    assert!(n_all > n_range, "all={n_all} range={n_range}");
    assert!(n_range > 0);

    // Tool path also works.
    let _ = query_sheet(
        &path,
        "SELECT AVG(\"revenue\") AS avg_val FROM \"sales\" WHERE _row BETWEEN 5 AND 40",
        Some("E5:E40"),
    )
    .unwrap();
}

#[test]
fn query_sheet_succeeds_on_clean_fixture() {
    set_fixtures_env();
    let mut reg = ToolRegistry::new();
    reg.sheet_fixtures.push("sheets/clean-01".into());
    let out = reg
        .call(
            ToolName::QuerySheet,
            json!({
                "sql": "SELECT AVG(\"revenue\") AS avg_val FROM \"sales\"",
                "sheet": "sales"
            }),
        )
        .expect("query_sheet should succeed");
    assert!(out.value.get("rows").is_some());
    assert_eq!(out.value["row_count"], 1);
}

#[test]
fn messy_detection_merged_cells() {
    set_fixtures_env();
    let clean = load_workbook(&donna_tools::MockSheet.path("sheets/clean-01").unwrap()).unwrap();
    assert!(!clean.messy, "clean-01 should not be messy");
    let messy = load_workbook(&donna_tools::MockSheet.path("sheets/messy-10").unwrap()).unwrap();
    assert!(messy.messy, "messy-10 should be messy (merged cells)");
}
