//! Read-only SQL guard: single SELECT / WITH only.

use crate::ToolError;

/// Reject anything that is not a single SELECT or WITH…SELECT statement.
pub fn assert_readonly_select(sql: &str) -> Result<(), ToolError> {
    let trimmed = sql.trim();
    if trimmed.is_empty() {
        return Err(ToolError::InvalidArgs("empty sql".into()));
    }

    // Strip trailing semicolons for the single-statement check, but reject
    // multiple statements (semicolon separating non-empty parts).
    let without_trailing = trimmed.trim_end_matches(';').trim();
    if without_trailing.contains(';') {
        return Err(ToolError::InvalidArgs(
            "multi-statement SQL is not allowed".into(),
        ));
    }

    let lower = without_trailing.to_ascii_lowercase();
    let head = lower.split_whitespace().next().unwrap_or("");
    if head != "select" && head != "with" {
        return Err(ToolError::InvalidArgs(
            "only SELECT/WITH queries are allowed".into(),
        ));
    }

    // Block common write / side-effect keywords as whole words.
    const FORBIDDEN: &[&str] = &[
        "insert", "update", "delete", "drop", "alter", "create", "attach", "detach", "copy",
        "pragma", "call", "execute", "install", "load", "export", "import", "vacuum", "replace",
        "merge", "truncate", "grant", "revoke",
    ];
    for tok in tokenize_sql(&lower) {
        if FORBIDDEN.contains(&tok.as_str()) {
            return Err(ToolError::InvalidArgs(format!(
                "forbidden SQL keyword: {tok}"
            )));
        }
    }
    Ok(())
}

fn tokenize_sql(lower: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_single = false;
    let mut in_double = false;
    for ch in lower.chars() {
        if in_single {
            if ch == '\'' {
                in_single = false;
            }
            continue;
        }
        if in_double {
            if ch == '"' {
                in_double = false;
            }
            continue;
        }
        match ch {
            '\'' => in_single = true,
            '"' => in_double = true,
            c if c.is_ascii_alphabetic() || c == '_' => cur.push(c),
            _ => {
                if !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                }
            }
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allows_select_and_with() {
        assert!(assert_readonly_select("SELECT 1").is_ok());
        assert!(assert_readonly_select("  with t as (select 1) select * from t  ").is_ok());
        assert!(assert_readonly_select("SELECT AVG(\"revenue\") FROM \"sales\";").is_ok());
    }

    #[test]
    fn rejects_writes_and_multi() {
        assert!(assert_readonly_select("INSERT INTO t VALUES (1)").is_err());
        assert!(assert_readonly_select("DELETE FROM t").is_err());
        assert!(assert_readonly_select("SELECT 1; SELECT 2").is_err());
        assert!(assert_readonly_select("PRAGMA table_info('t')").is_err());
        assert!(assert_readonly_select("CREATE TABLE t (a INT)").is_err());
    }
}
