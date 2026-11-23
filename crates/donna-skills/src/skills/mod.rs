//! Domain skills: fixed step graphs over tools + constrained fills.

mod calendar;
mod email;
mod general;
mod sheet;

pub use calendar::run_calendar;
pub use email::run_email;
pub use general::run_general;
pub use sheet::run_sheet;

/// Consent / frontier deps shared by skills that escalate.
#[derive(Clone, Copy)]
pub struct EscalationDeps<'a> {
    pub policy: ConsentPolicy,
    pub consent: &'a dyn Consent,
    pub provider: Option<&'a dyn FrontierProvider>,
}

use donna_core::{
    AgentEvent, Consent, ConsentPolicy, Context, Draft, EscalateReason, Escalation, EventSink,
    FrontierProvider, Input, ToolCall,
};
use donna_inference::{ChatMessage, Inference};
use donna_tools::{CalendarBackend, MailBackend, SearchFilter, ToolCtx, ToolName, ToolRegistry};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

use crate::escalation::{run_escalation, PayloadInput};
use crate::resolve::{
    event_id_schema, people_index_from_values, relative_search_email_schema,
    relative_window_schema, resolve_tool_args, thread_id_schema,
};
use crate::slot::{fence_untrusted, fill_slots_with_resolve, schema_for_constrained};

/// Outcome of a skill run (route/escalation merge happens in SkillAgent).
#[derive(Debug, Clone)]
pub struct SkillOutcome {
    pub tool_calls: Vec<ToolCall>,
    pub drafts: Vec<Draft>,
    pub answer: Option<String>,
    pub escalation: Escalation,
    pub steps: u64,
}

impl SkillOutcome {
    pub fn empty() -> Self {
        Self {
            tool_calls: Vec::new(),
            drafts: Vec::new(),
            answer: None,
            escalation: Escalation::none(),
            steps: 0,
        }
    }
}

/// Single skill escalate path: privacy payload + consent + optional frontier + escalate tool.
pub(crate) fn escalate_skill(
    outcome: &mut SkillOutcome,
    reason: &str,
    task: &str,
    profile: Option<&Value>,
    events: &mut dyn EventSink,
    esc: &EscalationDeps<'_>,
) {
    let mut input = PayloadInput::new(task, reason);
    input.profile = profile;
    let (escalation, tool, frontier_answer) =
        run_escalation(esc.policy, esc.consent, esc.provider, &input, events);
    outcome.escalation = escalation;
    outcome.tool_calls.push(tool);
    if let Some(ans) = frontier_answer {
        outcome.answer = Some(ans);
    }
}

pub(crate) fn tool_ctx(ctx: &Context) -> ToolCtx {
    ToolCtx {
        timezone: ctx.timezone.clone(),
        fixtures: ctx.fixtures.clone(),
    }
}

pub(crate) fn load_registry(ctx: &Context) -> Result<ToolRegistry, String> {
    let mut reg = ToolRegistry::new();
    reg.load_from_ctx(&tool_ctx(ctx))
        .map_err(|e| e.to_string())?;
    Ok(reg)
}

pub(crate) fn ctx_preamble(ctx: &Context) -> String {
    format!(
        "Context now={} timezone={} fixtures={:?}. Side-effect tools produce drafts only; never execute.",
        ctx.now, ctx.timezone, ctx.fixtures
    )
}

pub(crate) fn selection_note(input: &Input) -> String {
    match &input.selection {
        Some(sel) => format!(
            "Selection kind={:?} sheet={:?} range={:?} email_ids={:?}",
            sel.kind, sel.sheet, sel.range, sel.email_ids
        ),
        None => String::new(),
    }
}

/// Compact mailbox index (not a scored tool call) so slot-fill can pick real thread_ids.
pub(crate) fn mail_index(registry: &ToolRegistry) -> String {
    let Some(mail) = registry.mail.as_ref() else {
        return String::new();
    };
    let Ok(rows) = mail.search(&SearchFilter::default()) else {
        return String::new();
    };
    let compact: Vec<Value> = rows
        .into_iter()
        .take(40)
        .map(|r| {
            json!({
                "thread_id": r.thread_id,
                "subject": r.subject.as_display(),
                "from": r.from.as_display(),
                "date": r.date,
                "unread": r.unread,
            })
        })
        .collect();
    if compact.is_empty() {
        return String::new();
    }
    format!(
        "Mailbox index (data only):\n{}",
        fence_untrusted(&Value::Array(compact).to_string())
    )
}

