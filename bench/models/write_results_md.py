#!/usr/bin/env python3
"""Build RESULTS.md strictly from bench/results/*.json (and recorded env files)."""
from __future__ import annotations

import json
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "results"


def load(name):
    p = RES / name
    if not p.exists():
        return None
    return json.loads(p.read_text())


def fmt_rate(x):
    if x is None:
        return "not measured"
    return f"{x:.3f}"


def fmt_ms(x):
    if x is None:
        return "not measured"
    return f"{x:.1f}"


def fmt_tok(speed):
    if not speed or speed.get("mean") is None:
        return "not measured"
    return f"{speed['mean']:.1f} ± {speed.get('std') or 0:.1f}"


def main():
    models = load("models.json") or {}
    ui = load("ui.json") or {}
    llama = (RES / "llama-version.txt").read_text().strip() if (RES / "llama-version.txt").exists() else "not measured"

    lines = []
    lines.append("# Bench RESULTS")
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append("- Machine: Apple M1 Pro, 16 GB RAM, macOS 27.2.1")
    lines.append("- There is no 8 GB machine. **Fits 8 GB** := measured peak RSS of the base model process ≤ 3.5 GB.")
    lines.append(f"- llama.cpp: `{llama}`")
    lines.append("- Quant: GGUF Q4_K_M; context 4096; Metal on")
    lines.append("- UI B dependency: `gpui-kit` 0.10.0 (re-exports GPUI + gpui-component 0.10.0)")
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append("- Speed: `llama-bench -p 512 -n 128 -r 5`; report mean ± std of sample tok/s")
    lines.append("- Memory: peak RSS of `llama-server` during golden run (`ps` RSS samples); load time median of 3 (start → `/health`)")
    lines.append("- Quality: `golden.jsonl` (40 cases) via OpenAI-compatible API; temp 0; flags from `results/memory/*.json` (`--jinja -c 4096 --reasoning-budget 0 -np 1`)")
    lines.append("- Sheet score: DuckDB execution match vs `fixtures/sales.csv`")
    lines.append("- UI: 5 runs; median and min/max")
    ui_rss = (ui.get("rss_method") if isinstance(ui, dict) else None) or "not measured"
    lines.append(f"- UI RSS method: {ui_rss}")
    lines.append("")
    lines.append("## Models")
    lines.append("")
    lines.append("| id | repo | file | sha256 | gguf_bytes | pp512 tok/s | tg128 tok/s | peak_rss_bytes | fits_3.5GB | load_s_median | router_acc | tool_name_acc | tool_arg_acc | schema_valid | sql_exec | mean_lat_ms | role | status |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

    for m in models.get("models", []):
        if m.get("status") == "not available":
            lines.append(
                f"| {m['id']} | {m.get('hf_repo')} | — | — | — | — | — | — | — | — | — | — | — | — | — | — | {m.get('role')} | not available: {m.get('reason','')} |"
            )
            continue
        speed = m.get("speed") or {}
        mem = m.get("memory") or {}
        q = m.get("quality") if isinstance(m.get("quality"), dict) else {}
        sha = (m.get("sha256") or "")[:16] + "…" if m.get("sha256") else "not measured"
        lines.append(
            "| {id} | {repo} | {file} | {sha} | {gbytes} | {pp} | {tg} | {peak} | {fits} | {load} | {ra} | {tn} | {ta} | {sv} | {sql} | {lat} | {role} | {status} |".format(
                id=m.get("id"),
                repo=m.get("hf_repo"),
                file=m.get("file"),
                sha=sha,
                gbytes=m.get("gguf_bytes") or (mem.get("gguf_bytes") if isinstance(mem, dict) else None) or "not measured",
                pp=fmt_tok(speed.get("pp512_tok_s") if isinstance(speed, dict) else None),
                tg=fmt_tok(speed.get("tg128_tok_s") if isinstance(speed, dict) else None),
                peak=mem.get("peak_rss_bytes") if isinstance(mem, dict) else "not measured",
                fits=mem.get("fits_3_5gb") if isinstance(mem, dict) else "not measured",
                load=mem.get("load_time_s_median") if isinstance(mem, dict) else "not measured",
                ra=fmt_rate(q.get("router_accuracy")),
                tn=fmt_rate(q.get("tool_name_accuracy")),
                ta=fmt_rate(q.get("tool_arg_exact_match")),
                sv=fmt_rate(q.get("schema_valid_json_rate")),
                sql=fmt_rate(q.get("sql_execution_match_rate")),
                lat=fmt_ms(q.get("mean_latency_ms")),
                role=m.get("role"),
                status=m.get("status"),
            )
        )

    lines.append("")
    lines.append("## UI")
    lines.append("")

    def ui_row(key, label):
        block = ui.get(key)
        if not isinstance(block, dict) or block.get("status") == "not measured":
            reason = block.get("reason") if isinstance(block, dict) else "missing"
            lines.append(f"### {label}")
            lines.append("")
            lines.append(f"not measured: {reason}")
            lines.append("")
            return
        def s(metric):
            m = block.get(metric) or {}
            if m.get("median") is None:
                return "not measured"
            return f"median={m['median']}, min={m['min']}, max={m['max']}"
        lines.append(f"### {label}")
        lines.append("")
        lines.append(f"- bundle_bytes: {block.get('bundle_bytes')}")
        lines.append(f"- binary_bytes: {block.get('binary_bytes')}")
        lines.append(f"- cold_start_s: {s('cold_start_s')}")
        lines.append(f"- idle_rss_total_kb: {s('idle_rss_total_kb')}")
        lines.append(f"- idle_rss_largest_kb: {s('idle_rss_largest_kb')}")
        lines.append(f"- idle_cpu_pct_mean_30s: {s('idle_cpu_pct_mean_30s')}")
        lines.append(f"- scroll_peak_rss_kb: {s('scroll_peak_rss_kb')}")
        lines.append(f"- scroll_cpu_pct_mean_10s: {s('scroll_cpu_pct_mean_10s')}")
        lines.append("")

    ui_row("A", "A = Tauri 2 + React + shadcn-style")
    ui_row("B", "B = GPUI + gpui-kit 0.10.0")

    lines.append("## Component availability")
    lines.append("")
    avail = ui.get("component_availability") or {}
    lines.append("| stack | date picker | multi-select | virtualised table |")
    lines.append("|---|---|---|---|")
    for stack, comps in avail.items():
        def cell(c):
            if not c:
                return "not measured"
            return f"{'yes' if c.get('available') else 'no'}: {c.get('component')}"
        lines.append(
            f"| {stack} | {cell(comps.get('date_picker'))} | {cell(comps.get('multi_select'))} | {cell(comps.get('virtualised_table'))} |"
        )
    lines.append("")
    lines.append("Source files: `results/models.json`, `results/ui.json`, `results/raw/*.jsonl`, `results/speed/*`, `results/memory/*`.")
    (RES / "RESULTS.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {RES / 'RESULTS.md'}")


if __name__ == "__main__":
    main()
