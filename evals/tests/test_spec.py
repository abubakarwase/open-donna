"""Validate E-02 tool and label contracts under evals/spec/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, validate

SPEC = Path(__file__).resolve().parents[1] / "spec"
TOOLS_DIR = SPEC / "tools"
EXPECTED_TOOLS = [
    "search_email",
    "get_thread",
    "draft_email",
    "list_events",
    "find_free_slot",
    "create_event",
    "update_event",
    "cancel_event",
    "query_sheet",
    "escalate",
]
ROUTER_KEYS = {"email", "calendar", "sheet", "escalate", "general"}
ESCALATE_REASONS = {
    "low_confidence",
    "multi_step",
    "schema_failed",
    "messy_sheet",
    "cross_sheet_join",
    "why_or_forecast",
    "ambiguous",
    "user_requested",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_schema_paths() -> list[Path]:
    return sorted(p for p in TOOLS_DIR.glob("*.json") if p.name != "index.json")


@pytest.fixture(scope="module")
def tool_schemas() -> dict[str, dict]:
    return {p.stem: _load(p) for p in _tool_schema_paths()}


@pytest.fixture(scope="module")
def labels() -> dict:
    return _load(SPEC / "labels.json")


@pytest.fixture(scope="module")
def index() -> dict:
    return _load(TOOLS_DIR / "index.json")


def test_schema_count_is_ten(tool_schemas: dict[str, dict]) -> None:
    assert set(tool_schemas) == set(EXPECTED_TOOLS)
    assert len(tool_schemas) == 10


def test_schemas_are_draft_2020_12(tool_schemas: dict[str, dict]) -> None:
    for name, schema in tool_schemas.items():
        Draft202012Validator.check_schema(schema)


def test_examples_validate(tool_schemas: dict[str, dict]) -> None:
    for name, schema in tool_schemas.items():
        examples = schema.get("examples") or []
        assert examples, f"{name} missing examples"
        for i, example in enumerate(examples):
            validate(instance=example, schema=schema)


def test_labels_router_keys(labels: dict) -> None:
    assert set(labels["router"]) == ROUTER_KEYS
    assert "multi_step" not in labels["router"]


def test_index_lists_ten_and_files_exist(index: dict) -> None:
    names = [t["name"] for t in index["tools"]]
    assert names == EXPECTED_TOOLS
    for entry in index["tools"]:
        path = TOOLS_DIR / entry["schema"]
        assert path.is_file(), f"missing schema file for {entry['name']}: {path}"


def test_escalate_reason_enums_match_labels(
    tool_schemas: dict[str, dict], labels: dict
) -> None:
    enum = set(tool_schemas["escalate"]["properties"]["reason"]["enum"])
    assert enum == ESCALATE_REASONS
    assert set(labels["escalate_reasons"]) == ESCALATE_REASONS


def test_negative_search_email_empty_object(tool_schemas: dict[str, dict]) -> None:
    with pytest.raises(Exception):
        validate(instance={}, schema=tool_schemas["search_email"])


def test_negative_draft_email_body_only(tool_schemas: dict[str, dict]) -> None:
    with pytest.raises(Exception):
        validate(instance={"body": "hello"}, schema=tool_schemas["draft_email"])


def test_negative_update_event_id_only(tool_schemas: dict[str, dict]) -> None:
    with pytest.raises(Exception):
        validate(
            instance={"event_id": "evt_only"},
            schema=tool_schemas["update_event"],
        )
