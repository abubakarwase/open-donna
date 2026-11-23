#!/usr/bin/env bash
# Download pinned Q4_K_M GGUFs into bench/models/gguf/ and print SHA256.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/gguf"
mkdir -p "$OUT"
export PATH="${HOME}/.local/bin:${PATH}"

download_one() {
  local repo="$1" file="$2"
  local dest="$OUT/$file"
  if [[ -f "$dest" ]]; then
    echo "EXISTS $file"
  else
    echo "DOWNLOAD $repo/$file"
    hf download "$repo" "$file" --local-dir "$OUT"
  fi
  shasum -a 256 "$dest" | awk -v f="$file" '{print "SHA256", f, $1}'
}

download_one "unsloth/Qwen3.5-0.8B-GGUF" "Qwen3.5-0.8B-Q4_K_M.gguf"
download_one "unsloth/Qwen3.5-2B-GGUF" "Qwen3.5-2B-Q4_K_M.gguf"
download_one "unsloth/Qwen3.5-4B-GGUF" "Qwen3.5-4B-Q4_K_M.gguf"
download_one "unsloth/gemma-4-E2B-it-GGUF" "gemma-4-E2B-it-Q4_K_M.gguf"
download_one "unsloth/gemma-4-E4B-it-GGUF" "gemma-4-E4B-it-Q4_K_M.gguf"
download_one "unsloth/Phi-4-mini-instruct-GGUF" "Phi-4-mini-instruct-Q4_K_M.gguf"
download_one "unsloth/functiongemma-270m-it-GGUF" "functiongemma-270m-it-Q4_K_M.gguf"

echo "DONE"
