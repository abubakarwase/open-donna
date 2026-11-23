# Core API (C-01)

Public surface in `donna-core`. No I/O; connectors and UI call into these types.

## Agent

```rust
trait Agent {
    fn run(&self, input: &Input, ctx: &Context, events: &mut dyn EventSink) -> Trace;
}
```

`StubAgent` always routes `general` / confidence `0.0`, no tools. Real routing lands later.

## Events

`AgentEvent`: `Routed`, `ToolCalled`, `ToolResult`, `DraftReady`, `EscalationRequested`, `NeedsApproval`, `Done`.

`EventSink::emit` streams progress. Built-ins: `NoopSink`, `VecSink`.

## Approval

`ApprovalToken` is a newtype only this crate can construct (`pub(crate)` constructor). `Approver::approve` returns one. Side-effect tools produce a `Draft` with required `id`; the agent emits `NeedsApproval { draft_id }` and never calls execute. `ToolRegistry::execute(draft_id, &ApprovalToken)` records the side effect only after a real approver mints a token. `AlwaysApprover` is gated behind the `test-approver` feature (and `cfg(test)`).

## Untrusted

`Untrusted<T>` marks third-party strings (mail subjects/bodies, invite titles). The inner field is private; use `as_display` / `into_inner_for_display` for UI/tests only. Prompt paths must go through `fence_untrusted` (random per-request boundary). Serde is transparent (plain JSON strings).

## Call sites

**Tauri (M3):** deserialize UI payload to `Input` + `Context`, call `agent.run(..., &mut sink)`, serialize `Trace` (or stream `AgentEvent`) back to the frontend.

**`donna-sut`:** long-lived process. Stdin: one case JSON per line (`id`, `input`, `context`, plus skill metadata). Runs `StubAgent` with `NoopSink`, sets `case_id` / `latency_ms` / `peak_rss_bytes`, writes one trace JSON line to stdout. Logs on stderr only.

## Eval command

Build: `cargo build -p donna-sut` from repo root → `target/debug/donna-sut`.

From `evals/`:

```bash
uv run donna-evals run --sut "../target/debug/donna-sut" --suite all --split dev --out /tmp/donna-stub
```

From repo root, use `--sut "target/debug/donna-sut"` and run via `cd evals && ...` as above (runner cwd is `evals/`).