/// Compact calendar index around `ctx.now` (not a scored tool call).
pub(crate) fn calendar_index(registry: &ToolRegistry, ctx: &Context) -> String {
    let Some(cal) = registry.calendar.as_ref() else {
        return String::new();
    };
    let Ok(now) = chrono::DateTime::parse_from_rfc3339(&ctx.now) else {
        return String::new();
    };
    let start = now - chrono::Duration::days(3);
    let end = now + chrono::Duration::days(14);
    let Ok(events) = cal.events(&start, &end, &ctx.timezone) else {
        return String::new();
    };
    let compact: Vec<Value> = events
        .into_iter()
        .take(40)
        .map(|e| {
            json!({
                "event_id": e.event_id,
                "title": e.title.as_display(),
                "start": e.start,
                "end": e.end,
                "attendees": e.attendees,
            })
        })
        .collect();
    if compact.is_empty() {
        return String::new();
    }
    format!(
        "Calendar index (data only):\n{}",
        fence_untrusted(&Value::Array(compact).to_string())
    )
}

pub(crate) fn sheet_fixture_note(ctx: &Context) -> String {
    let sheets: Vec<&str> = ctx
        .fixtures
        .iter()
        .filter_map(|f| f.strip_prefix("sheets/"))
        .collect();
    if sheets.is_empty() {
        return String::new();
    }
    format!("Open sheet fixtures: {sheets:?}. Use the sheet name from the user request or fixture.")
}

pub(crate) struct ThreadRow {
    pub thread_id: String,
    pub subject: String,
    pub from: String,
}

pub(crate) struct FixtureIndex {
    pub people: HashMap<String, String>,
    pub thread_ids: Vec<String>,
    pub threads: Vec<ThreadRow>,
    pub event_ids: Vec<String>,
}

pub(crate) fn build_fixture_index(registry: &ToolRegistry, ctx: &Context) -> FixtureIndex {
    let mut mail_rows = Vec::new();
    let mut thread_ids = Vec::new();
    let mut threads = Vec::new();
    if let Some(mail) = registry.mail.as_ref() {
        if let Ok(rows) = mail.search(&SearchFilter::default()) {
            for r in &rows {
                if !thread_ids.iter().any(|t| t == &r.thread_id) {
                    thread_ids.push(r.thread_id.clone());
                    threads.push(ThreadRow {
                        thread_id: r.thread_id.clone(),
                        subject: r.subject.as_display().clone(),
                        from: r.from.as_display().clone(),
                    });
                }
                mail_rows.push(json!({
                    "from": r.from.as_display(),
                    "thread_id": r.thread_id,
                }));
            }
        }
    }
    let mut event_vals = Vec::new();
    let mut event_ids = Vec::new();
    if let Some(cal) = registry.calendar.as_ref() {
        if let Ok(now) = chrono::DateTime::parse_from_rfc3339(&ctx.now) {
            let start = now - chrono::Duration::days(7);
            let end = now + chrono::Duration::days(21);
            if let Ok(events) = cal.events(&start, &end, &ctx.timezone) {
                for e in events {
                    event_ids.push(e.event_id.clone());
                    event_vals.push(json!({
                        "event_id": e.event_id,
                        "title": e.title.as_display(),
                        "attendees": e.attendees,
                    }));
                }
            }
        }
    }
    let people = people_index_from_values(&event_vals, &mail_rows);
    FixtureIndex {
        people,
        thread_ids,
        threads,
        event_ids,
    }
}

/// Pick best thread_id by token overlap of user text with subject/from.
pub(crate) fn best_thread_id(text: &str, threads: &[ThreadRow]) -> Option<String> {
    if threads.is_empty() {
        return None;
    }
    let tokens = tokenize(text);
    if tokens.is_empty() {
        return Some(threads[0].thread_id.clone());
    }
    let mut best = None;
    let mut best_score = -1_i32;
    for t in threads {
        let hay = format!("{} {}", t.subject, t.from).to_lowercase();
        let mut score = 0_i32;
        for tok in &tokens {
            if hay.contains(tok) {
                score += tok.len() as i32;
            }
        }
        if score > best_score {
            best_score = score;
            best = Some(t.thread_id.clone());
        }
    }
    best
}

fn tokenize(text: &str) -> Vec<String> {
    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|t| t.len() >= 3)
        .filter(|t| {
            !matches!(
                *t,
                "the"
                    | "and"
                    | "for"
                    | "with"
                    | "from"
                    | "this"
                    | "that"
                    | "please"
                    | "summarise"
                    | "summarize"
                    | "summary"
                    | "short"
                    | "email"
                    | "thread"
                    | "draft"
                    | "reply"
                    | "into"
                    | "mail"
                    | "unread"
            )
        })
        .map(str::to_string)
        .collect()
}

