"""Load E-04 fixtures by ref via evals/fixtures/index.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
_INDEX_PATH = _FIXTURES_ROOT / "index.json"


def _index() -> dict[str, Any]:
    if not _INDEX_PATH.is_file():
        raise FileNotFoundError(f"fixture index not found: {_INDEX_PATH}")
    return json.loads(_INDEX_PATH.read_text(encoding="utf-8"))


def load(ref: str) -> dict[str, Any] | Path:
    """Resolve a fixture ref.

    Mailbox and calendar refs return parsed JSON dicts.
    Sheet refs return a Path to the .xlsx file (not parsed).
    """
    index = _index()
    entry = index.get(ref)
    if entry is None:
        raise KeyError(f"unknown fixture ref: {ref!r}")
    path = _FIXTURES_ROOT / entry["path"]
    kind = entry["kind"]
    if kind == "sheet":
        if not path.is_file():
            raise FileNotFoundError(f"sheet fixture missing: {path}")
        return path
    if kind in ("mailbox", "calendar"):
        if not path.is_file():
            raise FileNotFoundError(f"fixture missing: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    raise ValueError(f"unsupported fixture kind {kind!r} for ref {ref!r}")
