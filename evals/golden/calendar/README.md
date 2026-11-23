# Calendar golden set (E-07)

Layer-1 closed-loop tool calls for J5–J7 (plus ~20 calendar-flavoured escalate / J11). Ids `cal-NNNN`.

## Counts (200)

J5 agenda 55 (`list_events`, `structured`), J6 free slot 60 (`find_free_slot` + sidecar), J7 create/move/cancel 65 (drafts only), escalate 20 (J11). Split 50/150. Difficulty ~80/80/40. Test cases tagged `needs_human_review`.

## Rebuild

`cd evals && uv run python golden/calendar/build.py` (twice → identical bytes).

## Day / week / recurrence rules

- **Day:** local midnight → next midnight (or end of the named day); matches `day_bounds` in `build.py`.
- **Week:** Monday 00:00 → next Monday 00:00. If `now` is Sat/Sun, "this week" is the upcoming Mon–Sun (e.g. Sat 2026-11-21 → 2026-11-23 … 2026-11-30).
- **This afternoon:** local 12:00 → 18:00; if `now` is already afternoon, `now` → 18:00.
- **Recurring:** title+day without "series/all" → dated instance id (e.g. `evt_standup_2026-11-24`). Friday 1:1 uses series id `evt_friday_1on1`. No separate standup series-master id in fixtures; "cancel all standups" phrasing is skipped.

## Schema gap

Earliest free slot for J6 lives in `slot_truth.json` (keyed by case id), tagged `slot_truth:slot_truth.json`, because `case.schema.json` has no slot field.
