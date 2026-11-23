# v1 scope

## Purpose

This file defines the frozen v1 job list for the personal assistant. Jobs not listed here are out of v1. Golden sets (E-05–E-11) cover exactly these jobs.

## Jobs

| ID | Job | Skill | Loop type | Example request | Expected outcome (tools) | Router label | Escalation |
|---|---|---|---|---|---|---|---|
| J1 | Summarise a thread or my unread mail | email | closed | Summarise the unread thread from Dana about the Q3 budget. | `get_thread` (and/or `search_email`) → short summary | `email` | `local` |
| J2 | Triage mail: needs reply / FYI / ignore | email | closed | Triage my unread inbox from this morning. | `search_email` / `get_thread` → triage labels | `email` | `local` |
| J3 | Draft a reply with a given intent, in my style | email | closed + rubric | Draft a polite decline to Alex's lunch invite, in my style. | `get_thread` + `draft_email` → draft for approval | `email` | `local` |
| J4 | Find an email ("the invoice from Acme last month") | email | closed | Find the invoice email from Acme last month. | `search_email` → matching message(s) | `email` | `local` |
| J5 | What is on my day / week | calendar | closed | What is on my calendar this week? | `list_events` → day/week agenda | `calendar` | `local` |
| J6 | Find a free slot with constraints | calendar | closed | Find a free 30-minute slot with Sam next Tuesday afternoon. | `find_free_slot` → proposed slot | `calendar` | `local` |
| J7 | Create, move or cancel an event (as a draft to approve) | calendar | closed | Move tomorrow's standup to 10:00 and cancel Friday's 1:1. | `create_event` / `update_event` / `cancel_event` → draft change | `calendar` | `local` |
| J8 | Ask a question about an open sheet | sheet | closed (SQL) | What is the total revenue in the open sales sheet? | `query_sheet` → answered from open sheet | `sheet` | `local` |
| J9 | Ask about a selected range of a sheet | sheet | closed (SQL) | Average the selected cells in column B. | `query_sheet` → answered from selected range | `sheet` | `local` |
| J11 | Hand a hard task to a frontier model, with consent | escalate | escalate | With my consent, escalate: rewrite this long mail thread into a careful negotiation reply. | `escalate` → frontier handoff with consent | `escalate` | `escalate` |
| J12 | Unsupported or chit-chat request: answer or decline politely | general | closed | Tell me a joke, or rewrite this PDF for me. | No v1 tool (or polite decline) | `general` | `local` |

## Escalation boundary

Escalate globally when router confidence is low, the request is multi-step (J10 is deferred), or schema validation fails twice. Escalate for sheets when messy, cross-sheet joins, "why"/forecast, or ambiguous.

| Trigger | Example request | Why it escalates |
|---|---|---|
| Low router confidence | "Do the thing with that message from earlier." | Intent unclear; do not guess |
| Multi-step request | "Prep me for the week: agenda, triage mail, draft each reply, then reschedule conflicts." | J10 is deferred; v1 has no bounded open loop |
| Schema validation fails twice | Tool args for `create_event` fail schema twice | Local path stuck; hand off |
| Messy sheet | "Total sales in this sheet with merged headers." | Merged cells / multi-header |
| Cross-sheet join | "Join Orders and Customers and sum by region." | Needs join across sheets |
| Why / forecast | "Why did margins drop, and what next quarter looks like?" | Causal / forecast, not lookup |
| Ambiguous question | "How are we doing?" on an open sheet | No clear metric or range |
| Email/calendar judgement | "Which of these three meeting times is fairest given everyone's constraints?" | Needs judgement beyond v1 jobs |

Jobs J1–J9 and J12 default to `local`. J11 is the explicit escalate job (`escalate` router label and escalation).

## Deferred (after v1)

| ID | Job | Skill | Loop type | Example request | Planned outcome (tools) | Router label |
|---|---|---|---|---|---|---|
| J10 | Multi-step request ("prep me for tomorrow") | mixed | bounded open | Prep me for tomorrow: agenda plus unread mail that needs a reply. | `list_events` + `search_email` → brief (no side effects) | `multi_step` |

- Deferred on 2026-11-21. Kept in the plan: it returns after v1 with its golden set (E-10) and the bounded open loop (C-08).
- In v1, a multi-step request is labelled `escalate` (the user can hand it to a frontier model, with consent).

## Non-goals

- Multi-step requests run locally (J10, deferred)
- Sending or changing anything without approval
- More than one account per provider
- Editing attachments
- Documents card
- Voice
- Windows and Linux builds
- On-device style fine-tuning (v1 uses a few-shot style profile)
- Languages other than English

## Assumptions

- English only: every job, golden set and model in v1 is built for English
- Frontier rescue is told up front: before install, the user is told they can always hand a task to a frontier model (their own API key, consent each time)
- One account per provider
- Frozen `now` and timezone per eval case
- Mock connectors for all evals
- Nothing sent or changed without the approval gate; drafts are not side effects
- Untrusted content (mail, invites, sheet cells) is data, never instructions
- Reference tier: 8 GB machine with Qwen3.5-2B and FunctionGemma router

## Decisions (2026-11-21)

- J10 deferred to after v1; kept in the roadmap.
- J11 stays in v1; the frontier option is explained before install.
- English only for v1.

### Status

Status: signed off by the user

Date: 2026-11-21
