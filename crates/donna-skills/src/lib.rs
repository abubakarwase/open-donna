//! Skills and router. Closed-loop skill engine is C-05; router is C-04.

mod engine;
mod escalation;
mod resolve;
mod router;
mod skills;
mod slot;

pub use engine::SkillAgent;
pub use escalation::{
    build_privacy_payload, run_escalation, PayloadInput, DEFAULT_MAX_PAYLOAD_BYTES,
};
pub use router::{
    decision_to_trace_parts, label_confidence, LlmRouter, Result as RouterResult, Router,
    RouterDecision, RouterError, RoutingAgent, ROUTER_MIN_CONFIDENCE,
};
pub use skills::SkillOutcome;
pub use slot::{
    fence_untrusted, fill_slots, schema_for_constrained, trusted_user_ask, UNTRUSTED_DATA_RULE,
};

use donna_core::{Context, Input, Trace};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum SkillError {
    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, SkillError>;

/// Named skill that can run a case turn. Implemented per domain in C-05.
pub trait Skill {
    fn name(&self) -> &str;
    fn run(&self, input: &Input, ctx: &Context) -> Result<Trace>;
}
