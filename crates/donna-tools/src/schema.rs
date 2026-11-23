//! Embed evals tool JSON Schemas and validate args before tool bodies run.

use serde_json::Value;
use std::collections::HashMap;
use std::sync::LazyLock;

use crate::{ToolError, ToolName};

macro_rules! tool_schema {
    ($file:literal) => {
        include_str!(concat!("../../../evals/spec/tools/", $file))
    };
}

static SCHEMAS: LazyLock<HashMap<&'static str, Value>> = LazyLock::new(|| {
    let pairs: [(&str, &str); 10] = [
        ("search_email", tool_schema!("search_email.json")),
        ("get_thread", tool_schema!("get_thread.json")),
        ("draft_email", tool_schema!("draft_email.json")),
        ("list_events", tool_schema!("list_events.json")),
        ("find_free_slot", tool_schema!("find_free_slot.json")),
        ("create_event", tool_schema!("create_event.json")),
        ("update_event", tool_schema!("update_event.json")),
        ("cancel_event", tool_schema!("cancel_event.json")),
        ("query_sheet", tool_schema!("query_sheet.json")),
        ("escalate", tool_schema!("escalate.json")),
    ];
    pairs
        .into_iter()
        .map(|(name, raw)| {
            let v: Value =
                serde_json::from_str(raw).unwrap_or_else(|e| panic!("parse schema {name}: {e}"));
            (name, v)
        })
        .collect()
});

static VALIDATORS: LazyLock<HashMap<&'static str, jsonschema::Validator>> = LazyLock::new(|| {
    SCHEMAS
        .iter()
        .map(|(name, schema)| {
            let validator = jsonschema::validator_for(schema)
                .unwrap_or_else(|e| panic!("compile schema {name}: {e}"));
            (*name, validator)
        })
        .collect()
});

pub fn schema_for(name: &str) -> Option<&'static Value> {
    SCHEMAS.get(name)
}

pub fn schema_for_tool(name: ToolName) -> Value {
    schema_for(name.as_str())
        .cloned()
        .unwrap_or_else(|| Value::Object(Default::default()))
}

pub fn validate_args(name: &str, args: &Value) -> Result<(), ToolError> {
    let validator = VALIDATORS
        .get(name)
        .ok_or_else(|| ToolError::InvalidArgs(format!("unknown tool: {name}")))?;
    if let Err(err) = validator.validate(args) {
        return Err(ToolError::InvalidArgs(err.to_string()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn examples_pass_and_invalid_fail() {
        for (name, schema) in SCHEMAS.iter() {
            if let Some(examples) = schema.get("examples").and_then(|e| e.as_array()) {
                for ex in examples {
                    validate_args(name, ex)
                        .unwrap_or_else(|e| panic!("{name} example should pass: {e} args={ex}"));
                }
            }
            let bad = json!({"__not_a_real_prop": true});
            assert!(
                matches!(validate_args(name, &bad), Err(ToolError::InvalidArgs(_))),
                "{name} should reject unknown property"
            );
        }
    }
}
