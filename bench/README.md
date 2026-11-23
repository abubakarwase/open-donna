# Bench — model fit (8 GB budget) + UI A/B

All work lives under `bench/`. Numbers in `results/` are measured; regenerate with the commands below.

## Prerequisites

```bash
brew install llama.cpp duckdb cliclick
# pnpm, Rust stable + nightly (GPUI needs nightly for this crate pin), uv
uv tool install huggingface_hub
```

## Models

```bash
cd bench/models
./download.sh
# creates .venv + deps on first quality run
./run_bench.sh
# writes results/speed/*, results/memory/*, results/raw/<id>.jsonl, results/models.json
```

## UI

```bash
cd bench/ui/tauri-shadcn && pnpm install && pnpm tauri build --bundles app
cd bench/ui/gpui-component && cargo +nightly build --release

export TAURI_APP="$PWD/../tauri-shadcn/src-tauri/target/release/bundle/macos/tauri-shadcn.app"
# from bench/:
export TAURI_APP="bench/ui/tauri-shadcn/src-tauri/target/release/bundle/macos/tauri-shadcn.app"
export TAURI_BIN="$TAURI_APP/Contents/MacOS/tauri-shadcn"
export GPUI_BIN="bench/ui/gpui-component/target/release/gpui-bench"
python3 bench/ui/harness/measure_ui.py
# or: bash bench/ui/harness/run_ui_bench.sh
```

## Docs from measured JSON

```bash
python3 bench/models/write_results_md.py
# -> bench/results/RESULTS.md
```

Machine note: Apple M1 Pro 16 GB. Fits 8 GB := peak RSS ≤ 3.5 GB.
