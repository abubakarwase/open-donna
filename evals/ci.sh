#!/usr/bin/env bash
# Local/CI entry point for donna-evals: check | dev | test
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EVALS_DIR="$SCRIPT_DIR"
REPO_ROOT="$(cd "$EVALS_DIR/.." && pwd)"

export DONNA_SEED=0
export DONNA_TEMPERATURE=0

usage() {
  cat <<'EOF'
Usage: bash evals/ci.sh <check|dev|test> [options]

  check              Sync deps, pytest, validate cases, rebuild fixtures/golden
  dev [--sut CMD]    Run all suites on split=dev (default SUT: oracle)
  test --sut CMD     Run all suites on split=test (requires DONNA_ALLOW_TEST_SPLIT=1)
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

check_gates() {
  local out_dir="$1"
  local scorecard="$EVALS_DIR/$out_dir/scorecard.json"
  local thresholds="$EVALS_DIR/spec/thresholds.yaml"
  if [[ ! -f "$scorecard" ]]; then
    die "scorecard missing: $scorecard"
  fi
  cd "$EVALS_DIR"
  uv run python -c "
import json, sys
from pathlib import Path
import yaml

scorecard = json.loads(Path(sys.argv[1]).read_text())
thresholds = yaml.safe_load(Path(sys.argv[2]).read_text()) or {}
metrics_th = thresholds.get('metrics') or {}
scored = scorecard.get('metrics') or {}
failed = []
for name, th in metrics_th.items():
    if not isinstance(th, dict) or not th.get('gate'):
        continue
    entry = scored.get(name) or {}
    if entry.get('pass') is False:
        failed.append(name)
if failed:
    print('gate failure: ' + ', '.join(failed), file=sys.stderr)
    sys.exit(1)
" "$scorecard" "$thresholds"
}

cmd_check() {
  cd "$EVALS_DIR"
  uv sync
  uv run pytest -q

  local cases=()
  local suite
  for suite in router email calendar sheet safety; do
    cases+=("golden/${suite}/dev.jsonl" "golden/${suite}/test.jsonl")
  done
  uv run donna-evals-validate cases "${cases[@]}"

  # Snapshot porcelain before rebuild so pre-existing dirt is not a false fail.
  local before after
  before="$(cd "$REPO_ROOT" && git status --porcelain evals/fixtures evals/golden || true)"

  uv run python fixtures/build.py
  for suite in router email calendar sheet safety judge_calibration; do
    if [[ -f "golden/${suite}/build.py" ]]; then
      uv run python "golden/${suite}/build.py"
    fi
  done

  after="$(cd "$REPO_ROOT" && git status --porcelain evals/fixtures evals/golden || true)"
  if [[ "$after" != "$before" ]]; then
    echo "error: rebuild changed evals/fixtures or evals/golden (non-deterministic generators)" >&2
    echo "--- before ---" >&2
    echo "$before" >&2
    echo "--- after ---" >&2
    echo "$after" >&2
    exit 1
  fi
}

parse_sut() {
  SUT=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --sut)
        shift
        [[ $# -gt 0 ]] || die "--sut requires a command"
        SUT="$1"
        shift
        ;;
      --sut=*)
        SUT="${1#--sut=}"
        shift
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done
}

cmd_dev() {
  parse_sut "$@"
  if [[ -z "$SUT" ]]; then
    SUT="uv run python suts/oracle.py"
  fi
  cd "$EVALS_DIR"
  uv run donna-evals run --sut "$SUT" --suite all --split dev --out reports/ci-dev
  check_gates reports/ci-dev
}

cmd_test() {
  parse_sut "$@"
  [[ -n "$SUT" ]] || die "test requires --sut CMD"
  if [[ "${DONNA_ALLOW_TEST_SPLIT:-}" != "1" ]]; then
    echo "error: test split is gated; set DONNA_ALLOW_TEST_SPLIT=1 to run" >&2
    exit 1
  fi
  cd "$EVALS_DIR"
  uv run donna-evals run --sut "$SUT" --suite all --split test --out reports/ci-test
  check_gates reports/ci-test
}

main() {
  if [[ $# -lt 1 ]]; then
    usage
    exit 1
  fi
  local cmd="$1"
  shift
  case "$cmd" in
    check) cmd_check "$@" ;;
    dev) cmd_dev "$@" ;;
    test) cmd_test "$@" ;;
    -h|--help|help) usage ;;
    *)
      usage
      die "unknown subcommand: $cmd"
      ;;
  esac
}

main "$@"
