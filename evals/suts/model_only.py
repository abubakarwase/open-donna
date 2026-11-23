#!/usr/bin/env python3
"""Model-only SUT: drive local GGUF via llama-server; emit SUT-protocol traces."""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

EVALS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EVALS_ROOT.parent
SPEC_ROOT = EVALS_ROOT / "spec"
PROMPTS = Path(__file__).resolve().parent / "prompts"
GGUF_DIR = REPO_ROOT / "bench" / "models" / "gguf"

_DRAFT_KIND = {
    "draft_email": "email",
    "create_event": "event",
    "update_event": "event_update",
    "cancel_event": "event_cancel",
}

_ROUTE_LABELS = ("email", "calendar", "sheet", "escalate", "general")
_JSON_FENCE = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_SERVER_PROC: subprocess.Popen[bytes] | None = None
_PEAK_RSS = 0


def _miss_trace(case_id: str, error: str, latency_ms: float = 0.0, peak_rss: int = 0) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "route": {"label": "general", "confidence": 0.0},
        "tool_calls": [],
        "drafts": [],
        "escalation": {"escalated": False, "reason": None, "payload_bytes": 0},
        "executed_side_effects": [],
        "steps": 0,
        "latency_ms": latency_ms,
        "peak_rss_bytes": peak_rss,
        "error": error,
    }


def load_openai_tools() -> list[dict[str, Any]]:
    index = json.loads((SPEC_ROOT / "tools" / "index.json").read_text(encoding="utf-8"))
    tools: list[dict[str, Any]] = []
    for entry in index["tools"]:
        schema = json.loads(
            (SPEC_ROOT / "tools" / entry["schema"]).read_text(encoding="utf-8")
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "description": schema.get("description") or entry["name"],
                    "parameters": {
                        k: v
                        for k, v in schema.items()
                        if k
                        not in (
                            "$schema",
                            "$id",
                            "title",
                            "description",
                            "examples",
                        )
                    },
                },
            }
        )
    return tools


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _compact_mailbox(data: dict[str, Any], *, max_threads: int = 12) -> dict[str, Any]:
    """Omit bodies; prefer unread; hard-cap for ctx 4096 with tools."""
    scored: list[tuple[int, dict[str, Any]]] = []
    for th in data.get("threads") or []:
        msgs_in = th.get("messages") or []
        unread = any(m.get("unread") for m in msgs_in)
        # One message stub is enough for routing/tool choice under tight ctx.
        last = msgs_in[-1] if msgs_in else {}
        scored.append(
            (
                0 if unread else 1,
                {
                    "thread_id": th.get("thread_id"),
                    "subject": th.get("subject"),
                    "from": last.get("from"),
                    "date": last.get("date"),
                    "unread": bool(unread),
                },
            )
        )
    scored.sort(key=lambda x: x[0])
    threads = [t for _, t in scored[:max_threads]]
    return {"threads": threads, "thread_total": len(data.get("threads") or [])}


def _compact_calendar(data: dict[str, Any], now_s: str) -> dict[str, Any]:
    now = _parse_dt(now_s)
    lo, hi = now - timedelta(days=14), now + timedelta(days=14)
    events = []
    for ev in data.get("events") or []:
        try:
            start = _parse_dt(str(ev.get("start")))
        except Exception:
            continue
        if lo <= start <= hi:
            events.append(
                {
                    "event_id": ev.get("event_id"),
                    "title": ev.get("title"),
                    "start": ev.get("start"),
                    "end": ev.get("end"),
                    "attendees": ev.get("attendees"),
                }
            )
    return {"events": events}


def _sheet_profile(path: Path) -> dict[str, Any]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        title = wb.sheetnames[0]
        ws = wb[title]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return {"sheet": title, "columns": [], "types": [], "sample_rows": []}
    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    sample = []
    for r in rows[1:4]:
        sample.append(
            [None if c is None else (c.isoformat() if hasattr(c, "isoformat") else c) for c in r]
        )
    types: list[str] = []
    for i, _h in enumerate(headers):
        vals = [r[i] if i < len(r) else None for r in rows[1:] if r]
        non_null = [v for v in vals if v is not None]
        if non_null and all(isinstance(v, bool) for v in non_null):
            types.append("bool")
        elif non_null and all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
            types.append("int")
        elif non_null and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
            types.append("float")
        else:
            types.append("str")
    return {
        "sheet": title,
        "columns": headers,
        "types": types,
        "sample_rows": sample,
    }


