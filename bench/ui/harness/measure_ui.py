#!/usr/bin/env python3
"""Measure cold start, idle RSS (tree + new helpers only), idle CPU, scroll RSS/CPU."""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2]
RES = BENCH / "results"
RES.mkdir(parents=True, exist_ok=True)

N_RUNS = 5

# Documented in RESULTS.md: RSS = process tree of the launched PID, plus only
# WebKit helper PIDs that appear after launch (pre-launch snapshot subtracted).
RSS_METHOD = (
    "total RSS = sum of RSS for (1) the launched process and all descendants "
    "via ppid BFS, plus (2) WebKit WebContent/Networking/GPU PIDs that exist "
    "after launch but were absent in a pre-launch snapshot. Unrelated "
    "pre-existing WebKit processes are excluded."
)


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def webkit_pids() -> set[int]:
    """PIDs whose command looks like a WebKit helper XPC."""
    out = run(["ps", "-ax", "-o", "pid=,command="])
    pids: set[int] = set()
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        cmd = parts[1]
        if (
            "WebKit.WebContent" in cmd
            or "WebKit.Networking" in cmd
            or "WebKit.GPU" in cmd
            or "com.apple.WebKit.WebContent" in cmd
            or "com.apple.WebKit.Networking" in cmd
            or "com.apple.WebKit.GPU" in cmd
        ):
            pids.add(int(parts[0]))
    return pids


def pgrep_tree(root_pid: int) -> list[int]:
    out = run(["ps", "-ax", "-o", "pid=,ppid="])
    children: dict[int, list[int]] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(children.get(p, []))
    return sorted(seen)


def procs_info(pids: list[int]):
    if not pids:
        return []
    out = run(["ps", "-o", "pid=,rss=,pcpu=,comm=", "-p", ",".join(map(str, pids))])
    info = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        info.append(
            {
                "pid": int(parts[0]),
                "rss_kb": int(parts[1]),
                "pcpu": float(parts[2]),
                "comm": parts[3],
            }
        )
    return info


def collect_infos(root_pid: int, pre_webkit: set[int], include_new_webkit: bool) -> list[dict]:
    pids = set(pgrep_tree(root_pid))
    if include_new_webkit:
        new_helpers = webkit_pids() - pre_webkit
        pids |= new_helpers
    return procs_info(sorted(pids))


def total_rss(infos: list[dict]) -> int:
    return sum(i["rss_kb"] for i in infos)


def largest(infos: list[dict]) -> dict | None:
    return max(infos, key=lambda i: i["rss_kb"]) if infos else None


def mean_cpu(infos: list[dict]) -> float:
    return sum(i["pcpu"] for i in infos)


def app_bundle_size(path: Path) -> int:
    if not path.exists():
        return 0
    out = run(["du", "-sk", str(path)])
    return int(out.stdout.split()[0]) * 1024


def binary_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def measure_once(launch_cmd: list[str], cwd: Path | None, app_hint: str, scroll: bool):
    env = os.environ.copy()
    env["RUST_BACKTRACE"] = "1"
    is_tauri = "tauri" in app_hint.lower() or "tauri" in " ".join(launch_cmd).lower()
    pre_webkit = webkit_pids() if is_tauri else set()

    t_spawn = time.perf_counter()
    proc = subprocess.Popen(
        launch_cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )
    first_frame_ms = None
    cold_start_s = None
    deadline = time.time() + 90
    buf = []
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if line:
            buf.append(line)
            m = re.search(r"FIRST_FRAME_MS\s+([0-9.]+)", line)
            if m:
                first_frame_ms = float(m.group(1))
                cold_start_s = time.perf_counter() - t_spawn
                break
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    time.sleep(10)
    infos = collect_infos(proc.pid, pre_webkit, include_new_webkit=is_tauri)
    idle_total = total_rss(infos)
    idle_largest = largest(infos)
    helper_pids = sorted(
        (webkit_pids() - pre_webkit) if is_tauri else []
    )

    cpu_samples = []
    t_end = time.time() + 30
    while time.time() < t_end:
        infos_c = collect_infos(proc.pid, pre_webkit, include_new_webkit=is_tauri)
        cpu_samples.append(mean_cpu(infos_c))
        time.sleep(1)
    idle_cpu = statistics.mean(cpu_samples) if cpu_samples else None

    scroll_peak = None
    scroll_cpu = None
    if scroll:
        run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to set frontmost of first process whose unix id is {proc.pid} to true',
            ]
        )
        time.sleep(0.5)
        run(["cliclick", "m:400,400"])
        peak = idle_total or 0
        cpu_s = []
        t_end = time.time() + 10
        while time.time() < t_end:
            run(["cliclick", "m:400,450"])
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to key code 125'],
                capture_output=True,
            )
            infos_s = collect_infos(proc.pid, pre_webkit, include_new_webkit=is_tauri)
            peak = max(peak, total_rss(infos_s))
            cpu_s.append(mean_cpu(infos_s))
            time.sleep(0.2)
        scroll_peak = peak
        scroll_cpu = statistics.mean(cpu_s) if cpu_s else None

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    return {
        "cold_start_s": cold_start_s,
        "first_frame_ms_stdout": first_frame_ms,
        "idle_rss_total_kb": idle_total,
        "idle_rss_largest_kb": idle_largest["rss_kb"] if idle_largest else None,
        "idle_rss_largest_comm": idle_largest["comm"] if idle_largest else None,
        "idle_cpu_pct_mean_30s": idle_cpu,
        "scroll_peak_rss_kb": scroll_peak,
        "scroll_cpu_pct_mean_10s": scroll_cpu,
        "pre_webkit_pid_count": len(pre_webkit),
        "new_webkit_helper_pids": helper_pids,
        "stdout_tail": "".join(buf)[-2000:],
    }


