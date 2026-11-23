# donna-evals

Local eval harness for OpenDonna (router, email, calendar, sheet, safety).

## Layout

- `spec/` — case/trace schemas, tool contracts, thresholds, rubrics
- `golden/` — golden case JSONL (`dev` / `test` per suite)
- `fixtures/` — mailbox, calendar, sheet fixtures used by cases
- `donna_evals/` — runner, scorers, scorecard, CLI
- `suts/` — reference SUTs (oracle, null)
- `ci.sh` — local/CI entry point

## Run

From repo root (cwd for `uv` is handled inside the script):

```bash
bash evals/ci.sh check          # sync, pytest, validate, rebuild + determinism
bash evals/ci.sh dev            # all suites, split=dev, default oracle SUT
bash evals/ci.sh dev --sut 'uv run python suts/oracle.py'
```

`test` is on-demand only. Set `DONNA_ALLOW_TEST_SPLIT=1` or CI refuses:

```bash
DONNA_ALLOW_TEST_SPLIT=1 bash evals/ci.sh test --sut 'uv run python suts/oracle.py'
```

Policy: run `dev` on every change; reserve `test` for gates / explicit runs. CI sets `DONNA_SEED=0` and `DONNA_TEMPERATURE=0` (see `spec/sut-protocol.md`). Thresholds and optional `gate: true` metrics live in `spec/thresholds.yaml`.
