#!/usr/bin/env bash
# M1: stratified test-split agent eval (donna-sut + Qwen3.5-2B). No judge / rubrics.
set -euo pipefail

EVALS="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$EVALS/.." && pwd)"
cd "$EVALS"

export DONNA_SEED=0
export DONNA_TEMPERATURE=0
export DONNA_ALLOW_TEST_SPLIT=1
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:${PATH}"

TIMEOUT="${M1_TIMEOUT:-180}"
LIMIT_PER_SUITE="${M1_LIMIT_PER_SUITE:-}"
REPORTS_M1="$EVALS/reports-m1"
MODEL_ARG="${M1_MODEL:-Qwen3.5-2B}"
PORT="${M1_PORT:-18775}"
mkdir -p "$REPORTS_M1" reports

# Prefer release binary; fall back to cargo run --release.
if [[ -x "$REPO/target/release/donna-sut" ]]; then
  SUT_BIN="$REPO/target/release/donna-sut"
else
  SUT_BIN="cargo run --release -p donna-sut --"
fi

kill_leftover_servers() {
  local pids
  pids="$(pgrep -x llama-server 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "killing leftover llama-server: $pids" >&2
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
}

merge_suite_scorecards() {
  local out_dir="$1"
  local tag="$2"
  OUT_DIR="$out_dir" LIMIT_PER_SUITE="${LIMIT_PER_SUITE}" uv run python - <<'PY'
from pathlib import Path
import json
import os

out = Path(os.environ["OUT_DIR"])
limit = int(os.environ.get("LIMIT_PER_SUITE") or "0") or None
suites = ["router", "email", "calendar", "sheet", "safety"]
cards = []
for suite in suites:
    p = out / suite / "scorecard.json"
    cards.append(json.loads(p.read_text(encoding="utf-8")))

def merge_metrics(cards):
    keys = set()
    for c in cards:
        keys.update((c.get("metrics") or {}).keys())
    merged = {}
    for key in sorted(keys):
        entries = [(c["metrics"][key], c) for c in cards if key in (c.get("metrics") or {})]
        if not entries:
            continue
        if all(e[0].get("status") for e in entries) and all(e[0].get("value") is None for e in entries):
            merged[key] = dict(entries[0][0])
            continue
        if key in ("peak_rss_bytes_observed", "peak_rss_bytes_max") or key.endswith("_max"):
            vals = [e[0]["value"] for e in entries if e[0].get("value") is not None]
            n = sum(int(e[0].get("n") or 0) for e in entries)
            merged[key] = {**entries[0][0], "value": max(vals) if vals else None, "n": n}
            continue
        if key.endswith("_p50") or key.endswith("_p95"):
            num = 0.0
            den = 0
            thr = None
            for e, _c in entries:
                if e.get("value") is None:
                    continue
                n = int(e.get("n") or 0)
                num += float(e["value"]) * max(n, 1)
                den += max(n, 1)
                thr = e.get("threshold") or thr
            merged[key] = {
                "value": (num / den) if den else None,
                "n": den,
                "threshold": thr,
                "pass": None,
                "note": "approx_weighted_suite_percentile",
            }
            continue
        num = 0.0
        den = 0
        thr = None
        status = None
        for e, _c in entries:
            if e.get("value") is None:
                status = e.get("status") or status
                continue
            n = int(e.get("n") or 0)
            num += float(e["value"]) * n
            den += n
            thr = e.get("threshold") or thr
        if den == 0:
            merged[key] = {"value": None, "n": 0, "status": status or "skipped"}
            continue
        val = num / den
        entry = {"value": val, "n": den, "threshold": thr}
        if thr and thr.get("target") is not None:
            op = thr.get("op", ">=")
            t = thr["target"]
            if op == ">=":
                entry["pass"] = val >= t
            elif op == "<=":
                entry["pass"] = val <= t
            elif op == "==":
                entry["pass"] = val == t
        merged[key] = entry
    return merged

scorecard = {
    "metrics": merge_metrics(cards),
    "by_suite": {
        s: json.loads((out / s / "scorecard.json").read_text())["metrics"] for s in suites
    },
    "meta": {
        "merged_from": suites,
        "limit_per_suite": limit,
        "stratified": True,
        "sut": "donna-sut",
        "model": "Qwen3.5-2B",
    },
}
(out / "scorecard.json").write_text(json.dumps(scorecard, indent=2) + "\n", encoding="utf-8")
print("merged scorecard", out / "scorecard.json")
PY
  cp "$out_dir/scorecard.json" "$REPORTS_M1/${tag}-scorecard.json"
}

