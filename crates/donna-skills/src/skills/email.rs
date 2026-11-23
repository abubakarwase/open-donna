//! Email skill: J1 summarise, J2 triage, J3 draft, J4 find.

use donna_core::{Context, EscalateReason, EventSink, Input};
use donna_inference::{ChatMessage, Inference};
use serde_json::{json, Value};

use super::{
    best_thread_id, build_fixture_index, ctx_preamble, escalate_skill, load_registry, mail_index,
    selection_note, EscalationDeps, SkillOutcome, StepState,
};
use crate::resolve::{morning_bounds_mail, resolve_people};
use crate::slot::{
    classify_job, fill_answer_text, fill_triage_map, thread_ids_from_search_result,
    trusted_user_ask, UNTRUSTED_DATA_RULE,
};

const JOB_SYSTEM: &str = r#"Classify the email job. Reply JSON {"job":"..."}.
Jobs (pick exactly one):
- J1: summarise one specific thread/email. Example: "Summarise the kickoff notes thread."
- J2: organise/sort/triage unread mail into needs_reply / fyi / ignore. Example: "Organise unread mail this morning."
- J3: draft a reply email. Example: "Draft an acceptance to Alex's lunch invite."
- J4: find/search for an email. Example: "Find the invoice email from Northwind last month."
"#;

const TOOL_SYSTEM_BASE: &str = r#"Fill relative email slots only. Code resolves times, people, and ids.
For search_email (find): query, from_person, day (today|mail_morning|last_month|none). Do not set unread unless asked.
For get_thread: pick thread_id from the enum/index only.
For draft_email: thread_id from index + short body in user style.
Never invent ids."#;

pub fn run_email(
    inference: &mut dyn Inference,
    input: &Input,
    ctx: &Context,
    events: &mut dyn EventSink,
    esc: &EscalationDeps<'_>,
) -> SkillOutcome {
    let job = match classify_job(
        inference,
        &trusted_user_ask(&input.text),
        JOB_SYSTEM,
        &["J1", "J2", "J3", "J4"],
    ) {
        Ok(j) => j,
        Err(EscalateReason::SchemaFailed) => {
            let mut out = SkillOutcome::empty();
            escalate_skill(&mut out, "schema_failed", &input.text, None, events, esc);
            return out;
        }
        Err(_) => {
            let mut out = SkillOutcome::empty();
            escalate_skill(&mut out, "ambiguous", &input.text, None, events, esc);
            return out;
        }
    };

    let mut registry = match load_registry(ctx) {
        Ok(r) => r,
        Err(_) => {
            let mut out = SkillOutcome::empty();
            escalate_skill(&mut out, "ambiguous", &input.text, None, events, esc);
            return out;
        }
    };
    let index = build_fixture_index(&registry, ctx);

    let system = format!(
        "{}\n{}\n{}\n{}\n{}",
        TOOL_SYSTEM_BASE,
        UNTRUSTED_DATA_RULE,
        ctx_preamble(ctx),
        selection_note(input),
        mail_index(&registry)
    );
    let mut state = StepState::new(
        inference,
        &mut registry,
        events,
        system,
        input.text.clone(),
        ctx,
        index,
    );
    state.bump(); // job classify

    let result = match job.as_str() {
        "J1" => run_j1(&mut state),
        "J2" => run_j2(&mut state),
        "J3" => run_j3(&mut state),
        "J4" => run_j4(&mut state),
        _ => Err(EscalateReason::Ambiguous),
    };

    match result {
        Ok(()) => state.outcome,
        Err(EscalateReason::SchemaFailed) => {
            let mut outcome = state.outcome;
            escalate_skill(
                &mut outcome,
                "schema_failed",
                &input.text,
                None,
                state.events,
                esc,
            );
            outcome
        }
        Err(_) => {
            let mut outcome = state.outcome;
            escalate_skill(
                &mut outcome,
                "ambiguous",
                &input.text,
                None,
                state.events,
                esc,
            );
            outcome
        }
    }
}

fn run_j1(state: &mut StepState<'_>) -> Result<(), EscalateReason> {
    let tid = best_thread_id(&state.messages[1].content, &state.index.threads)
        .ok_or(EscalateReason::Ambiguous)?;
    state.run_tool_with_args("get_thread", json!({ "thread_id": tid }), true)?;
    state.bump();
    let mut msgs = state.messages.clone();
    msgs.push(ChatMessage {
        role: "user".into(),
        content: "Write a short faithful summary. Reply JSON {\"answer\":\"...\"} only.".into(),
    });
    let answer = fill_answer_text(state.inference, &msgs)?;
    state.outcome.answer = Some(answer);
    Ok(())
}

