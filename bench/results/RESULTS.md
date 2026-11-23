# Bench RESULTS

## Environment

- Machine: Apple M1 Pro, 16 GB RAM, macOS 27.2.1
- There is no 8 GB machine. **Fits 8 GB** := measured peak RSS of the base model process ≤ 3.5 GB.
- llama.cpp: `version: 1.0.0 (build 12359, commit b1b770d90)
built with AppleClang 21.0.0.21000401 for Darwin arm64`
- Quant: GGUF Q4_K_M; context 4096; Metal on
- UI B dependency: `gpui-kit` 0.10.0 (re-exports GPUI + gpui-component 0.10.0)

## Method

- Speed: `llama-bench -p 512 -n 128 -r 5`; report mean ± std of sample tok/s
- Memory: peak RSS of `llama-server` during golden run (`ps` RSS samples); load time median of 3 (start → `/health`)
- Quality: `golden.jsonl` (40 cases) via OpenAI-compatible API; temp 0; flags from `results/memory/*.json` (`--jinja -c 4096 --reasoning-budget 0 -np 1`)
- Sheet score: DuckDB execution match vs `fixtures/sales.csv`
- UI: 5 runs; median and min/max
- UI RSS method: total RSS = sum of RSS for (1) the launched process and all descendants via ppid BFS, plus (2) WebKit WebContent/Networking/GPU PIDs that exist after launch but were absent in a pre-launch snapshot. Unrelated pre-existing WebKit processes are excluded.

## Models

| id | repo | file | sha256 | gguf_bytes | pp512 tok/s | tg128 tok/s | peak_rss_bytes | fits_3.5GB | load_s_median | router_acc | tool_name_acc | tool_arg_acc | schema_valid | sql_exec | mean_lat_ms | role | status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen35-0.8b | unsloth/Qwen3.5-0.8B-GGUF | Qwen3.5-0.8B-Q4_K_M.gguf | bd258782e35f7f45… | 532517120 | 2333.0 ± 19.7 | 134.6 ± 3.9 | 1045184512 | True | 1.100898027420044 | 0.600 | 0.800 | 0.533 | 0.800 | 0.700 | 447.2 | base | measured |
| qwen35-2b | unsloth/Qwen3.5-2B-GGUF | Qwen3.5-2B-Q4_K_M.gguf | aaf42c8b7c3cab2b… | 1280835840 | 1094.3 ± 5.1 | 81.5 ± 0.4 | 1703084032 | True | 1.1064348220825195 | 0.467 | 1.000 | 0.733 | 1.000 | 0.800 | 588.1 | base | measured |
| qwen35-4b | unsloth/Qwen3.5-4B-GGUF | Qwen3.5-4B-Q4_K_M.gguf | 00fe7986ff5f6b46… | 2740937888 | 412.6 ± 8.6 | 37.6 ± 0.2 | 3476045824 | True | 1.1204628944396973 | 0.800 | 1.000 | 0.733 | 1.000 | 0.900 | 1252.0 | base | measured |
| gemma4-e2b | unsloth/gemma-4-E2B-it-GGUF | gemma-4-E2B-it-Q4_K_M.gguf | 740185b21d22ceb8… | 3106738272 | 1974.3 ± 75.0 | 60.3 ± 1.0 | 2933948416 | True | 1.6534316539764404 | 0.667 | 0.933 | 0.667 | 0.933 | 0.800 | 1374.6 | base | measured |
| gemma4-e4b | unsloth/gemma-4-E4B-it-GGUF | gemma-4-E4B-it-Q4_K_M.gguf | 85a896a047553e84… | 4977171584 | 564.7 ± 19.6 | 34.7 ± 0.7 | 4041261056 | False | 4.7982587814331055 | 0.867 | 0.933 | 0.667 | 0.933 | 0.700 | 2491.8 | base | measured |
| phi4-mini | unsloth/Phi-4-mini-instruct-GGUF | Phi-4-mini-instruct-Q4_K_M.gguf | 88c00229914083cd… | 2491874272 | 523.5 ± 0.5 | 43.7 ± 3.4 | 3162144768 | True | 1.6906681060791016 | 0.667 | 0.000 | 0.000 | 0.000 | 0.800 | 1896.9 | base | measured |
| functiongemma-270m | unsloth/functiongemma-270m-it-GGUF | functiongemma-270m-it-Q4_K_M.gguf | 7474135cf63b5de8… | 253127904 | 10926.7 ± 93.7 | 190.4 ± 21.2 | 455131136 | True | 0.6196930408477783 | 0.133 | not measured | not measured | not measured | not measured | 192.2 | router | measured |
| qwen36-le4b | unsloth/Qwen | — | — | — | — | — | — | — | — | — | — | — | — | — | — | base | not available: Qwen3.6 ≤4B GGUF not available on Hugging Face (checked unsloth/Qwen) |
| qwen38-le4b | unsloth/Qwen | — | — | — | — | — | — | — | — | — | — | — | — | — | — | base | not available: Qwen3.8 ≤4B GGUF not available on Hugging Face (checked unsloth/Qwen) |

## UI

### A = Tauri 2 + React + shadcn-style

- bundle_bytes: 5435392
- binary_bytes: 5326848
- cold_start_s: median=1.3436740000033751, min=0.6418897919356823, max=1.9376262910664082
- idle_rss_total_kb: median=210320, min=185856, max=223952
- idle_rss_largest_kb: median=93328, min=87584, max=105648
- idle_cpu_pct_mean_30s: median=0.025925925925925925, min=0.0, max=0.3423076923076923
- scroll_peak_rss_kb: median=222224, min=185856, max=223952
- scroll_cpu_pct_mean_10s: median=0.7764705882352941, min=0.54375, max=3.635294117647059

### B = GPUI + gpui-kit 0.10.0

- bundle_bytes: 17629184
- binary_bytes: 17628336
- cold_start_s: median=0.23964137502480298, min=0.21448104199953377, max=0.48163662489969283
- idle_rss_total_kb: median=86224, min=84720, max=93248
- idle_rss_largest_kb: median=86224, min=84720, max=93248
- idle_cpu_pct_mean_30s: median=0.0, min=0.0, max=0.0
- scroll_peak_rss_kb: median=102672, min=100320, max=105584
- scroll_cpu_pct_mean_10s: median=0.3, min=0.16666666666666666, max=0.4

## Component availability

| stack | date picker | multi-select | virtualised table |
|---|---|---|---|
| A_tauri_shadcn | yes: Radix Popover + react-day-picker DayPicker | yes: Radix Popover + cmdk Command (multi) | yes: TanStack Table + TanStack Virtual |
| B_gpui_kit | yes: gpui-component DatePicker | yes: gpui-component Combobox.multiple | yes: gpui-component DataTable |

Source files: `results/models.json`, `results/ui.json`, `results/raw/*.jsonl`, `results/speed/*`, `results/memory/*`.