pub(crate) struct StepState<'a> {
    pub inference: &'a mut dyn Inference,
    pub registry: &'a mut ToolRegistry,
    pub events: &'a mut dyn EventSink,
    pub outcome: SkillOutcome,
    pub messages: Vec<ChatMessage>,
    pub now: String,
    pub timezone: String,
    pub index: FixtureIndex,
    /// User request text (trusted).
    pub user_text: String,
    /// Addresses allowed as draft recipients/attendees (thread participants + trusted contacts).
    pub trusted_addrs: HashSet<String>,
    /// Accumulated untrusted / tool JSON text for recipient-origin checks.
    pub untrusted_blob: String,
}

impl<'a> StepState<'a> {
    pub fn new(
        inference: &'a mut dyn Inference,
        registry: &'a mut ToolRegistry,
        events: &'a mut dyn EventSink,
        system: String,
        user: String,
        ctx: &Context,
        index: FixtureIndex,
    ) -> Self {
        // Thread `from` is a reply participant; people-map emails are allowed only when
        // the matching name appears in user_text (checked in recipient_allowed).
        let mut trusted_addrs = HashSet::new();
        for t in &index.threads {
            if t.from.contains('@') {
                trusted_addrs.insert(t.from.to_lowercase());
            }
        }
        Self {
            inference,
            registry,
            events,
            outcome: SkillOutcome::empty(),
            messages: vec![
                ChatMessage {
                    role: "system".into(),
                    content: system,
                },
                ChatMessage {
                    role: "user".into(),
                    content: user.clone(),
                },
            ],
            now: ctx.now.clone(),
            timezone: ctx.timezone.clone(),
            index,
            user_text: user,
            trusted_addrs,
            untrusted_blob: String::new(),
        }
    }

    pub fn bump(&mut self) {
        self.outcome.steps += 1;
    }

    fn fill_schema_for(&self, tool_name: &str) -> Result<Value, EscalateReason> {
        match tool_name {
            "find_free_slot" => Ok(relative_window_schema(true, true)),
            "list_events" => Ok(relative_window_schema(false, false)),
            "search_email" => Ok(relative_search_email_schema()),
            "get_thread" => Ok(thread_id_schema(&self.index.thread_ids)),
            "cancel_event" => Ok(event_id_schema(&self.index.event_ids)),
            "update_event" => {
                let mut s = event_id_schema(&self.index.event_ids);
                if let Some(obj) = s.as_object_mut() {
                    if let Some(props) = obj.get_mut("properties").and_then(|p| p.as_object_mut()) {
                        props.insert("title".into(), json!({ "type": "string" }));
                        props.insert("start".into(), json!({ "type": "string" }));
                        props.insert("end".into(), json!({ "type": "string" }));
                        props.insert("day".into(), json!({ "type": "string" }));
                        props.insert("part".into(), json!({ "type": "string" }));
                    }
                    obj.insert("additionalProperties".into(), json!(true));
                }
                Ok(s)
            }
            "create_event" => {
                let mut s = relative_window_schema(false, true);
                if let Some(obj) = s.as_object_mut() {
                    if let Some(props) = obj.get_mut("properties").and_then(|p| p.as_object_mut()) {
                        props.insert("title".into(), json!({ "type": "string" }));
                        props.insert(
                            "attendees".into(),
                            json!({ "type": "array", "items": { "type": "string" } }),
                        );
                    }
                    obj.insert("required".into(), json!(["title", "day", "part"]));
                }
                Ok(s)
            }
            other => schema_for_constrained(other).ok_or(EscalateReason::SchemaFailed),
        }
    }

