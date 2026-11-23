#!/usr/bin/env python3
"""Validate golden case and SUT trace JSONL against evals/spec schemas."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

SPEC = Path(__file__).resolve().parents[1] / "spec"
TOOLS_DIR = SPEC / "tools"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_tool_schemas() -> dict[str, dict]:
    index = _load(TOOLS_DIR / "index.json")
    schemas: dict[str, dict] = {}
    for entry in index["tools"]:
        schemas[entry["name"]] = _load(TOOLS_DIR / entry["schema"])
    return schemas


def _load_labels() -> dict:
    return _load(SPEC / "labels.json")


def _emit(path: Path, line_no: int, message: str) -> None:
    print(f"{path}:{line_no}: {message}", file=sys.stderr)


def _validate_tool_args(
    tool_schemas: dict[str, dict],
    name: str,
    args: object,
) -> str | None:
    schema = tool_schemas.get(name)
    if schema is None:
        return f"unknown tool {name!r}"
    if not isinstance(args, dict):
        return f"tool {name!r} args must be an object"
    try:
        Draft202012Validator(schema).validate(args)
    except ValidationError as exc:
        return f"tool {name!r} args: {exc.message}"
    return None


def _iter_jsonl(path: Path):
    text = path.read_text(encoding="utf-8")
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            yield i, json.loads(line)
        except json.JSONDecodeError as exc:
            yield i, exc


def validate_cases(paths: list[Path]) -> int:
    case_schema = _load(SPEC / "case.schema.json")
    case_validator = Draft202012Validator(case_schema)
    tool_schemas = _load_tool_schemas()
    labels = _load_labels()
    router = set(labels["router"])
    escalate_reasons = set(labels["escalate_reasons"])
    seen_ids: dict[str, tuple[Path, int]] = {}
    errors = 0

    for path in paths:
        for line_no, obj in _iter_jsonl(path):
            if isinstance(obj, json.JSONDecodeError):
                _emit(path, line_no, f"invalid JSON: {obj.msg}")
                errors += 1
                continue
            try:
                case_validator.validate(obj)
            except ValidationError as exc:
                _emit(path, line_no, f"schema: {exc.message}")
                errors += 1
                continue

            case_id = obj["id"]
            if case_id in seen_ids:
                prev_path, prev_line = seen_ids[case_id]
                _emit(
                    path,
                    line_no,
                    f"duplicate id {case_id!r} (also {prev_path}:{prev_line})",
                )
                errors += 1
            else:
                seen_ids[case_id] = (path, line_no)

            expected = obj["expected"]
            route = expected["route"]
            if route not in router:
                _emit(path, line_no, f"unknown route {route!r}")
                errors += 1

            if expected["escalation"] == "escalate":
                reason = expected.get("escalate_reason")
                if not reason:
                    _emit(path, line_no, "escalation==escalate requires escalate_reason")
                    errors += 1
                elif reason not in escalate_reasons:
                    _emit(path, line_no, f"unknown escalate_reason {reason!r}")
                    errors += 1
            elif expected.get("escalate_reason") is not None:
                reason = expected["escalate_reason"]
                if reason not in escalate_reasons:
                    _emit(path, line_no, f"unknown escalate_reason {reason!r}")
                    errors += 1

            for call in expected.get("tool_calls") or []:
                msg = _validate_tool_args(tool_schemas, call["name"], call["args"])
                if msg:
                    _emit(path, line_no, msg)
                    errors += 1

    return 1 if errors else 0


def validate_traces(paths: list[Path]) -> int:
    trace_schema = _load(SPEC / "trace.schema.json")
    trace_validator = Draft202012Validator(trace_schema)
    tool_schemas = _load_tool_schemas()
    hard_errors = 0
    soft_false = 0

    for path in paths:
        for line_no, obj in _iter_jsonl(path):
            if isinstance(obj, json.JSONDecodeError):
                _emit(path, line_no, f"invalid JSON: {obj.msg}")
                hard_errors += 1
                continue
            try:
                trace_validator.validate(obj)
            except ValidationError as exc:
                _emit(path, line_no, f"schema: {exc.message}")
                hard_errors += 1
                continue

            line_soft = False
            for call in obj.get("tool_calls") or []:
                msg = _validate_tool_args(tool_schemas, call["name"], call["args"])
                if msg:
                    _emit(path, line_no, f"(soft) {msg}")
                    line_soft = True
            if line_soft:
                soft_false += 1

    print(f"schema_valid: false={soft_false}")
    return 1 if hard_errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="donna-evals-validate")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cases = sub.add_parser("cases", help="Validate golden case JSONL")
    p_cases.add_argument("files", nargs="+", type=Path)

    p_traces = sub.add_parser("traces", help="Validate SUT trace JSONL")
    p_traces.add_argument("files", nargs="+", type=Path)

    args = parser.parse_args(argv)
    if args.cmd == "cases":
        return validate_cases(args.files)
    return validate_traces(args.files)


if __name__ == "__main__":
    raise SystemExit(main())
