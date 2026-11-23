# SUT protocol

The eval runner starts the system under test (SUT) as a process and talks JSONL over stdin/stdout.

## IO contract

- **stdin**: one golden case JSON object per line. The runner strips `expected` and `split` before sending; the SUT receives `id`, `skill`, `layer`, `job`, `difficulty`, `source`, `tags`, `input`, and `context` (frozen `now`, IANA `timezone`, opaque fixture refs).
- **stdout**: one trace JSON object per line, same order as stdin.
- **stderr**: free-form logs; not scored.

## Trace shape

Required fields: `case_id`, `route`, `tool_calls`, `drafts`, `escalation`, `executed_side_effects`, `steps`, `latency_ms`, `peak_rss_bytes`. Optional: `answer`, `error`.

Example (calendar happy path; tool args match E-02 schemas):

```json
{
  "case_id": "cal-0042",
  "route": {"label": "calendar", "confidence": 0.93},
  "tool_calls": [{"name": "find_free_slot", "args": {"duration_min": 30, "after": "2026-11-23T09:00:00+01:00"}}],
  "drafts": [{"kind": "event", "payload": {}}],
  "escalation": {"escalated": false, "reason": null, "payload_bytes": 0},
  "executed_side_effects": [],
  "steps": 2,
  "latency_ms": 840,
  "peak_rss_bytes": 2310000000
}
```

## Notes

- Drafts are proposals for approval, not executed side effects.
- `find_free_slot` uses `duration_min` / `after` (not legacy `duration_minutes` / `from`).
- Model-only and agent-core adapters must emit this same trace so scores stay comparable.
- CI and local `evals/ci.sh` set `DONNA_SEED=0` and `DONNA_TEMPERATURE=0`. Stochastic SUTs should read these so runs stay comparable across machines and commits.