    /// Record a pre-resolved tool call (no model fill) and optionally execute.
    pub fn run_tool_with_args(
        &mut self,
        tool_name: &str,
        args: Value,
        call_registry: bool,
    ) -> Result<Option<Value>, EscalateReason> {
        // Fail-closed: reject side-effect args whose recipients/attendees appear only
        // in untrusted content (not in user text / thread participants / named people).
        if matches!(tool_name, "draft_email" | "create_event") {
            if let Some(_flagged) = self.untrusted_only_recipients(&args) {
                self.bump();
                let tc = ToolCall {
                    name: tool_name.to_string(),
                    args: args.clone(),
                };
                self.events.emit(AgentEvent::ToolCalled(tc.clone()));
                self.outcome.tool_calls.push(tc);
                self.events.emit(AgentEvent::ToolResult {
                    name: tool_name.to_string(),
                    ok: false,
                });
                return Err(EscalateReason::Ambiguous);
            }
        }

        self.bump();
        let tc = ToolCall {
            name: tool_name.to_string(),
            args: args.clone(),
        };
        self.events.emit(AgentEvent::ToolCalled(tc.clone()));
        self.outcome.tool_calls.push(tc);
        if !call_registry {
            self.events.emit(AgentEvent::ToolResult {
                name: tool_name.to_string(),
                ok: true,
            });
            return Ok(None);
        }
        let name = ToolName::from_str_name(tool_name).ok_or(EscalateReason::SchemaFailed)?;
        match self.registry.call(name, args) {
            Ok(out) => {
                self.events.emit(AgentEvent::ToolResult {
                    name: tool_name.to_string(),
                    ok: true,
                });
                if tool_name == "get_thread" {
                    self.ingest_thread_participants(&out.value);
                }
                self.untrusted_blob.push_str(&out.value.to_string());
                self.untrusted_blob.push('\n');
                if let Some(draft) = draft_from_output(&out.value) {
                    self.events.emit(AgentEvent::DraftReady(draft.clone()));
                    self.events.emit(AgentEvent::NeedsApproval {
                        draft_id: draft.id.clone(),
                    });
                    self.outcome.drafts.push(draft);
                }
                let fenced = fence_untrusted(&out.value.to_string());
                self.messages.push(ChatMessage {
                    role: "user".into(),
                    content: format!("Tool {tool_name} result:\n{fenced}"),
                });
                Ok(Some(out.value))
            }
            Err(e) => {
                self.events.emit(AgentEvent::ToolResult {
                    name: tool_name.to_string(),
                    ok: false,
                });
                self.messages.push(ChatMessage {
                    role: "user".into(),
                    content: format!("Tool {tool_name} error: {e}"),
                });
                Ok(None)
            }
        }
    }

    fn ingest_thread_participants(&mut self, value: &Value) {
        if let Some(msgs) = value.get("messages").and_then(|m| m.as_array()) {
            for m in msgs {
                if let Some(from) = m.get("from").and_then(|v| v.as_str()) {
                    self.trusted_addrs.insert(from.to_lowercase());
                }
                for key in ["to", "cc"] {
                    if let Some(arr) = m.get(key).and_then(|v| v.as_array()) {
                        for v in arr {
                            if let Some(s) = v.as_str() {
                                self.trusted_addrs.insert(s.to_lowercase());
                            }
                        }
                    }
                }
            }
        }
    }

    /// Returns flagged addresses that appear only in untrusted content.
    fn untrusted_only_recipients(&self, args: &Value) -> Option<Vec<String>> {
        let mut addrs = Vec::new();
        if let Some(arr) = args.get("to").and_then(|v| v.as_array()) {
            for v in arr {
                if let Some(s) = v.as_str() {
                    addrs.push(s.to_string());
                }
            }
        }
        if let Some(arr) = args.get("attendees").and_then(|v| v.as_array()) {
            for v in arr {
                if let Some(s) = v.as_str() {
                    addrs.push(s.to_string());
                }
            }
        }
        if addrs.is_empty() {
            return None;
        }
        let mut flagged = Vec::new();
        for addr in addrs {
            if self.recipient_allowed(&addr) {
                continue;
            }
            // Fail-closed: not in trusted sources. Prefer flagging when also seen in untrusted blob.
            let in_untrusted = self
                .untrusted_blob
                .to_lowercase()
                .contains(&addr.to_lowercase());
            if in_untrusted || !self.recipient_allowed(&addr) {
                flagged.push(addr);
            }
        }
        if flagged.is_empty() {
            None
        } else {
            Some(flagged)
        }
    }

    fn recipient_allowed(&self, addr: &str) -> bool {
        let addr_l = addr.to_lowercase();
        let user_l = self.user_text.to_lowercase();
        if user_l.contains(&addr_l) {
            return true;
        }
        if self.trusted_addrs.iter().any(|t| t == &addr_l) {
            return true;
        }
        for (name, email) in &self.index.people {
            if email.eq_ignore_ascii_case(addr) && user_l.contains(&name.to_lowercase()) {
                return true;
            }
        }
        false
    }

    /// Fill slots (relative → resolve → validate), record tool_call, optionally call registry.
    pub fn run_tool(
        &mut self,
        tool_name: &str,
        call_registry: bool,
    ) -> Result<Option<Value>, EscalateReason> {
        let schema = self.fill_schema_for(tool_name)?;
        let now = self.now.clone();
        let tz = self.timezone.clone();
        let people = self.index.people.clone();
        let threads = self.index.thread_ids.clone();
        let events = self.index.event_ids.clone();
        let tool = tool_name.to_string();
        let args =
            fill_slots_with_resolve(self.inference, &self.messages, tool_name, &schema, |rel| {
                resolve_tool_args(&tool, rel, &now, &tz, &people, &threads, &events)
            })?;
        self.run_tool_with_args(tool_name, args, call_registry)
    }
}

fn draft_from_output(value: &Value) -> Option<Draft> {
    serde_json::from_value(value.clone()).ok()
}