run_full_stratified() {
  local tag="$1"
  local model="$2"
  local port="$3"
  local out="reports/m1-${tag}"
  mkdir -p "$out"
  echo "=== m1 ${tag} stratified limit=${LIMIT_PER_SUITE}/suite (donna-sut, route/job balanced) ==="
  local suite
  for suite in router email calendar sheet safety; do
    kill_leftover_servers
    echo "--- ${tag} suite=${suite} ---"
    # One port; server stays up for the suite process (idle_unload long in SUT).
    # Port arg reserved for future Config.port wiring; SUT currently picks a free port.
    echo "(port hint ${port}; donna-sut uses free local port)" >&2
    SUT_CMD="${SUT_BIN} --model ${model}" \
    SUITE="$suite" SPLIT=test LIMIT="$LIMIT_PER_SUITE" TIMEOUT_S="$TIMEOUT" \
    OUT_DIR="$out/$suite" \
    uv run python - <<'PY'
"""True stratified sample (≥limit) then score via runner internals (not cases[:N])."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from donna_evals.runner import (
    EVALS_ROOT,
    _load_sidecars,
    _load_tool_schemas,
    _public_trace,
    _run_cases,
    load_cases,
)
from donna_evals.scorecard import build_scorecard, write_scorecard

suite = os.environ["SUITE"]
limit = int(os.environ["LIMIT"])
timeout_s = float(os.environ["TIMEOUT_S"])
sut = os.environ["SUT_CMD"]
out_dir = Path(os.environ["OUT_DIR"])
out_dir.mkdir(parents=True, exist_ok=True)

all_cases = load_cases(suite, os.environ.get("SPLIT", "test"), limit=None)
sidecars = _load_sidecars()
slot_ids = set(sidecars.get("slot_truth") or {})
triage_ids = set(sidecars.get("triage_truth") or {})


def _bucket_key(case: dict) -> str:
    if suite == "router":
        return str((case.get("expected") or {}).get("route") or case.get("job") or "other")
    return str(case.get("job") or "other")


def stratified(cases: list[dict], n: int) -> list[dict]:
    """Round-robin across buckets; pin escalate-gold / slot / triage when present."""
    by: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by[_bucket_key(c)].append(c)

    pinned: list[dict] = []
    seen: set[str] = set()

    def take(c: dict) -> None:
        cid = c["id"]
        if cid in seen:
            return
        seen.add(cid)
        pinned.append(c)

    if suite == "router":
        for c in cases:
            if (c.get("expected") or {}).get("escalation") == "escalate" or (
                c.get("expected") or {}
            ).get("route") == "escalate":
                take(c)
                if sum(
                    1
                    for x in pinned
                    if (x.get("expected") or {}).get("route") == "escalate"
                ) >= max(8, n // 8):
                    break
    elif suite == "calendar":
        for c in cases:
            if c["id"] in slot_ids:
                take(c)
                if sum(1 for x in pinned if x["id"] in slot_ids) >= max(8, n // 6):
                    break
    elif suite == "email":
        for c in cases:
            if c["id"] in triage_ids:
                take(c)
                if sum(1 for x in pinned if x["id"] in triage_ids) >= max(8, n // 6):
                    break

    keys = sorted(by.keys())
    idxs = {k: 0 for k in keys}
    while len(pinned) < n and keys:
        progress = False
        for k in keys:
            bucket = by[k]
            i = idxs[k]
            while i < len(bucket) and bucket[i]["id"] in seen:
                i += 1
            idxs[k] = i
            if i >= len(bucket):
                continue
            take(bucket[i])
            idxs[k] = i + 1
            progress = True
            if len(pinned) >= n:
                break
        if not progress:
            break
    if len(pinned) < n:
        for c in cases:
            take(c)
            if len(pinned) >= n:
                break
    return pinned[: max(n, len(pinned))]


selected = stratified(all_cases, limit)
assert len(selected) >= min(limit, len(all_cases)), (len(selected), limit, len(all_cases))
if suite == "router":
    routes = {(c.get("expected") or {}).get("route") for c in selected}
    assert "escalate" in routes, routes
    assert len(routes) >= 4, routes
if suite == "calendar":
    assert sum(1 for c in selected if c["id"] in slot_ids) >= 1
if suite == "email":
    assert sum(1 for c in selected if c["id"] in triage_ids) >= 1

print(
    f"stratified {suite}: n={len(selected)} buckets="
    + str(sorted({_bucket_key(c) for c in selected})),
    flush=True,
)

traces = _run_cases(sut, selected, timeout_s)
with (out_dir / "traces.jsonl").open("w", encoding="utf-8") as fh:
    for tr in traces:
        fh.write(json.dumps(_public_trace(tr), separators=(",", ":")) + "\n")

ctx = {
    "tool_schemas": _load_tool_schemas(),
    "slot_truth": sidecars["slot_truth"],
    "triage_truth": sidecars["triage_truth"],
    "judge_url": None,
    "judge_model": None,
    "evals_root": EVALS_ROOT,
}
scorecard = build_scorecard(selected, traces, ctx)
scorecard.setdefault("meta", {})
scorecard["meta"]["stratified"] = True
scorecard["meta"]["limit"] = limit
scorecard["meta"]["sampling"] = "bucket_round_robin_not_first_n"
scorecard["meta"]["sut"] = "donna-sut"
write_scorecard(out_dir, scorecard)
print(f"wrote {out_dir / 'scorecard.json'}", flush=True)
PY
    kill_leftover_servers
  done
  merge_suite_scorecards "$out" "$tag"
}

WALL0=$(date +%s)
kill_leftover_servers

if [[ -z "$LIMIT_PER_SUITE" ]]; then
  LIMIT_PER_SUITE=60
fi
export LIMIT_PER_SUITE

# Ensure release binary exists for long runs.
if [[ ! -x "$REPO/target/release/donna-sut" ]]; then
  (cd "$REPO" && cargo build --release -p donna-sut)
  SUT_BIN="$REPO/target/release/donna-sut"
fi

run_full_stratified "qwen35-2b" "$MODEL_ARG" "$PORT"

kill_leftover_servers
if pgrep -x llama-server >/dev/null 2>&1; then
  echo "ERROR: llama-server still running" >&2
  pgrep -x llama-server >&2 || true
  exit 1
fi

WALL1=$(date +%s)
{
  echo "M1_WALL_SECONDS=$((WALL1 - WALL0))"
  echo "LIMIT_PER_SUITE=${LIMIT_PER_SUITE}"
  echo "STRATIFIED=1"
  echo "SUT=donna-sut"
  echo "MODEL=${MODEL_ARG}"
} | tee "$REPORTS_M1/wall.txt"
echo "m1 done"
