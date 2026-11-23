//! Core API types and traits for Donna. No I/O, no OS code.

mod agent;
mod approval;
mod consent;
mod events;
mod frontier;
mod types;

pub use agent::{Agent, StubAgent};
#[cfg(any(test, feature = "test-approver"))]
pub use approval::AlwaysApprover;
pub use approval::{ApprovalToken, Approver};
#[cfg(any(test, feature = "test-approver"))]
pub use consent::GrantConsent;
pub use consent::{Consent, ConsentPolicy, DenyConsent};
pub use events::{AgentEvent, EventSink, NoopSink, VecSink};
pub use frontier::{FrontierError, FrontierProvider};
pub use types::{
    Context, Draft, DraftKind, EscalateReason, Escalation, Input, Route, RouteLabel, Selection,
    SelectionKind, ToolCall, Trace, Untrusted,
};
