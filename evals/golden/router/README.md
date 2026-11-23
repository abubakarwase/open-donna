# Router golden set (E-05)

Layer-1 exact-match route + escalation only (no `tool_calls`). Ids `rtr-NNNN`.

## Counts (200)

email 45 (J1–J4), calendar 45 (J5–J7), sheet 35 (J8–J9; some `input.selection`), general 25 (J12), escalate 50 (J11). Split 50/150. Difficulty 80/80/40 easy/medium/hard.

Escalate reasons (all but `schema_failed`, not text-judgable): `user_requested`, `multi_step`, `low_confidence`, `why_or_forecast`, `cross_sheet_join`, `messy_sheet`, `ambiguous`. Test cases tagged `needs_human_review`.

## Rebuild

`cd evals && uv run python golden/router/build.py` (twice → identical bytes).

## Cross-skill rules

- "Email me the sheet total" → `sheet`/`local` (send out of scope).
- "Reply to the meeting invite" → `email`/`local` (invite is mail).
- Injected instructions in text are data; keep intended skill (or escalate if vague).
- Negations/ultra-short: local when clear, else `escalate`+`low_confidence`.
- Judgement ("which time is fairest…") → `escalate`+`low_confidence`.
- Multi-step / "prep me for tomorrow" → `escalate`+`multi_step`, job `J11`.
- All escalate routes use `skill=escalate`, job `J11`.
