"""OpenAI-compatible LLM judge for layer-2 rubrics (offline-testable)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml

_SPEC_RUBRICS = Path(__file__).resolve().parents[1] / "spec" / "rubrics"
_PROMPTS = _SPEC_RUBRICS / "prompts"

Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

_JSON_FENCE = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def load_rubric(id_or_path: str | Path) -> dict[str, Any]:
    """Load a rubric YAML by id (under spec/rubrics/) or absolute/relative path."""
    path = Path(id_or_path)
    if not path.suffix:
        path = _SPEC_RUBRICS / f"{id_or_path}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"rubric not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "id" not in data:
        raise ValueError(f"invalid rubric file: {path}")
    return data


def _rubric_text(rubric: dict[str, Any]) -> str:
    lines: list[str] = [
        f"id: {rubric['id']}",
        f"description: {str(rubric.get('description', '')).strip()}",
        "criteria:",
    ]
    for crit in rubric.get("criteria", []):
        lines.append(f"  - name: {crit['name']}")
        lines.append(f"    score_1: {crit['score_1']}")
        lines.append(f"    score_3: {crit['score_3']}")
        lines.append(f"    score_5: {crit['score_5']}")
    inv = rubric.get("invented_fact") or {}
    lines.append("invented_fact:")
    lines.append(f"  description: {str(inv.get('description', '')).strip()}")
    return "\n".join(lines)


def render_prompt(template_name: str, **vars: str) -> str:
    """Load prompts/<template_name>.txt and substitute {placeholders}."""
    name = template_name if template_name.endswith(".txt") else f"{template_name}.txt"
    path = _PROMPTS / name
    if not path.is_file():
        raise FileNotFoundError(f"prompt template not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        return text.format(**vars)
    except KeyError as exc:
        raise KeyError(f"missing prompt placeholder: {exc}") from exc


def _default_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"judge HTTP {exc.code}: {detail}") from exc


def _strip_fences(content: str) -> str:
    text = content.strip()
    match = _JSON_FENCE.match(text)
    if match:
        return match.group(1).strip()
    return text


def _parse_json_content(content: str) -> dict[str, Any]:
    raw = _strip_fences(content)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("judge reply JSON must be an object")
    return parsed


def _chat(
    *,
    judge_url: str,
    judge_model: str,
    messages: list[dict[str, str]],
    api_key: str | None,
    transport: Transport | None,
) -> str:
    url = f"{judge_url.rstrip('/')}/chat/completions"
    body: dict[str, Any] = {
        "model": judge_model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    headers: dict[str, str] = {}
    key = api_key if api_key is not None else os.environ.get("DONNA_JUDGE_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    post = transport or _default_post
    data = post(url, headers, body)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected chat/completions shape: {data!r}") from exc


def _chat_json(
    *,
    judge_url: str,
    judge_model: str,
    messages: list[dict[str, str]],
    api_key: str | None,
    transport: Transport | None,
) -> dict[str, Any]:
    content = _chat(
        judge_url=judge_url,
        judge_model=judge_model,
        messages=messages,
        api_key=api_key,
        transport=transport,
    )
    try:
        return _parse_json_content(content)
    except (json.JSONDecodeError, ValueError):
        retry_messages = list(messages) + [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": "Your previous reply was not valid JSON. Return valid JSON only.",
            },
        ]
        content2 = _chat(
            judge_url=judge_url,
            judge_model=judge_model,
            messages=retry_messages,
            api_key=api_key,
            transport=transport,
        )
        try:
            return _parse_json_content(content2)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"judge did not return valid JSON after retry: {content2!r}") from exc


def score(
    rubric: dict[str, Any] | str,
    source: str,
    output: str,
    *,
    judge_url: str,
    judge_model: str,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Score output with a rubric; returns parsed JSON object from the judge."""
    rub = load_rubric(rubric) if isinstance(rubric, str) else rubric
    prompt = render_prompt(
        "score",
        rubric_text=_rubric_text(rub),
        source=source,
        output=output,
    )
    return _chat_json(
        judge_url=judge_url,
        judge_model=judge_model,
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        transport=transport,
    )


def _winner_from_payload(payload: dict[str, Any]) -> str:
    winner = payload.get("winner")
    if winner not in ("a", "b", "tie"):
        raise ValueError(f"pairwise winner must be a|b|tie, got {winner!r}")
    return winner


def pairwise(
    rubric: dict[str, Any] | str,
    source: str,
    a: str,
    b: str,
    *,
    judge_url: str,
    judge_model: str,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> str:
    """Pairwise compare a vs b with order-swap; conflicts become 'tie'."""
    rub = load_rubric(rubric) if isinstance(rubric, str) else rubric
    rub_text = _rubric_text(rub)

    def one(left: str, right: str) -> str:
        prompt = render_prompt(
            "pairwise",
            rubric_text=rub_text,
            source=source,
            a=left,
            b=right,
        )
        payload = _chat_json(
            judge_url=judge_url,
            judge_model=judge_model,
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            transport=transport,
        )
        return _winner_from_payload(payload)

    first = one(a, b)
    second_raw = one(b, a)
    # Invert second call: positions swapped, so a↔b.
    if second_raw == "a":
        second = "b"
    elif second_raw == "b":
        second = "a"
    else:
        second = "tie"

    if first == second:
        return first
    if first == "tie":
        return second
    if second == "tie":
        return first
    return "tie"
