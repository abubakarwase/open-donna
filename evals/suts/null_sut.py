#!/usr/bin/env python3
"""Null SUT: always general route, no tools, no escalate."""
from __future__ import annotations

import json
import sys


def main() -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        inbound = json.loads(line)
        trace = {
            "case_id": inbound["id"],
            "route": {"label": "general", "confidence": 0.0},
            "tool_calls": [],
            "drafts": [],
            "escalation": {"escalated": False, "reason": None, "payload_bytes": 0},
            "executed_side_effects": [],
            "steps": 0,
            "latency_ms": 0,
            "peak_rss_bytes": 0,
        }
        sys.stdout.write(json.dumps(trace, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
