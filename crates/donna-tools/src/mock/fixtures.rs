use serde::Deserialize;
use serde_json::Value;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use crate::ToolError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixtureKind {
    Mailbox,
    Calendar,
    Sheet,
}

#[derive(Debug, Deserialize)]
struct IndexEntry {
    kind: String,
    path: String,
}

static INDEX: OnceLock<HashMap<String, IndexEntry>> = OnceLock::new();

pub fn fixtures_root() -> PathBuf {
    if let Ok(dir) = std::env::var("DONNA_FIXTURES_DIR") {
        return PathBuf::from(dir);
    }
    let from_manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../evals/fixtures");
    if from_manifest.is_dir() {
        return from_manifest;
    }
    PathBuf::from("evals/fixtures")
}

fn index() -> &'static HashMap<String, IndexEntry> {
    INDEX.get_or_init(|| {
        let path = fixtures_root().join("index.json");
        let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
            panic!("read fixture index {}: {e}", path.display());
        });
        serde_json::from_str(&text).unwrap_or_else(|e| {
            panic!("parse fixture index: {e}");
        })
    })
}

pub fn resolve_path(ref_name: &str) -> Result<(FixtureKind, PathBuf), ToolError> {
    let entry = index()
        .get(ref_name)
        .ok_or_else(|| ToolError::Other(format!("unknown fixture ref: {ref_name}")))?;
    let kind = match entry.kind.as_str() {
        "mailbox" => FixtureKind::Mailbox,
        "calendar" => FixtureKind::Calendar,
        "sheet" => FixtureKind::Sheet,
        other => {
            return Err(ToolError::Other(format!(
                "unsupported fixture kind {other} for {ref_name}"
            )))
        }
    };
    Ok((kind, fixtures_root().join(&entry.path)))
}

pub fn load_json(ref_name: &str) -> Result<Value, ToolError> {
    let (kind, path) = resolve_path(ref_name)?;
    if kind == FixtureKind::Sheet {
        return Err(ToolError::Other(format!(
            "ref {ref_name} is a sheet; use path()"
        )));
    }
    let text = std::fs::read_to_string(&path)
        .map_err(|e| ToolError::Other(format!("read {}: {e}", path.display())))?;
    serde_json::from_str(&text)
        .map_err(|e| ToolError::Other(format!("parse {}: {e}", path.display())))
}

pub fn load_mailbox(ref_name: &str) -> Result<Value, ToolError> {
    let (kind, _) = resolve_path(ref_name)?;
    if kind != FixtureKind::Mailbox {
        return Err(ToolError::Other(format!("{ref_name} is not a mailbox")));
    }
    load_json(ref_name)
}

pub fn load_calendar(ref_name: &str) -> Result<Value, ToolError> {
    let (kind, _) = resolve_path(ref_name)?;
    if kind != FixtureKind::Calendar {
        return Err(ToolError::Other(format!("{ref_name} is not a calendar")));
    }
    load_json(ref_name)
}

pub fn sheet_path(ref_name: &str) -> Result<PathBuf, ToolError> {
    let (kind, path) = resolve_path(ref_name)?;
    if kind != FixtureKind::Sheet {
        return Err(ToolError::Other(format!("{ref_name} is not a sheet")));
    }
    if !path.is_file() {
        return Err(ToolError::Other(format!(
            "sheet fixture missing: {}",
            path.display()
        )));
    }
    Ok(path)
}

#[allow(dead_code)]
pub fn path_exists(p: &Path) -> bool {
    p.exists()
}