def compact_fixtures(refs: list[str], now_s: str) -> dict[str, Any]:
    from donna_evals.fixtures import load as load_fixture

    out: dict[str, Any] = {}
    for ref in refs:
        try:
            data = load_fixture(ref)
        except Exception as exc:
            out[ref] = {"error": str(exc)}
            continue
        if isinstance(data, Path):
            out[ref] = _sheet_profile(data)
        elif "threads" in data:
            out[ref] = _compact_mailbox(data)
        elif "events" in data:
            out[ref] = _compact_calendar(data, now_s)
        else:
            out[ref] = data
    return out


def build_user_message(case: dict[str, Any]) -> str:
    ctx = case.get("context") or {}
    now_s = str(ctx.get("now") or "")
    tz = str(ctx.get("timezone") or "")
    refs = list(ctx.get("fixtures") or [])
    fixtures = compact_fixtures(refs, now_s) if refs else {}
    parts = [
        f"Request: {(case.get('input') or {}).get('text', '')}",
        f"now: {now_s}",
        f"timezone: {tz}",
        "fixtures:\n" + json.dumps(fixtures, ensure_ascii=False, separators=(",", ":")),
    ]
    return "\n".join(parts)


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    m = _JSON_FENCE.match(t)
    return m.group(1).strip() if m else t


