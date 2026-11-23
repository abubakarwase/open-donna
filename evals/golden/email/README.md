# Email golden set (E-06)

Layer-1 closed-loop tool calls for J1–J4 (plus ~15 email-flavoured escalate / J11). Ids `eml-NNNN`.

## Counts (200)

J1 summarise 47 (`match: rubric`, `summary_faithfulness`), J2 triage 42 (`structured` + sidecar), J3 draft 51 (`rubric`, `draft_quality`), J4 find 45 (`structured`), escalate 15 (J11). Split 50/150. Difficulty ~80/80/40. Test cases tagged `needs_human_review`.

## Rebuild

`cd evals && uv run python golden/email/build.py` (twice → identical bytes).

## Schema gap

Per-thread triage class for J2 lives in `triage_truth.json` (keyed by case id), tagged `triage_truth:triage_truth.json`, because `case.schema.json` has no triage-map field. Do not overload `sql_result`; leave schema for a later ticket.
