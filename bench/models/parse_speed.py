#!/usr/bin/env python3
"""Parse llama-bench JSON into mean±std for pp512 and tg128."""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def ms(xs):
    if not xs:
        return {"mean": None, "std": None, "n": 0, "values": []}
    if len(xs) == 1:
        return {"mean": xs[0], "std": 0.0, "n": 1, "values": xs}
    return {
        "mean": statistics.mean(xs),
        "std": statistics.stdev(xs),
        "n": len(xs),
        "values": xs,
    }


def parse(path: Path, model_id: str) -> dict:
    data = json.loads(path.read_text())
    pp, tg = [], []
    if isinstance(data, list):
        for row in data:
            samples = row.get("samples_ts") or []
            if row.get("n_prompt", 0) > 0 and row.get("n_gen", 0) == 0:
                pp = [float(x) for x in samples] or [float(row.get("avg_ts", 0))]
            elif row.get("n_gen", 0) > 0:
                tg = [float(x) for x in samples] or [float(row.get("avg_ts", 0))]
    return {"id": model_id, "pp512_tok_s": ms(pp), "tg128_tok_s": ms(tg)}


if __name__ == "__main__":
    src, dst, mid = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    out = parse(src, mid)
    dst.write_text(json.dumps(out, indent=2))
    print(json.dumps(out))
