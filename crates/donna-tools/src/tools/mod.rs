//! Registry and implementations for the 10 eval tool surfaces.

use chrono::{DateTime, Duration, FixedOffset};
use donna_core::{ApprovalToken, Draft, DraftKind};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::backend::{CalendarBackend, MailBackend, SearchFilter, SheetBackend};
use crate::mock::{find_free_slot, format_datetime, MockCalendar, MockMail, MockSheet};
use crate::schema::{schema_for_tool, validate_args};
use crate::sheet::query_sheet;
use crate::{Result, Tool, ToolCtx, ToolError, ToolName, ToolOutput};

static DRAFT_SEQ: AtomicU64 = AtomicU64::new(1);

fn next_draft_id() -> String {
    let n = DRAFT_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("draft_{n}")
}

fn parse_dt(s: &str) -> Result<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(s)
        .map_err(|e| ToolError::InvalidArgs(format!("bad datetime {s}: {e}")))
}

/// Holds mock backends, pending drafts, and executed side effects.
#[derive(Debug)]
pub struct ToolRegistry {
    pub mail: Option<MockMail>,
    pub calendar: Option<MockCalendar>,
    pub sheet: MockSheet,
    /// Open sheet fixture refs from the eval context (e.g. `sheets/clean-01`).
    pub sheet_fixtures: Vec<String>,
    pub drafts: HashMap<String, Draft>,
    pub executed: Vec<Value>,
    timezone: String,
}

impl Default for ToolRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self {
            mail: None,
            calendar: None,
            sheet: MockSheet,
            sheet_fixtures: Vec::new(),
            drafts: HashMap::new(),
            executed: Vec::new(),
            timezone: "UTC".into(),
        }
    }

    pub fn with_mailbox(mut self, ref_name: &str) -> Result<Self> {
        self.mail = Some(MockMail::from_ref(ref_name)?);
        Ok(self)
    }

    pub fn with_calendar(mut self, ref_name: &str) -> Result<Self> {
        self.calendar = Some(MockCalendar::from_ref(ref_name)?);
        Ok(self)
    }

    pub fn with_timezone(mut self, tz: impl Into<String>) -> Self {
        self.timezone = tz.into();
        self
    }

    pub fn timezone(&self) -> &str {
        &self.timezone
    }

    pub fn load_from_ctx(&mut self, ctx: &ToolCtx) -> Result<()> {
        self.timezone = ctx.timezone.clone();
        self.sheet_fixtures.clear();
        for fix in &ctx.fixtures {
            if fix.starts_with("mailbox/") {
                self.mail = Some(MockMail::from_ref(fix)?);
            } else if fix.starts_with("calendar/") {
                self.calendar = Some(MockCalendar::from_ref(fix)?);
            } else if fix.starts_with("sheets/") {
                self.sheet_fixtures.push(fix.clone());
            }
        }
        Ok(())
    }

    /// Resolve a workbook path from a fixture ref or the open sheet fixtures.
    pub fn resolve_sheet_path(&self, sheet_arg: &str) -> Result<std::path::PathBuf> {
        if sheet_arg.starts_with("sheets/") || sheet_arg.ends_with(".xlsx") {
            return self.sheet.path(sheet_arg);
        }
        // Try as fixture ref `sheets/{arg}`.
        let as_ref = format!("sheets/{sheet_arg}");
        if let Ok(p) = self.sheet.path(&as_ref) {
            return Ok(p);
        }
        // Fall back to the first open sheet fixture (worksheet name is for SQL).
        let fix = self.sheet_fixtures.first().ok_or_else(|| {
            ToolError::Other("no sheet fixture loaded; pass sheets/... in context".into())
        })?;
        self.sheet.path(fix)
    }

    fn require_mail(&self) -> Result<&MockMail> {
        self.mail
            .as_ref()
            .ok_or_else(|| ToolError::Other("no mailbox fixture loaded".into()))
    }

    fn require_calendar(&self) -> Result<&MockCalendar> {
        self.calendar
            .as_ref()
            .ok_or_else(|| ToolError::Other("no calendar fixture loaded".into()))
    }

    pub fn call(&mut self, name: ToolName, args: Value) -> Result<ToolOutput> {
        validate_args(name.as_str(), &args)?;
        match name {
            ToolName::SearchEmail => self.search_email(args),
            ToolName::GetThread => self.get_thread(args),
            ToolName::DraftEmail => self.draft_side_effect(DraftKind::Email, args),
            ToolName::ListEvents => self.list_events(args),
            ToolName::FindFreeSlot => self.find_free_slot_tool(args),
            ToolName::CreateEvent => self.draft_side_effect(DraftKind::Event, args),
            ToolName::UpdateEvent => self.draft_side_effect(DraftKind::EventUpdate, args),
            ToolName::CancelEvent => self.draft_side_effect(DraftKind::EventCancel, args),
            ToolName::QuerySheet => self.query_sheet_tool(args),
            ToolName::Escalate => self.escalate(args),
        }
    }

    fn query_sheet_tool(&self, args: Value) -> Result<ToolOutput> {
        let sql = args
            .get("sql")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("sql required".into()))?;
        let sheet = args
            .get("sheet")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("sheet required".into()))?;
        let range = args.get("range").and_then(|v| v.as_str());
        let path = self.resolve_sheet_path(sheet)?;
        let value = query_sheet(&path, sql, range)?;
        Ok(ToolOutput { value })
    }

    /// Record an approved draft into `executed`. Requires `&ApprovalToken` (will not compile without it).
    pub fn execute(&mut self, draft_id: &str, _token: &ApprovalToken) -> Result<Value> {
        let draft = self
            .drafts
            .get(draft_id)
            .cloned()
            .ok_or_else(|| ToolError::Other(format!("unknown draft_id: {draft_id}")))?;
        let record = json!({
            "draft_id": draft.id,
            "kind": draft.kind,
            "payload": draft.payload,
        });
        self.executed.push(record.clone());
        Ok(record)
    }

    fn search_email(&self, args: Value) -> Result<ToolOutput> {
        let filter = SearchFilter {
            query: args
                .get("query")
                .and_then(|v| v.as_str())
                .map(str::to_string),
            from: args
                .get("from")
                .and_then(|v| v.as_str())
                .map(str::to_string),
            unread: args.get("unread").and_then(|v| v.as_bool()),
            after: args
                .get("after")
                .and_then(|v| v.as_str())
                .map(str::to_string),
            before: args
                .get("before")
                .and_then(|v| v.as_str())
                .map(str::to_string),
        };
        let results = self.require_mail()?.search(&filter)?;
        Ok(ToolOutput {
            value: serde_json::to_value(results).map_err(|e| ToolError::Other(e.to_string()))?,
        })
    }

    fn get_thread(&self, args: Value) -> Result<ToolOutput> {
        let tid = args
            .get("thread_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ToolError::InvalidArgs("thread_id required".into()))?;
        let thread = self.require_mail()?.thread(tid)?;
        Ok(ToolOutput {
            value: serde_json::to_value(thread).map_err(|e| ToolError::Other(e.to_string()))?,
        })
    }

    fn list_events(&self, args: Value) -> Result<ToolOutput> {
        let start = parse_dt(
            args.get("start")
                .and_then(|v| v.as_str())
                .ok_or_else(|| ToolError::InvalidArgs("start required".into()))?,
        )?;
        let end = parse_dt(
            args.get("end")
                .and_then(|v| v.as_str())
                .ok_or_else(|| ToolError::InvalidArgs("end required".into()))?,
        )?;
        let events = self
            .require_calendar()?
            .events(&start, &end, &self.timezone)?;
        Ok(ToolOutput {
            value: serde_json::to_value(events).map_err(|e| ToolError::Other(e.to_string()))?,
        })
    }

    fn find_free_slot_tool(&self, args: Value) -> Result<ToolOutput> {
        let duration_min = args
            .get("duration_min")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| ToolError::InvalidArgs("duration_min required".into()))?;
        let after = parse_dt(
            args.get("after")
                .and_then(|v| v.as_str())
                .ok_or_else(|| ToolError::InvalidArgs("after required".into()))?,
        )?;
        let before = match args.get("before").and_then(|v| v.as_str()) {
            Some(s) => Some(parse_dt(s)?),
            None => None,
        };
        let attendees: Vec<String> = args
            .get("attendees")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let cal = self.require_calendar()?;
        let slot = find_free_slot(cal, duration_min, after, before, &self.timezone, &attendees)?;
        let value = match slot {
            Some((s, e)) => json!({"start": format_datetime(&s), "end": format_datetime(&e)}),
            None => Value::Null,
        };
        Ok(ToolOutput { value })
    }

    fn draft_side_effect(&mut self, kind: DraftKind, args: Value) -> Result<ToolOutput> {
        let id = next_draft_id();
        let draft = Draft {
            id: id.clone(),
            kind,
            payload: args,
            flags: Vec::new(),
        };
        self.drafts.insert(id.clone(), draft.clone());
        Ok(ToolOutput {
            value: serde_json::to_value(draft).map_err(|e| ToolError::Other(e.to_string()))?,
        })
    }

    fn escalate(&self, args: Value) -> Result<ToolOutput> {
        Ok(ToolOutput {
            value: json!({
                "escalation_requested": true,
                "reason": args.get("reason"),
                "task": args.get("task"),
                "payload_description": args.get("payload_description"),
            }),
        })
    }
}

