#!/usr/bin/env python3
"""Run golden.jsonl against llama-server OpenAI-compatible API; write raw jsonl."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from score import (
    ROUTES,
    aggregate,
    extract_route,
    extract_sql,
    score_case,
    tool_schemas_for,
)

PORT = int(os.environ.get("BENCH_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}/v1"
GOLDEN = Path(os.environ["BENCH_GOLDEN"])
RAW = Path(os.environ["BENCH_RAW"])
MODEL_ID = os.environ.get("BENCH_MODEL_ID", "model")
ROLE = os.environ.get("BENCH_ROLE", "base")

ROUTER_SYSTEM = (
    "You are a router. Reply with exactly one word from: "
    "email, calendar, sheet, general, escalate. No other text."
)
SHEET_SYSTEM = (
    "You write DuckDB SQL for table sales. Reply with SQL only "
    "(optional ```sql fence). Columns: region, product, units, revenue, month, rep."
)


def chat(messages, tools=None, temperature=0.0):
    body = {
        "model": "local",
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    t0 = time.perf_counter()
    r = httpx.post(f"{BASE}/chat/completions", json=body, timeout=180.0)
    lat = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    data = r.json()
    msg = data["choices"][0]["message"]
    return msg, lat


def run_case(case: dict) -> dict:
    t = case["type"]
    if ROLE == "router" and t != "router":
        return {
            "id": case["id"],
            "skipped": True,
            "reason": "functiongemma router-only",
            "latency_ms": None,
            "content": None,
        }
    if t == "router":
        msg, lat = chat(
            [
                {"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": case["input"]},
            ]
        )
        content = msg.get("content") or ""
        return {
            "id": case["id"],
            "type": t,
            "latency_ms": lat,
            "content": content,
            "route": extract_route(content),
            "message": msg,
        }
    if t == "tool":
        tools = tool_schemas_for(case)
        msg, lat = chat(
            [
                {
                    "role": "system",
                    "content": "Call the appropriate tool with correct arguments. Do not invent tools.",
                },
                {"role": "user", "content": case["input"]},
            ],
            tools=tools,
        )
        return {
            "id": case["id"],
            "type": t,
            "latency_ms": lat,
            "content": msg.get("content"),
            "message": msg,
        }
    if t == "sheet":
        profile = json.dumps(case.get("profile", {}), ensure_ascii=False)
        msg, lat = chat(
            [
                {"role": "system", "content": SHEET_SYSTEM},
                {
                    "role": "user",
                    "content": f"Table profile:\n{profile}\n\nQuestion: {case['input']}",
                },
            ]
        )
        content = msg.get("content") or ""
        return {
            "id": case["id"],
            "type": t,
            "latency_ms": lat,
            "content": content,
            "sql": extract_sql(content),
            "message": msg,
        }
    raise ValueError(t)


def main():
    cases = [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]
    RAW.parent.mkdir(parents=True, exist_ok=True)
    scored = []
    with RAW.open("w") as f:
        for case in cases:
            try:
                out = run_case(case)
            except Exception as e:
                out = {
                    "id": case["id"],
                    "type": case["type"],
                    "error": str(e),
                    "latency_ms": None,
                    "content": None,
                }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
            if out.get("skipped"):
                continue
            scored.append(score_case(case, out))
    agg = aggregate(scored, ROLE)
    agg["model_id"] = MODEL_ID
    side = RAW.with_suffix(".scores.json")
    side.write_text(json.dumps(agg, indent=2))
    print(json.dumps({k: v for k, v in agg.items() if k != "cases"}, indent=2))


if __name__ == "__main__":
    main()