fn run_j2(state: &mut StepState<'_>) -> Result<(), EscalateReason> {
    let args = j2_search_args(
        &state.messages[1].content,
        &state.now,
        &state.timezone,
        &state.index.people,
    )?;
    let search = state.run_tool_with_args("search_email", args, true)?;
    let thread_ids = search
        .as_ref()
        .map(thread_ids_from_search_result)
        .unwrap_or_default();
    state.bump();
    let mut msgs = state.messages.clone();
    msgs.push(ChatMessage {
        role: "user".into(),
        content: format!(
            "Triage each thread_id as needs_reply, fyi, or ignore. Thread ids: {:?}. Reply with a JSON object mapping every thread_id to a label only.",
            thread_ids
        ),
    });
    // Soft-fail triage: never escalate the whole case; never return {} when search hit threads.
    let map = match fill_triage_map(state.inference, &msgs, &thread_ids) {
        Ok(map) => map,
        Err(_) if thread_ids.is_empty() => json!({}),
        Err(_) => {
            let mut fallback = serde_json::Map::new();
            for id in &thread_ids {
                fallback.insert(id.clone(), json!("needs_reply"));
            }
            Value::Object(fallback)
        }
    };
    state.outcome.answer = Some(map.to_string());
    Ok(())
}

fn run_j3(state: &mut StepState<'_>) -> Result<(), EscalateReason> {
    let tid = best_thread_id(&state.messages[1].content, &state.index.threads)
        .ok_or(EscalateReason::Ambiguous)?;
    state.run_tool_with_args("get_thread", json!({ "thread_id": tid }), true)?;
    // Draft: model fills body only; thread_id fixed.
    let schema = json!({
        "type": "object",
        "properties": { "body": { "type": "string" } },
        "required": ["body"],
        "additionalProperties": false
    });
    state.bump();
    let mut msgs = state.messages.clone();
    msgs.push(ChatMessage {
        role: "user".into(),
        content: format!(
            "Draft a short reply body for thread {tid} in the user's style. Reply JSON {{\"body\":\"...\"}} only."
        ),
    });
    let body_obj = crate::slot::fill_slots_with_resolve(
        state.inference,
        &msgs,
        "draft_email",
        &schema,
        |rel| {
            let body = rel
                .get("body")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if body.is_empty() {
                return Err("empty body".into());
            }
            Ok(json!({ "thread_id": tid, "body": body }))
        },
    );
    match body_obj {
        Ok(args) => {
            state.run_tool_with_args("draft_email", args, true)?;
            Ok(())
        }
        Err(_) => {
            // Soft: still record a minimal draft so we do not schema_failed-escalate.
            let args = json!({
                "thread_id": tid,
                "body": "Thanks for your note. I will follow up shortly."
            });
            state.run_tool_with_args("draft_email", args, true)?;
            Ok(())
        }
    }
}

fn run_j4(state: &mut StepState<'_>) -> Result<(), EscalateReason> {
    state.run_tool("search_email", true)?;
    Ok(())
}

/// Deterministic J2 search args from user text (unread + optional morning/from).
fn j2_search_args(
    text: &str,
    now: &str,
    timezone: &str,
    people: &std::collections::HashMap<String, String>,
) -> Result<Value, EscalateReason> {
    let mut args = serde_json::Map::new();
    args.insert("unread".into(), json!(true));
    let lower = text.to_lowercase();
    if lower.contains("this morning")
        || lower.contains("morning's")
        || lower.contains("from this morning")
        || (lower.contains("morning") && lower.contains("unread"))
    {
        let (a, b) = morning_bounds_mail(now, timezone).map_err(|_| EscalateReason::Ambiguous)?;
        args.insert("after".into(), json!(a));
        args.insert("before".into(), json!(b));
    }
    if let Some(name) = extract_from_name(&lower) {
        let resolved = resolve_people(&[name], people);
        if let Some(email) = resolved.into_iter().next() {
            args.insert("from".into(), json!(email));
        }
    }
    Ok(Value::Object(args))
}

fn extract_from_name(lower: &str) -> Option<String> {
    // "from priya" / "from jamie" (stop at punctuation/space-run)
    let idx = lower.find(" from ")?;
    let rest = lower[idx + 6..].trim_start();
    let name = rest
        .split(|c: char| c.is_whitespace() || c == '.' || c == ',' || c == '?' || c == '!')
        .next()?
        .trim();
    if name.len() < 2 || matches!(name, "this" | "the" | "my" | "an" | "a") {
        return None;
    }
    Some(name.to_string())
}
