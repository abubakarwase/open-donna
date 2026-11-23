#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="/opt/homebrew/bin:${HOME}/.cargo/bin:${PATH}"

TAURI_DIR="$ROOT/ui/tauri-shadcn"
GPUI_DIR="$ROOT/ui/gpui-component"

echo "Building Tauri release..."
(cd "$TAURI_DIR" && pnpm tauri build --bundles app 2>&1 | tee "$ROOT/results/tauri-build.log")

echo "Building GPUI release..."
(cd "$GPUI_DIR" && cargo build --release 2>&1 | tee "$ROOT/results/gpui-build.log")

# Locate binaries
TAURI_APP=$(find "$TAURI_DIR/src-tauri/target/release/bundle/macos" -name "*.app" -maxdepth 1 | head -1)
TAURI_BIN="$TAURI_APP/Contents/MacOS/tauri-shadcn"
if [[ ! -x "$TAURI_BIN" ]]; then
  TAURI_BIN=$(find "$TAURI_DIR/src-tauri/target/release" -maxdepth 1 -type f -perm +111 -name 'tauri-shadcn' | head -1)
fi
GPUI_BIN="$GPUI_DIR/target/release/gpui-bench"

export TAURI_APP TAURI_BIN GPUI_BIN
echo "TAURI_APP=$TAURI_APP"
echo "TAURI_BIN=$TAURI_BIN"
echo "GPUI_BIN=$GPUI_BIN"

python3 "$ROOT/ui/harness/measure_ui.py"
echo "UI BENCH DONE"
