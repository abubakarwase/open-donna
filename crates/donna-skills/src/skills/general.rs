//! General skill: J12 answer or polite decline; no tools.

use donna_core::{Context, EscalateReason, EventSink, Input};
use donna_inference::{ChatMessage, Inference};

use super::{escalate_skill, EscalationDeps, SkillOutcome};
use crate::slot::fill_answer_text;

const SYSTEM: &str = r#"You are Donna, an on-device assistant. For unsupported asks or chit-chat, reply briefly.
Politely decline anything outside email, calendar, and sheet help. Do not call tools.
Reply JSON {"answer":"..."} only."#;

pub fn run_general(
    inference: &mut dyn Inference,
    input: &Input,
    _ctx: &Context,
    events: &mut dyn EventSink,
    esc: &EscalationDeps<'_>,
) -> SkillOutcome {
    let mut outcome = SkillOutcome::empty();
    outcome.steps = 1;
    let messages = vec![
        ChatMessage {
            role: "system".into(),
            content: SYSTEM.into(),
        },
        ChatMessage {
            role: "user".into(),
            content: input.text.clone(),
        },
    ];
    match fill_answer_text(inference, &messages) {
        Ok(answer) => {
            outcome.answer = Some(answer);
            outcome
        }
        Err(EscalateReason::SchemaFailed) => {
            escalate_skill(
                &mut outcome,
                "schema_failed",
                &input.text,
                None,
                events,
                esc,
            );
            outcome
        }
        Err(_) => {
            escalate_skill(&mut outcome, "ambiguous", &input.text, None, events, esc);
            outcome
        }
    }
}
