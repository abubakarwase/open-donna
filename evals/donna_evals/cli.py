"""CLI for donna-evals: validate + run."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from donna_evals import validate as validate_mod
from donna_evals.runner import run_eval


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="donna-evals")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Validate golden cases or SUT traces")
    val_sub = p_val.add_subparsers(dest="validate_cmd", required=True)
    p_cases = val_sub.add_parser("cases", help="Validate golden case JSONL")
    p_cases.add_argument("files", nargs="+", type=Path)
    p_traces = val_sub.add_parser("traces", help="Validate SUT trace JSONL")
    p_traces.add_argument("files", nargs="+", type=Path)

    p_run = sub.add_parser("run", help="Run SUT against a golden suite and score")
    p_run.add_argument("--sut", required=True, help="Shell command for the SUT process")
    p_run.add_argument(
        "--suite",
        required=True,
        choices=["router", "email", "calendar", "sheet", "safety", "all"],
    )
    p_run.add_argument("--split", required=True, choices=["dev", "test"])
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--timeout", type=float, default=60.0, help="Per-case timeout seconds")
    p_run.add_argument("--judge-url", default=None)
    p_run.add_argument("--judge-model", default=None)
    p_run.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: reports/<timestamp>)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "validate":
        if args.validate_cmd == "cases":
            return validate_mod.validate_cases(args.files)
        return validate_mod.validate_traces(args.files)

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path("reports") / stamp

    return run_eval(
        sut=args.sut,
        suite=args.suite,
        split=args.split,
        limit=args.limit,
        timeout_s=args.timeout,
        judge_url=args.judge_url,
        judge_model=args.judge_model,
        out_dir=out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
