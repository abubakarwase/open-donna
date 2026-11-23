#!/usr/bin/env python3
"""Build results/models.json from speed/memory/raw score artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
RES = BENCH / "results"
GGUF = BENCH / "models" / "gguf"

CANDIDATES = [
    {
        "id": "qwen35-0.8b",
        "hf_repo": "unsloth/Qwen3.5-0.8B-GGUF",
        "file": "Qwen3.5-0.8B-Q4_K_M.gguf",
        "role": "base",
    },
    {
        "id": "qwen35-2b",
        "hf_repo": "unsloth/Qwen3.5-2B-GGUF",
        "file": "Qwen3.5-2B-Q4_K_M.gguf",
        "role": "base",
    },
    {
        "id": "qwen35-4b",
        "hf_repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q4_K_M.gguf",
        "role": "base",
    },
    {
        "id": "gemma4-e2b",
        "hf_repo": "unsloth/gemma-4-E2B-it-GGUF",
        "file": "gemma-4-E2B-it-Q4_K_M.gguf",
        "role": "base",
    },
    {
        "id": "gemma4-e4b",
        "hf_repo": "unsloth/gemma-4-E4B-it-GGUF",
        "file": "gemma-4-E4B-it-Q4_K_M.gguf",
        "role": "base",
    },
    {
        "id": "phi4-mini",
        "hf_repo": "unsloth/Phi-4-mini-instruct-GGUF",
        "file": "Phi-4-mini-instruct-Q4_K_M.gguf",
        "role": "base",
    },
    {
        "id": "functiongemma-270m",
        "hf_repo": "unsloth/functiongemma-270m-it-GGUF",
        "file": "functiongemma-270m-it-Q4_K_M.gguf",
        "role": "router",
    },
    {
        "id": "qwen36-le4b",
        "hf_repo": "unsloth/Qwen",
        "file": None,
        "role": "base",
        "status": "not available",
        "reason": "Qwen3.6 ≤4B GGUF not available on Hugging Face (checked unsloth/Qwen)",
    },
    {
        "id": "qwen38-le4b",
        "hf_repo": "unsloth/Qwen",
        "file": None,
        "role": "base",
        "status": "not available",
        "reason": "Qwen3.8 ≤4B GGUF not available on Hugging Face (checked unsloth/Qwen)",
    },
]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(p: Path):
    if not p.exists():
        return None
    return json.loads(p.read_text())


def main():
    models = []
    for c in CANDIDATES:
        entry = dict(c)
        if entry.get("status") == "not available":
            models.append(entry)
            continue
        path = GGUF / entry["file"]
        if not path.exists():
            entry["status"] = "not measured"
            entry["reason"] = f"GGUF missing: {entry['file']}"
            models.append(entry)
            continue
        entry["status"] = "measured"
        entry["sha256"] = sha256_file(path)
        entry["gguf_bytes"] = path.stat().st_size
        speed = load_json(RES / "speed" / f"{entry['id']}.parsed.json")
        mem = load_json(RES / "memory" / f"{entry['id']}.json")
        scores = load_json(RES / "raw" / f"{entry['id']}.scores.json")
        entry["speed"] = speed
        entry["memory"] = mem
        if scores:
            entry["quality"] = {
                k: scores.get(k)
                for k in (
                    "role",
                    "n_cases",
                    "router_accuracy",
                    "tool_name_accuracy",
                    "tool_arg_exact_match",
                    "schema_valid_json_rate",
                    "sql_execution_match_rate",
                    "mean_latency_ms",
                )
            }
        else:
            entry["quality"] = "not measured"
            entry["quality_reason"] = "scores file missing"
        models.append(entry)

    out = {
        "environment": {
            "machine": "Apple M1 Pro, 16 GB RAM, macOS 27.2.1",
            "note": "No 8 GB machine available. Fits 8 GB := peak RSS of base model process ≤ 3.5 GB.",
            "quant": "Q4_K_M",
            "context": 4096,
        },
        "models": models,
    }
    RES.mkdir(parents=True, exist_ok=True)
    (RES / "models.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {RES / 'models.json'} ({len(models)} entries)")


if __name__ == "__main__":
    main()
