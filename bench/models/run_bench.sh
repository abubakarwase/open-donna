#!/usr/bin/env bash
# Speed (llama-bench), memory (llama-server RSS), quality (golden via OpenAI API).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BENCH="$(cd "$ROOT/.." && pwd)"
GGUF="$ROOT/gguf"
RES="$BENCH/results"
RAW="$RES/raw"
mkdir -p "$RES" "$RAW" "$RES/speed" "$RES/memory"
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:${PATH}"

PORT="${BENCH_PORT:-8765}"
CTX=4096
BUDGET_BYTES=$((3500 * 1024 * 1024))

# id|repo|file|role
MODELS=(
  "qwen35-0.8b|unsloth/Qwen3.5-0.8B-GGUF|Qwen3.5-0.8B-Q4_K_M.gguf|base"
  "qwen35-2b|unsloth/Qwen3.5-2B-GGUF|Qwen3.5-2B-Q4_K_M.gguf|base"
  "qwen35-4b|unsloth/Qwen3.5-4B-GGUF|Qwen3.5-4B-Q4_K_M.gguf|base"
  "gemma4-e2b|unsloth/gemma-4-E2B-it-GGUF|gemma-4-E2B-it-Q4_K_M.gguf|base"
  "gemma4-e4b|unsloth/gemma-4-E4B-it-GGUF|gemma-4-E4B-it-Q4_K_M.gguf|base"
  "phi4-mini|unsloth/Phi-4-mini-instruct-GGUF|Phi-4-mini-instruct-Q4_K_M.gguf|base"
  "functiongemma-270m|unsloth/functiongemma-270m-it-GGUF|functiongemma-270m-it-Q4_K_M.gguf|router"
)

uv_py() {
  if [[ -d "$ROOT/.venv" ]]; then
    "$ROOT/.venv/bin/python" "$@"
  else
    python3 "$@"
  fi
}

ensure_venv() {
  if [[ ! -d "$ROOT/.venv" ]]; then
    uv venv "$ROOT/.venv"
    uv pip install --python "$ROOT/.venv/bin/python" duckdb httpx
  fi
}

mean_std() {
  # stdin: one number per line -> mean std
  uv_py - <<'PY'
import sys, statistics
xs=[float(l) for l in sys.stdin if l.strip()]
if not xs:
  print("null null")
elif len(xs)==1:
  print(f"{xs[0]} 0")
else:
  print(f"{statistics.mean(xs)} {statistics.stdev(xs)}")
PY
}

peak_rss_kb() {
  local pid="$1"
  local max=0
  local i
  for i in $(seq 1 120); do
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    local rss
    rss=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ' || echo 0)
    if [[ -n "$rss" && "$rss" -gt "$max" ]]; then max=$rss; fi
    sleep 0.25
  done
  echo "$max"
}

wait_healthy() {
  local url="http://127.0.0.1:${PORT}/health"
  local i
  for i in $(seq 1 180); do
    if curl -sf "$url" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  return 1
}

kill_port() {
  local pids
  pids=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    kill $pids 2>/dev/null || true
    sleep 1
    kill -9 $pids 2>/dev/null || true
  fi
}

run_speed() {
  local id="$1" file="$2"
  local path="$GGUF/$file"
  local out="$RES/speed/${id}.json"
  echo "SPEED $id"
  # llama-bench: -p 512 -n 128, 5 reps each; parse tok/s
  local tmp
  tmp=$(mktemp)
  llama-bench -m "$path" -p 512 -n 128 -r 5 -o json 2>"$RES/speed/${id}.stderr" | tee "$tmp" >"$out" || true
  if [[ ! -s "$out" ]]; then
    llama-bench -m "$path" -p 512 -n 128 -r 5 >"$RES/speed/${id}.txt" 2>&1 || true
  fi
  uv_py "$ROOT/parse_speed.py" "$out" "$RES/speed/${id}.parsed.json" "$id"
}

