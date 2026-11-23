use crate::types::{Draft, Route, ToolCall};

#[derive(Debug, Clone, PartialEq)]
pub enum AgentEvent {
    Routed(Route),
    ToolCalled(ToolCall),
    ToolResult { name: String, ok: bool },
    DraftReady(Draft),
    EscalationRequested { reason: String, payload_bytes: u64 },
    NeedsApproval { draft_id: String },
    Done,
}

pub trait EventSink {
    fn emit(&mut self, e: AgentEvent);
}

#[derive(Debug, Default)]
pub struct NoopSink;

impl EventSink for NoopSink {
    fn emit(&mut self, _e: AgentEvent) {}
}

#[derive(Debug, Default)]
pub struct VecSink {
    pub events: Vec<AgentEvent>,
}

impl EventSink for VecSink {
    fn emit(&mut self, e: AgentEvent) {
        self.events.push(e);
    }
}
