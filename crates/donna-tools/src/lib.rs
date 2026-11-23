//! Tool trait, arg validation, mock connectors, and the 10 eval tool surfaces.

mod backend;
mod mock;
mod schema;
mod sheet;
mod tools;

pub use backend::{
    CalendarBackend, Event, FreeBusyBlock, FreeBusyMap, MailBackend, MailMessage, MessageSummary,
    SearchFilter, SheetBackend, Thread,
};
pub use mock::{
    earliest_slot, find_free_slot, fixtures_root, format_datetime, merge_busy, MockCalendar,
    MockMail, MockSheet,
};
pub use schema::{schema_for, schema_for_tool, validate_args};
pub use sheet::{
    assert_readonly_select, build_profile, execute_query, load_workbook, parse_a1_row_bounds,
    query_sheet, run_sql_rows, LoadedWorkbook, QuerySheetResult, SheetProfile, TableProfile,
};
pub use tools::{all_tools, NamedTool, ToolRegistry};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

/// The 10 tool names from `evals/spec/tools/index.json`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolName {
    SearchEmail,
    GetThread,
    DraftEmail,
    ListEvents,
    FindFreeSlot,
    CreateEvent,
    UpdateEvent,
    CancelEvent,
    QuerySheet,
    Escalate,
}

impl ToolName {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SearchEmail => "search_email",
            Self::GetThread => "get_thread",
            Self::DraftEmail => "draft_email",
            Self::ListEvents => "list_events",
            Self::FindFreeSlot => "find_free_slot",
            Self::CreateEvent => "create_event",
            Self::UpdateEvent => "update_event",
            Self::CancelEvent => "cancel_event",
            Self::QuerySheet => "query_sheet",
            Self::Escalate => "escalate",
        }
    }

    pub fn from_str_name(s: &str) -> Option<Self> {
        match s {
            "search_email" => Some(Self::SearchEmail),
            "get_thread" => Some(Self::GetThread),
            "draft_email" => Some(Self::DraftEmail),
            "list_events" => Some(Self::ListEvents),
            "find_free_slot" => Some(Self::FindFreeSlot),
            "create_event" => Some(Self::CreateEvent),
            "update_event" => Some(Self::UpdateEvent),
            "cancel_event" => Some(Self::CancelEvent),
            "query_sheet" => Some(Self::QuerySheet),
            "escalate" => Some(Self::Escalate),
            _ => None,
        }
    }
}

/// Tool execution context: fixtures refs and timezone from the eval case.
#[derive(Debug, Clone, Default)]
pub struct ToolCtx {
    pub timezone: String,
    pub fixtures: Vec<String>,
}

/// Tool output value (JSON). Third-party strings inside are `Untrusted` at the typed boundary.
#[derive(Debug, Clone)]
pub struct ToolOutput {
    pub value: Value,
}

#[derive(Debug, Error)]
pub enum ToolError {
    #[error("invalid args: {0}")]
    InvalidArgs(String),
    #[error("not implemented: {0}")]
    NotImplemented(String),
    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, ToolError>;

/// Tool surface. Side-effect execution goes through `ToolRegistry::execute(..., &ApprovalToken)`.
pub trait Tool {
    fn name(&self) -> ToolName;
    fn args_schema(&self) -> Value {
        schema_for_tool(self.name())
    }
    fn is_side_effect(&self) -> bool;
    fn call(&self, args: Value, ctx: &ToolCtx) -> Result<ToolOutput>;
}