run_memory_and_quality() {
  local id="$1" file="$2" role="$3" repo="$4"
  local path="$GGUF/$file"
  local mem_out="$RES/memory/${id}.json"
  echo "MEMORY+QUALITY $id"

  kill_port
  local loads=()
  local i
  for i in 1 2 3; do
    kill_port
    local t0 t1 load_s
    t0=$(python3 -c 'import time; print(time.time())')
    llama-server -m "$path" -c "$CTX" --port "$PORT" --jinja -np 1 --reasoning off --reasoning-budget 0 >"$RES/memory/${id}.server.${i}.log" 2>&1 &
    local spid=$!
    if wait_healthy; then
      t1=$(python3 -c 'import time; print(time.time())')
      load_s=$(python3 -c "print(($t1)-($t0))")
      loads+=("$load_s")
    else
      loads+=("null")
    fi
    kill "$spid" 2>/dev/null || true
    wait "$spid" 2>/dev/null || true
    kill_port
  done

  # peak RSS: start server, sample during idle + quality run
  kill_port
  local t0 t1 load_final
  t0=$(python3 -c 'import time; print(time.time())')
  llama-server -m "$path" -c "$CTX" --port "$PORT" --jinja -np 1 --reasoning off --reasoning-budget 0 >"$RES/memory/${id}.server.final.log" 2>&1 &
  local spid=$!
  if ! wait_healthy; then
    echo "FAIL healthy $id" >&2
    kill "$spid" 2>/dev/null || true
    kill_port
    echo "{\"id\":\"$id\",\"error\":\"server_not_healthy\"}" >"$mem_out"
    return 1
  fi
  t1=$(python3 -c 'import time; print(time.time())')
  load_final=$(python3 -c "print(($t1)-($t0))")

  # sample RSS in background while quality runs
  local rss_file
  rss_file=$(mktemp)
  (
    max=0
    while kill -0 "$spid" 2>/dev/null; do
      rss=$(ps -o rss= -p "$spid" 2>/dev/null | tr -d ' ' || echo 0)
      if [[ -n "$rss" && "$rss" -gt "$max" ]]; then max=$rss; echo "$max" >"$rss_file"; fi
      sleep 0.2
    done
  ) &
  local mon=$!

  local raw_out="$RAW/${id}.jsonl"
  local flags='["--jinja","-c 4096","--reasoning-budget 0","temp 0"]'
  BENCH_MODEL_ID="$id" BENCH_ROLE="$role" BENCH_PORT="$PORT" BENCH_RAW="$raw_out" \
    BENCH_GOLDEN="$ROOT/golden.jsonl" \
    uv_py "$ROOT/run_quality.py" || true

  sleep 1
  kill "$mon" 2>/dev/null || true
  wait "$mon" 2>/dev/null || true
  local peak_kb=0
  if [[ -f "$rss_file" ]]; then peak_kb=$(cat "$rss_file"); fi
  # one more read
  local now
  now=$(ps -o rss= -p "$spid" 2>/dev/null | tr -d ' ' || echo 0)
  if [[ "${now:-0}" -gt "$peak_kb" ]]; then peak_kb=$now; fi

  kill "$spid" 2>/dev/null || true
  wait "$spid" 2>/dev/null || true
  kill_port
  rm -f "$rss_file"

  local size_bytes
  size_bytes=$(stat -f%z "$path")
  local peak_bytes=$((peak_kb * 1024))
  local fits=false
  if [[ "$peak_bytes" -le "$BUDGET_BYTES" ]]; then fits=true; fi

  LOADS_JSON=$(printf '%s\n' "${loads[@]}" | uv_py -c 'import sys,json; print(json.dumps([None if x.strip()=="null" else float(x) for x in sys.stdin]))')
  FITS_JSON=$([ "$fits" = true ] && echo true || echo false)
  MEM_OUT="$mem_out" MODEL_ID="$id" SIZE_BYTES="$size_bytes" PEAK_KB="$peak_kb" PEAK_BYTES="$peak_bytes" \
  FITS_JSON="$FITS_JSON" LOADS_JSON="$LOADS_JSON" LOAD_FINAL="$load_final" uv_py - <<'PY'
import json, statistics, pathlib, os
loads = json.loads(os.environ["LOADS_JSON"])
nums=[x for x in loads if x is not None]
median = statistics.median(nums) if nums else None
out={
  "id": os.environ["MODEL_ID"],
  "gguf_bytes": int(os.environ["SIZE_BYTES"]),
  "peak_rss_kb": int(os.environ["PEAK_KB"]),
  "peak_rss_bytes": int(os.environ["PEAK_BYTES"]),
  "fits_3_5gb": os.environ["FITS_JSON"] == "true",
  "load_time_s_runs": loads,
  "load_time_s_median": median,
  "load_time_s_final": float(os.environ["LOAD_FINAL"]),
  "llama_server_flags": ["--jinja","-c","4096","--reasoning","off","--reasoning-budget","0","-np","1"],
}
path = pathlib.Path(os.environ["MEM_OUT"])
path.write_text(json.dumps(out, indent=2))
print(json.dumps(out, indent=2))
PY
}

aggregate_all() {
  uv_py "$ROOT/aggregate_models.py"
}

ensure_venv
kill_port

for entry in "${MODELS[@]}"; do
  IFS='|' read -r id repo file role <<<"$entry"
  if [[ ! -f "$GGUF/$file" ]]; then
    echo "MISSING $file — skip measurements" >&2
    continue
  fi
  run_speed "$id" "$file"
  run_memory_and_quality "$id" "$file" "$role" "$repo"
done

uv_py "$ROOT/aggregate_models.py" || {
  echo "aggregate_models.py failed" >&2
  exit 1
}
echo "MODEL BENCH DONE"