/// Thin `Tool` adapters that validate + delegate into a shared registry pattern for docs/tests.
pub struct NamedTool {
    name: ToolName,
    side_effect: bool,
}

impl NamedTool {
    pub fn new(name: ToolName) -> Self {
        Self {
            name,
            side_effect: matches!(
                name,
                ToolName::DraftEmail
                    | ToolName::CreateEvent
                    | ToolName::UpdateEvent
                    | ToolName::CancelEvent
            ),
        }
    }
}

impl Tool for NamedTool {
    fn name(&self) -> ToolName {
        self.name
    }

    fn args_schema(&self) -> Value {
        schema_for_tool(self.name)
    }

    fn is_side_effect(&self) -> bool {
        self.side_effect
    }

    fn call(&self, args: Value, _ctx: &ToolCtx) -> Result<ToolOutput> {
        validate_args(self.name.as_str(), &args)?;
        // Stateless validation-only path; full execution uses ToolRegistry::call.
        Err(ToolError::Other(
            "use ToolRegistry::call for execution with fixtures".into(),
        ))
    }
}

pub fn all_tools() -> Vec<NamedTool> {
    [
        ToolName::SearchEmail,
        ToolName::GetThread,
        ToolName::DraftEmail,
        ToolName::ListEvents,
        ToolName::FindFreeSlot,
        ToolName::CreateEvent,
        ToolName::UpdateEvent,
        ToolName::CancelEvent,
        ToolName::QuerySheet,
        ToolName::Escalate,
    ]
    .into_iter()
    .map(NamedTool::new)
    .collect()
}

#[allow(dead_code)]
fn _duration_default_before(after: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    after + Duration::days(7)
}
