# M0 / M1 build progress (overnight run, 2026-11-21)

User instruction (2026-11-21, before sleeping): finish M0, make judgement calls on M0 outcomes, continue M1. Do not commit without approval (E-01 committed as d307ef8; everything after is staged only).

How: one Cursor crew task per ticket via `/cursor-orchestrator` (scripts in the session scratchpad: `run_crew.sh <ticket>`, `run_fix.sh <ticket>`). Opus writes `opus-plan.md` per ticket and does the final check.

## M0 status

| Ticket | Status | Notes |
|---|---|---|
| E-01 | done, committed | `evals/spec/scope.md`; J10 deferred |
| E-02 | done, staged | 10 tool schemas, labels; Opus fixes: unread filter, reply thread_id, no-op update |
| E-03 | done, staged | case/trace schemas, `donna-evals-validate`, SUT protocol doc |
| E-04 | done, staged | fixtures generator; Opus fixes: realistic mail, triage labels, injection per mailbox. Mail bodies still somewhat templated |
| E-05 | done, staged | router 200 cases |
| E-06 | done, staged | email 200 cases; Opus fixes: no persona hints, realistic `now` |
| E-07 | done, staged | calendar 200 cases; Opus fixes: no placeholders, 25 DST cases |
| E-08 | done, staged | sheet 200 cases, DuckDB loader; Opus fixes: ratios, escalate variety, 12 join cases |
| E-11 | done, staged | safety 100 cases; Opus fix: id suffixes removed, repo-wide input-leak test |
| E-09 | done, staged | 4 rubrics, backend-agnostic judge, 50 calibration rows from Qwen3.5-4B (human grades pending) |
| E-12 | done, staged | runner + scorers + oracle/null SUTs; Opus fixes: schema-valid over emitted calls, runner-measured wall latency + RSS |
| E-14 | done, staged | `evals/ci.sh` check/dev/test (test gated by env), GitHub workflow, evals/README.md |
| E-13 | done, staged | model-only SUT, baseline (BASELINE.md), thresholds ratified provisionally (2B default; SQL fixed scorer: aliases ignored). See decisions.md "M0 outcome" |
| E-10 | deferred | with J10 |
| E-15, E-16 | user | 8 GB device; real slice |

## Needs the user

- Review 100% of `test` splits (tag `needs_human_review`).
- Grade ~50 calibration examples for the judge (E-09).
- Confirm provisional thresholds (E-13).
- Approve commits.

## M1 status

| Ticket | Status | Notes |
|---|---|---|
| C-01+C-02 | done, staged | 5 crates, core API (`docs/core-api.md`), stub agent runs all dev cases; workspace, core API, stub agent, `donna-sut` |
| C-03 | done, staged | llama-server child backend, JSON schema, LoRA, idle unload, RSS; real-model test passed; llama-server backend |
| C-06 | done, staged | schema arg validation, mock mail/calendar/sheet, free-slot = all 60 slot truths, draft-only side effects; draft id optional in trace schema; tools, mock connectors, draft-only side effects |
| C-04 | done, staged | LlmRouter (2B), logprob confidence; dev route_accuracy 0.82, escalation_recall 0.86, over_escalation 0.17; router (Qwen3.5-2B prompted default) |
| C-05 | done, staged | skill engine + code-side slot resolver (dates, people, ids); dev email tool 0.86 arg_f1 0.81 over_esc 0; calendar tool 0.74 arg_f1 0.75 slot 0.41; router 0.86; sheet gap → C-07; triage 0.25 on small n (watch); skill engine, closed loop |
| C-07 | done, staged | calamine+DuckDB loader (parity with evals), read-only SQL guard, sheet skill; dev tool 0.78 arg_f1 0.81 sql_exec 0.38 esc_recall 0.77 over_esc 0.22; sheet pipeline |
| C-09 | done, staged | approval token test-only approver + compile_fail proof, random-boundary fencing, recipient guard; safety dev 100%; approval gate, untrusted fencing |
| C-10 | done, staged | FrontierProvider (OpenAI-compatible + mock), consent policy (default ask each time), privacy payload (no raw rows, 16 KiB cap); safety dev 100%; escalation |
| C-11 | done, staged | RSS = max(self, llama-server); `--cache-ram 512` (8 GiB default leaked to 4.2 GB); triage fixed; `evals/m1.sh` + `evals/reports-m1/M1.md`: all M1 gates pass on M1 Pro; perf + M1 eval run |
| C-08 | deferred | with J10 |