def summarize(runs: list[dict], key: str):
    vals = [r[key] for r in runs if isinstance(r.get(key), (int, float))]
    if not vals:
        return {"median": None, "min": None, "max": None, "n": 0, "values": []}
    return {
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "n": len(vals),
        "values": vals,
    }


def measure_app(name: str, launch_cmd: list[str], cwd: Path | None, bundle: Path, binary: Path, app_hint: str):
    runs = []
    for i in range(N_RUNS):
        print(f"=== {name} run {i+1}/{N_RUNS}", flush=True)
        runs.append(measure_once(launch_cmd, cwd, app_hint, scroll=True))
        time.sleep(2)
    return {
        "name": name,
        "bundle_bytes": app_bundle_size(bundle),
        "binary_bytes": binary_size(binary),
        "cold_start_s": summarize(runs, "cold_start_s"),
        "idle_rss_total_kb": summarize(runs, "idle_rss_total_kb"),
        "idle_rss_largest_kb": summarize(runs, "idle_rss_largest_kb"),
        "idle_cpu_pct_mean_30s": summarize(runs, "idle_cpu_pct_mean_30s"),
        "scroll_peak_rss_kb": summarize(runs, "scroll_peak_rss_kb"),
        "scroll_cpu_pct_mean_10s": summarize(runs, "scroll_cpu_pct_mean_10s"),
        "runs": runs,
    }


def main():
    tauri_bin = Path(os.environ.get("TAURI_BIN", ""))
    tauri_app = Path(os.environ.get("TAURI_APP", ""))
    gpui_bin = Path(os.environ.get("GPUI_BIN", ""))

    out = {
        "rss_method": RSS_METHOD,
        "component_availability": {
            "A_tauri_shadcn": {
                "date_picker": {
                    "available": True,
                    "component": "Radix Popover + react-day-picker DayPicker",
                },
                "multi_select": {
                    "available": True,
                    "component": "Radix Popover + cmdk Command (multi)",
                },
                "virtualised_table": {
                    "available": True,
                    "component": "TanStack Table + TanStack Virtual",
                },
            },
            "B_gpui_kit": {
                "date_picker": {"available": True, "component": "gpui-component DatePicker"},
                "multi_select": {
                    "available": True,
                    "component": "gpui-component Combobox.multiple",
                },
                "virtualised_table": {
                    "available": True,
                    "component": "gpui-component DataTable",
                },
            },
        },
        "deps": {"B": {"crate": "gpui-kit", "version": "0.10.0"}},
    }

    if tauri_bin.exists():
        out["A"] = measure_app(
            "A_tauri_shadcn",
            [str(tauri_bin)],
            None,
            tauri_app if tauri_app.exists() else tauri_bin.parent,
            tauri_bin,
            "tauri-shadcn",
        )
    else:
        out["A"] = {"status": "not measured", "reason": f"missing binary {tauri_bin}"}

    if gpui_bin.exists():
        out["B"] = measure_app(
            "B_gpui_kit",
            [str(gpui_bin)],
            None,
            gpui_bin,
            gpui_bin,
            "gpui-bench",
        )
        if isinstance(out["B"], dict) and "bundle_bytes" in out["B"]:
            out["B"]["bundle_note"] = (
                "no .app bundle; bundle_bytes is du of binary path; binary_bytes is main binary"
            )
    else:
        out["B"] = {"status": "not measured", "reason": f"missing binary {gpui_bin}"}

    (RES / "ui.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {RES / 'ui.json'}")


if __name__ == "__main__":
    main()