def _parse_args_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def extract_tool_calls_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer native OpenAI tool_calls; else parse content JSON tool_calls[]."""
    native = message.get("tool_calls") or []
    if native:
        out: list[dict[str, Any]] = []
        for tc in native:
            fn = tc.get("function") or {}
            out.append({"name": fn.get("name") or "", "args": _parse_args_obj(fn.get("arguments"))})
        return [c for c in out if c["name"]]
    content = _strip_fence(message.get("content") or "")
    if not content:
        return []
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    calls = obj.get("tool_calls") or []
    out = []
    for c in calls:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        out.append({"name": c["name"], "args": _parse_args_obj(c.get("args") or c.get("arguments"))})
    return out


def extract_route_from_message(message: dict[str, Any], tool_calls: list[dict[str, Any]]) -> tuple[str, float, str | None]:
    """Return (label, confidence, escalate_reason?)."""
    content = _strip_fence(message.get("content") or "")
    label: str | None = None
    confidence = 0.5
    escalate_reason: str | None = None
    if content:
        try:
            obj = json.loads(content)
            if isinstance(obj, dict):
                raw = obj.get("route")
                if isinstance(raw, str) and raw in _ROUTE_LABELS:
                    label = raw
                conf = obj.get("confidence")
                if isinstance(conf, (int, float)):
                    confidence = max(0.0, min(1.0, float(conf)))
                er = obj.get("escalate_reason")
                if isinstance(er, str):
                    escalate_reason = er
        except json.JSONDecodeError:
            low = content.lower()
            for r in _ROUTE_LABELS:
                if re.search(rf"\b{r}\b", low):
                    label = r
                    break
    if label is None:
        names = {c["name"] for c in tool_calls}
        if "escalate" in names:
            label = "escalate"
        elif names & {"search_email", "get_thread", "draft_email"}:
            label = "email"
        elif names & {
            "list_events",
            "find_free_slot",
            "create_event",
            "update_event",
            "cancel_event",
        }:
            label = "calendar"
        elif "query_sheet" in names:
            label = "sheet"
        else:
            label = "general"
    return label, confidence, escalate_reason


def drafts_from_tools(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for call in tool_calls:
        kind = _DRAFT_KIND.get(call["name"])
        if kind:
            drafts.append({"kind": kind, "payload": dict(call.get("args") or {})})
    return drafts


_ROUTE_META_KEYS = frozenset(
    {"route", "confidence", "escalate_reason", "tool_calls", "answer", "label"}
)


def extract_answer_from_message(message: dict[str, Any]) -> str | None:
    """Pull scoreable answer text (slot/triage/summary) from assistant content."""
    content = _strip_fence(message.get("content") or "")
    if not content:
        return None
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return content
    if not isinstance(obj, dict):
        return None
    if "answer" in obj:
        ans = obj["answer"]
        if ans is None:
            return json.dumps({"start": None, "end": None}, separators=(",", ":"))
        if isinstance(ans, str):
            return ans
        return json.dumps(ans, separators=(",", ":"))
    if "start" in obj and "end" in obj:
        return json.dumps(
            {"start": obj.get("start"), "end": obj.get("end")},
            separators=(",", ":"),
        )
    ps = obj.get("proposed_slot")
    if isinstance(ps, dict) and "start" in ps and "end" in ps:
        return json.dumps(
            {"start": ps.get("start"), "end": ps.get("end")},
            separators=(",", ":"),
        )
    tri = obj.get("triage")
    if isinstance(tri, dict) and tri:
        return json.dumps(tri, separators=(",", ":"))
    # Bare triage map: all str->str and not only route metadata.
    if obj and all(isinstance(k, str) and isinstance(v, str) for k, v in obj.items()):
        if set(obj.keys()) - _ROUTE_META_KEYS and "route" not in obj and "confidence" not in obj:
            return json.dumps(obj, separators=(",", ":"))
    return None


def message_has_structured_tools(message: dict[str, Any]) -> bool:
    """True if native tool_calls or content JSON tool_calls[] are present."""
    if message.get("tool_calls"):
        return True
    return bool(extract_tool_calls_from_message(message))


def message_to_trace(
    case_id: str,
    message: dict[str, Any],
    *,
    latency_ms: float,
    peak_rss_bytes: int,
    mode: str = "full",
) -> dict[str, Any]:
    """Map an assistant message to a schema-shaped trace (unit-testable)."""
    try:
        tool_calls = extract_tool_calls_from_message(message) if mode == "full" else []
        label, confidence, escalate_reason = extract_route_from_message(message, tool_calls)
        escalated = label == "escalate"
        reason = escalate_reason
        payload_bytes = 0
        for call in tool_calls:
            if call["name"] == "escalate":
                escalated = True
                if reason is None:
                    reason = (call.get("args") or {}).get("reason")
                payload_bytes = len(json.dumps(call.get("args") or {}, separators=(",", ":")))
                break
        if escalated and label != "escalate":
            label = "escalate"
        steps = 1 if mode == "router" else max(1, len(tool_calls))
        if mode == "router" and not tool_calls:
            steps = 1
        content = message.get("content")
        trace: dict[str, Any] = {
            "case_id": case_id,
            "route": {"label": label, "confidence": confidence},
            "tool_calls": tool_calls,
            "drafts": drafts_from_tools(tool_calls),
            "escalation": {
                "escalated": escalated,
                "reason": reason if escalated else None,
                "payload_bytes": payload_bytes if escalated else 0,
            },
            "executed_side_effects": [],
            "steps": steps,
            "latency_ms": float(latency_ms),
            "peak_rss_bytes": int(peak_rss_bytes),
        }
        if mode == "full":
            answer = extract_answer_from_message(message)
            if answer is None and isinstance(content, str) and content.strip() and not tool_calls:
                # Free-text summary when no tools and not structured JSON.
                try:
                    json.loads(_strip_fence(content))
                except json.JSONDecodeError:
                    answer = content
            if answer is not None:
                trace["answer"] = answer
        return trace
    except Exception as exc:
        return _miss_trace(case_id, f"parse_failure:{exc}", latency_ms, peak_rss_bytes)


def resolve_model(model: str) -> Path:
    p = Path(model)
    if p.is_file():
        return p.resolve()
    candidate = GGUF_DIR / model
    if candidate.is_file():
        return candidate.resolve()
    raise FileNotFoundError(f"GGUF not found: {model} (looked under {GGUF_DIR})")


def _rss_bytes(pid: int) -> int:
    try:
        import psutil

        return int(psutil.Process(pid).memory_info().rss)
    except Exception:
        try:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True)
            kb = int(out.strip() or "0")
            return kb * 1024
        except Exception:
            return 0


def _update_peak_rss(pid: int) -> int:
    global _PEAK_RSS
    rss = _rss_bytes(pid)
    if rss > _PEAK_RSS:
        _PEAK_RSS = rss
    return _PEAK_RSS


def stop_server() -> None:
    global _SERVER_PROC
    proc = _SERVER_PROC
    _SERVER_PROC = None
    if proc is None:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def start_server(model_path: Path, port: int) -> subprocess.Popen[bytes]:
    global _SERVER_PROC, _PEAK_RSS
    stop_server()
    _PEAK_RSS = 0
    log_path = EVALS_ROOT / "reports" / f"model_only-server-{port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            "llama-server",
            "-m",
            str(model_path),
            "-c",
            "4096",
            "--port",
            str(port),
            "--jinja",
            "-np",
            "1",
            "--reasoning",
            "off",
            "--reasoning-budget",
            "0",
        ],
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    _SERVER_PROC = proc
    atexit.register(stop_server)

    def _on_signal(_signum: int, _frame: Any) -> None:
        stop_server()
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    url = f"http://127.0.0.1:{port}/health"
    for _ in range(180):
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited early; see {log_path}")
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    _update_peak_rss(proc.pid)
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    stop_server()
    raise RuntimeError(f"llama-server health timeout; see {log_path}")


_JSON_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "donna_sut",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "route": {"type": "string"},
                "confidence": {"type": "number"},
                "escalate_reason": {"type": ["string", "null"]},
                "tool_calls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["name", "args"],
                    },
                },
                "answer": {},
            },
            "required": ["route", "confidence"],
        },
    },
}


def chat(
    port: int,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    seed: int | None,
    timeout_s: float = 180.0,
    response_format: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], float]:
    body: dict[str, Any] = {
        "model": "local",
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if response_format is not None:
        body["response_format"] = response_format
    if seed is not None:
        body["seed"] = seed
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"chat HTTP {exc.code}: {detail[:500]}") from exc
    lat = (time.perf_counter() - t0) * 1000.0
    msg = payload["choices"][0]["message"]
    return msg, lat


def chat_with_fallback(
    port: int,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    seed: int | None,
) -> tuple[dict[str, Any], float]:
    """Prefer native tool calls; else retry once with JSON response_format."""
    msg, lat = chat(port, messages, tools, temperature, seed)
    if tools is None:
        return msg, lat
    if message_has_structured_tools(msg):
        return msg, lat
    # Content may already be route/tool JSON without native tool_calls.
    content = _strip_fence(msg.get("content") or "")
    if content:
        try:
            obj = json.loads(content)
            if isinstance(obj, dict) and (
                obj.get("tool_calls") or obj.get("route") or "answer" in obj
            ):
                return msg, lat
        except json.JSONDecodeError:
            pass
    try:
        msg2, lat2 = chat(
            port,
            messages,
            tools=None,
            temperature=temperature,
            seed=seed,
            response_format=_JSON_RESPONSE_FORMAT,
        )
        return msg2, lat + lat2
    except Exception:
        # Some llama-server builds reject json_schema; try json_object.
        try:
            msg2, lat2 = chat(
                port,
                messages,
                tools=None,
                temperature=temperature,
                seed=seed,
                response_format={"type": "json_object"},
            )
            return msg2, lat + lat2
        except Exception:
            return msg, lat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model-only SUT via llama-server")
    parser.add_argument("--model", required=True, help="GGUF path or basename under bench/models/gguf/")
    parser.add_argument("--mode", choices=("full", "router"), default="full")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    model_path = resolve_model(args.model)
    temperature = float(os.environ.get("DONNA_TEMPERATURE", "0"))
    seed_raw = os.environ.get("DONNA_SEED", "0")
    seed = int(seed_raw) if seed_raw != "" else None

    prompt_name = "router.txt" if args.mode == "router" else "full.txt"
    system = (PROMPTS / prompt_name).read_text(encoding="utf-8").strip()
    tools = load_openai_tools() if args.mode == "full" else None

    print(f"model_only: starting {model_path.name} mode={args.mode} port={args.port}", file=sys.stderr)
    proc = start_server(model_path, args.port)
    print(f"model_only: healthy pid={proc.pid}", file=sys.stderr)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        case = json.loads(line)
        case_id = case.get("id") or "unk-0000"
        try:
            user = build_user_message(case)
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            msg, lat = chat_with_fallback(
                args.port, messages, tools, temperature, seed
            )
            # J2/J6 need a scoreable answer; ask once more if tools came back alone.
            job = case.get("job")
            first_tools = msg.get("tool_calls")
            if (
                args.mode == "full"
                and job in ("J2", "J6")
                and extract_answer_from_message(msg) is None
            ):
                prior = msg.get("content") or (
                    json.dumps({"note": "tools_called", "names": [
                        (t.get("function") or {}).get("name") for t in (first_tools or [])
                    ]})
                )
                follow = list(messages) + [
                    {"role": "assistant", "content": prior},
                    {
                        "role": "user",
                        "content": (
                            "Return JSON only with route, confidence, and answer. "
                            "J2 answer: {thread_id: needs_reply|fyi|ignore}. "
                            "J6 answer: {start, end} RFC3339 (or nulls)."
                        ),
                    },
                ]
                try:
                    msg2, lat2 = chat(
                        args.port,
                        follow,
                        tools=None,
                        temperature=temperature,
                        seed=seed,
                        response_format={"type": "json_object"},
                    )
                    msg2 = dict(msg2)
                    if first_tools and not msg2.get("tool_calls"):
                        msg2["tool_calls"] = first_tools
                    msg, lat = msg2, lat + lat2
                except Exception as follow_exc:
                    print(
                        f"model_only: answer follow-up failed {case_id}: {follow_exc}",
                        file=sys.stderr,
                    )
            peak = _update_peak_rss(proc.pid)
            trace = message_to_trace(
                case_id,
                msg,
                latency_ms=lat,
                peak_rss_bytes=peak,
                mode=args.mode,
            )
        except Exception as exc:
            peak = _update_peak_rss(proc.pid) if proc.poll() is None else _PEAK_RSS
            print(f"model_only: case {case_id} error: {exc}", file=sys.stderr)
            trace = _miss_trace(case_id, str(exc), peak_rss=peak)
        sys.stdout.write(json.dumps(trace, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    stop_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
