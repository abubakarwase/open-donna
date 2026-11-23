"""Unit tests for model_only message→trace mapping (no server / network)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "suts" / "model_only.py"


def _load():
    spec = importlib.util.spec_from_file_location("model_only", MOD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mo():
    return _load()


REQUIRED = {
    "case_id",
    "route",
    "tool_calls",
    "drafts",
    "escalation",
    "executed_side_effects",
    "steps",
    "latency_ms",
    "peak_rss_bytes",
}


def test_native_tool_calls(mo):
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {
                    "name": "search_email",
                    "arguments": json.dumps({"query": "invoice", "from": "acme@x.com"}),
                },
            }
        ],
    }
    tr = mo.message_to_trace("eml-0001", msg, latency_ms=12.5, peak_rss_bytes=1000)
    assert REQUIRED <= tr.keys()
    assert tr["route"]["label"] == "email"
    assert tr["tool_calls"] == [
        {"name": "search_email", "args": {"query": "invoice", "from": "acme@x.com"}}
    ]
    assert tr["drafts"] == []
    assert tr["executed_side_effects"] == []
    assert tr["steps"] == 1
    assert tr["latency_ms"] == 12.5
    assert tr["peak_rss_bytes"] == 1000


def test_content_json_tool_calls(mo):
    msg = {
        "role": "assistant",
        "content": json.dumps(
            {
                "route": "calendar",
                "confidence": 0.8,
                "tool_calls": [
                    {
                        "name": "find_free_slot",
                        "args": {"duration_min": 30, "after": "2026-11-23T09:00:00+01:00"},
                    }
                ],
            }
        ),
    }
    tr = mo.message_to_trace("cal-0002", msg, latency_ms=1, peak_rss_bytes=2)
    assert tr["route"] == {"label": "calendar", "confidence": 0.8}
    assert tr["tool_calls"][0]["name"] == "find_free_slot"
    assert tr["executed_side_effects"] == []


def test_escalate_only(mo):
    args = {
        "reason": "user_requested",
        "task": "rewrite",
        "payload_description": "thread summary",
    }
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {"name": "escalate", "arguments": json.dumps(args)},
            }
        ],
    }
    tr = mo.message_to_trace("sft-0003", msg, latency_ms=3, peak_rss_bytes=4)
    assert tr["route"]["label"] == "escalate"
    assert tr["escalation"]["escalated"] is True
    assert tr["escalation"]["reason"] == "user_requested"
    assert tr["escalation"]["payload_bytes"] == len(json.dumps(args, separators=(",", ":")))
    assert tr["executed_side_effects"] == []


def test_side_effect_becomes_draft(mo):
    msg = {
        "role": "assistant",
        "content": '{"route":"email","confidence":0.9}',
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {
                    "name": "draft_email",
                    "arguments": json.dumps(
                        {"to": ["a@b.com"], "subject": "Hi", "body": "Hello"}
                    ),
                },
            }
        ],
    }
    tr = mo.message_to_trace("eml-0004", msg, latency_ms=1, peak_rss_bytes=1)
    assert tr["drafts"] == [
        {"kind": "email", "payload": {"to": ["a@b.com"], "subject": "Hi", "body": "Hello"}}
    ]
    assert tr["executed_side_effects"] == []


def test_router_only(mo):
    msg = {
        "role": "assistant",
        "content": '{"route":"sheet","confidence":0.77}',
    }
    tr = mo.message_to_trace(
        "rtr-0005", msg, latency_ms=9, peak_rss_bytes=8, mode="router"
    )
    assert tr["route"] == {"label": "sheet", "confidence": 0.77}
    assert tr["tool_calls"] == []
    assert tr["drafts"] == []
    assert tr["steps"] == 1
    assert tr["executed_side_effects"] == []
    assert REQUIRED <= tr.keys()


def test_answer_with_native_tools_slot(mo):
    msg = {
        "role": "assistant",
        "content": json.dumps(
            {
                "route": "calendar",
                "confidence": 0.9,
                "answer": {
                    "start": "2026-11-23T10:00:00+01:00",
                    "end": "2026-11-23T10:30:00+01:00",
                },
            }
        ),
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {
                    "name": "find_free_slot",
                    "arguments": json.dumps(
                        {"duration_min": 30, "after": "2026-11-23T09:00:00+01:00"}
                    ),
                },
            }
        ],
    }
    tr = mo.message_to_trace("cal-0012", msg, latency_ms=1, peak_rss_bytes=1)
    assert tr["tool_calls"][0]["name"] == "find_free_slot"
    assert json.loads(tr["answer"]) == {
        "start": "2026-11-23T10:00:00+01:00",
        "end": "2026-11-23T10:30:00+01:00",
    }


def test_answer_triage_map_in_content(mo):
    triage = {"thr_a": "needs_reply", "thr_b": "fyi"}
    msg = {
        "role": "assistant",
        "content": json.dumps(
            {"route": "email", "confidence": 0.8, "answer": triage}
        ),
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {
                    "name": "search_email",
                    "arguments": json.dumps({"unread": True}),
                },
            }
        ],
    }
    tr = mo.message_to_trace("eml-0008", msg, latency_ms=1, peak_rss_bytes=1)
    assert json.loads(tr["answer"]) == triage


def test_response_format_constant(mo):
    assert mo._JSON_RESPONSE_FORMAT["type"] == "json_schema"
    assert "donna_sut" in mo._JSON_RESPONSE_FORMAT["json_schema"]["name"]
