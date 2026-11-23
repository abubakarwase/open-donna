//! A1-style range → Excel `_row` bounds.

use crate::ToolError;

/// Parse an A1 range like `E5:E40` or `B2:D10` into inclusive Excel row bounds.
/// Single-cell `A1` yields `(1, 1)`.
pub fn parse_a1_row_bounds(range: &str) -> Result<(i64, i64), ToolError> {
    let s = range.trim();
    if s.is_empty() {
        return Err(ToolError::InvalidArgs("empty range".into()));
    }
    let (left, right) = match s.split_once(':') {
        Some((a, b)) => (a.trim(), b.trim()),
        None => (s, s),
    };
    let r1 = cell_row(left)?;
    let r2 = cell_row(right)?;
    Ok((r1.min(r2), r1.max(r2)))
}

fn cell_row(cell: &str) -> Result<i64, ToolError> {
    let bytes = cell.as_bytes();
    let mut i = 0;
    while i < bytes.len() && bytes[i].is_ascii_alphabetic() {
        i += 1;
    }
    if i == 0 || i == bytes.len() {
        return Err(ToolError::InvalidArgs(format!("bad A1 cell: {cell}")));
    }
    let row: i64 = std::str::from_utf8(&bytes[i..])
        .ok()
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| ToolError::InvalidArgs(format!("bad A1 row: {cell}")))?;
    if row < 1 {
        return Err(ToolError::InvalidArgs(format!("row must be >= 1: {cell}")));
    }
    Ok(row)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_j9_example() {
        assert_eq!(parse_a1_row_bounds("E5:E40").unwrap(), (5, 40));
        assert_eq!(parse_a1_row_bounds("B2:B50").unwrap(), (2, 50));
        assert_eq!(parse_a1_row_bounds("A1").unwrap(), (1, 1));
    }
}
