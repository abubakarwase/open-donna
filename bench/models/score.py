#!/usr/bin/env python3
"""Score golden.jsonl model outputs; DuckDB execution match for sheet cases."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import duckdb
except ImportError:
    duckdb = None

ROOT = Path(__file__).resolve().parent
CSV = ROOT / "fixtures" / "sales.csv"
TOOLS = {
    "draft_email": {
        "type": "function",
        "function": {
            "name": "draft_email",
            "description": "Draft an email",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    "create_event": {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Create a calendar event",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    "find_free_slot": {
        "type": "function",
        "function": {
            "name": "find_free_slot",
            "description": "Find a free calendar slot",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_minutes": {"type": "integer"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["duration_minutes", "from", "to"],
            },
        },
    },
    "search_email": {
        "type": "function",
        "function": {
            "name": "search_email",
            "description": "Search email",
            "parameters": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["from", "query"],
            },
        },
    },
}

ROUTES = ["email", "calendar", "sheet", "general", "escalate"]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def norm_date(s: str) -> str:
    s = norm(s)
    s = s.replace(" ", "t")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.match(r"(\d{4}-\d{2}-\d{2})[t ](\d{2}:\d{2})(?::(\d{2}))?", s)
    if m:
        sec = m.group(3) or "00"
        return f"{m.group(1)}t{m.group(2)}:{sec}"
    return s


def args_match(expected: dict, got: dict) -> bool:
    if not isinstance(got, dict):
        return False
    for k, v in expected.items():
        if k not in got:
            return False
        ev, gv = v, got[k]
        if isinstance(ev, (int, float)) and isinstance(gv, (int, float, str)):
            try:
                if float(ev) != float(gv):
                    return False
            except Exception:
                return False
        else:
            e, g = str(ev), str(gv)
            if "date" in k.lower() or k in ("start", "end", "from", "to") and re.search(
                r"\d{4}-\d{2}-\d{2}", e
            ):
                if norm_date(e) != norm_date(g):
                    # from/to may be emails
                    if "@" in e or "@" in g:
                        if norm(e) != norm(g):
                            return False
                    else:
                        return False
            elif norm(e) != norm(g):
                return False
    return True


def extract_route(text: str) -> str | None:
    t = norm(text)
    for r in ROUTES:
        if re.search(rf"\b{r}\b", t):
            return r
    return None


def extract_sql(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"```sql\s*(.*?)```", text, re.I | re.S)
    if m:
        return m.group(1).strip().rstrip(";")
    m = re.search(r"(SELECT\b.*)", text, re.I | re.S)
    if m:
        return m.group(1).strip().split(";")[0].strip()
    return None


def run_sql(sql: str):
    if duckdb is None:
        raise RuntimeError("duckdb python package missing")
    con = duckdb.connect()
    con.execute(f"CREATE TABLE sales AS SELECT * FROM read_csv_auto('{CSV.as_posix()}')")
    return con.execute(sql).fetchall()


def sheet_match(pred_sql: str | None, expected_sql: str) -> bool:
    if not pred_sql:
        return False
    try:
        a = run_sql(pred_sql)
        b = run_sql(expected_sql)
        return a == b
    except Exception:
        return False


def parse_tool_call(obj) -> tuple[str | None, dict | None, bool]:
    """Return name, args, schema_valid."""
    if obj is None:
        return None, None, False
    # OpenAI tool_calls shape
    if isinstance(obj, dict):
        if "tool_calls" in obj:
            tcs = obj["tool_calls"]
            if not tcs:
                return None, None, False
            tc = tcs[0]
            fn = tc.get("function") or {}
            name = fn.get("name")
            raw = fn.get("arguments", "{}")
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                return name, args, isinstance(args, dict)
            except Exception:
                return name, None, False
        if "name" in obj and "arguments" in obj:
            args = obj["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    return obj.get("name"), None, False
            return obj.get("name"), args, isinstance(args, dict)
        # content may be JSON
        content = obj.get("content")
        if isinstance(content, str):
            try:
                inner = json.loads(content)
                return parse_tool_call(inner)
            except Exception:
                m = re.search(
                    r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
                    content,
                    re.S,
                )
                if m:
                    try:
                        inner = json.loads(m.group(0))
                        return parse_tool_call(inner)
                    except Exception:
                        pass
    return None, None, False


def score_case(case: dict, output: dict) -> dict:
    t = case["type"]
    lat = output.get("latency_ms")
    result = {
        "id": case["id"],
        "type": t,
        "latency_ms": lat,
        "ok": False,
        "schema_valid": None,
        "detail": {},
    }
    if t == "router":
        pred = output.get("route") or extract_route(output.get("content") or "")
        exp = case["expected"]["route"]
        result["ok"] = pred == exp
        result["detail"] = {"pred": pred, "expected": exp}
    elif t == "tool":
        name, args, valid = parse_tool_call(output.get("message") or output)
        if name is None and output.get("content"):
            name, args, valid = parse_tool_call({"content": output["content"]})
        exp = case["expected"]
        name_ok = name == exp["name"]
        arg_ok = args_match(exp["arguments"], args or {})
        result["schema_valid"] = bool(valid)
        result["ok"] = bool(name_ok and arg_ok)
        result["detail"] = {
            "name_ok": name_ok,
            "arg_ok": arg_ok,
            "pred_name": name,
            "pred_args": args,
        }
    elif t == "sheet":
        sql = output.get("sql") or extract_sql(output.get("content") or "")
        ok = sheet_match(sql, case["expected_sql"])
        result["ok"] = ok
        result["detail"] = {"pred_sql": sql}
    return result


def aggregate(scored: list[dict], role: str) -> dict:
    by = {"router": [], "tool": [], "sheet": []}
    for s in scored:
        by[s["type"]].append(s)

    def rate(xs, key=lambda x: x["ok"]):
        if not xs:
            return None
        return sum(1 for x in xs if key(x)) / len(xs)

    tool = by["tool"]
    lats = [s["latency_ms"] for s in scored if isinstance(s.get("latency_ms"), (int, float))]
    return {
        "role": role,
        "n_cases": len(scored),
        "router_accuracy": rate(by["router"]),
        "tool_name_accuracy": rate(tool, lambda x: x["detail"].get("name_ok")),
        "tool_arg_exact_match": rate(tool, lambda x: x["detail"].get("arg_ok")),
        "schema_valid_json_rate": rate(
            [t for t in tool if t.get("schema_valid") is not None],
            lambda x: x["schema_valid"],
        ),
        "sql_execution_match_rate": rate(by["sheet"]),
        "mean_latency_ms": (sum(lats) / len(lats)) if lats else None,
        "cases": scored,
    }


def tool_schemas_for(case: dict):
    names = case.get("tools") or list(TOOLS.keys())
    return [TOOLS[n] for n in names if n in TOOLS]


if __name__ == "__main__":
    # CLI: score a raw jsonl against golden
    if len(sys.argv) < 3:
        print("usage: score.py <golden.jsonl> <raw.jsonl>", file=sys.stderr)
        sys.exit(2)
    golden = [json.loads(l) for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()]
    raw = {
        json.loads(l)["id"]: json.loads(l)
        for l in Path(sys.argv[2]).read_text().splitlines()
        if l.strip()
    }
    scored = [score_case(c, raw.get(c["id"], {})) for c in golden]
    print(json.dumps(aggregate(scored, "scored"), indent=2))
