#!/usr/bin/env python3
"""Build / validate judge calibration cases (50 rows).

Default: validate the committed cases.jsonl (no model).
With --regenerate: start llama-server on Qwen3.5-4B-Q4_K_M, generate
realistic summary/draft candidates (temp 0, fixed seed), write cases.jsonl,
then stop the server. Always leaves no llama-server on the calibration port.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from donna_evals.fixtures import load

OUT_DIR = Path(__file__).resolve().parent
OUT_PATH = OUT_DIR / "cases.jsonl"
REPO_ROOT = OUT_DIR.parents[2]
GGUF = REPO_ROOT / "bench" / "models" / "gguf" / "Qwen3.5-4B-Q4_K_M.gguf"

PERSONAS = ("mailbox/teamlead", "mailbox/freelancer", "mailbox/smallbiz")
# 5 classes × 5 rows × 2 kinds = 50
QUALITIES = ("model_plain", "vague", "wrong_tone", "invented_fact", "missing_ask")
PER_QUALITY = 5

PORT = int(os.environ.get("JUDGE_CAL_PORT", "18765"))
CTX = 4096
SEED = 42
TEMPERATURE = 0.0
BASE = f"http://127.0.0.1:{PORT}"

REQUIRED_FIELDS = (
    "id",
    "kind",
    "fixture_ref",
    "thread_id",
    "source",
    "candidate",
    "intended_quality",
    "human_grades",
    "rubric",
)


def _thread_source(thread: dict[str, Any]) -> str:
    parts = [f"Subject: {thread.get('subject', '')}"]
    for msg in thread.get("messages", []):
        frm = msg.get("from", "")
        body = str(msg.get("body", "")).strip()
        parts.append(f"From: {frm}\n{body}")
    return "\n\n".join(parts)


def _is_injection(thread: dict[str, Any]) -> bool:
    tid = str(thread.get("thread_id", "")).lower()
    if "injection" in tid:
        return True
    for msg in thread.get("messages") or []:
        for lab in msg.get("labels") or []:
            if "injection" in str(lab).lower():
                return True
    return False


def _eligible_threads() -> list[tuple[str, dict[str, Any]]]:
    """Multi-message, non-injection threads, round-robin across mailboxes."""
    pools: list[tuple[str, list[dict[str, Any]]]] = []
    for ref in PERSONAS:
        mailbox = load(ref)
        assert isinstance(mailbox, dict)
        threads = [
            t
            for t in mailbox["threads"]
            if len(t.get("messages") or []) >= 2 and not _is_injection(t)
        ]
        threads.sort(key=lambda t: str(t["thread_id"]))
        pools.append((ref, threads))

    picked: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    idx = 0
    # Prefer spreading: take idx-th from each pool in turn.
    while len(picked) < 50:
        ref, threads = pools[idx % len(pools)]
        t_i = idx // len(pools)
        if t_i >= len(threads):
            # Exhausted this depth; if all exhausted, break later.
            idx += 1
            if idx > 10_000:
                break
            continue
        thread = threads[t_i]
        key = f"{ref}:{thread['thread_id']}"
        idx += 1
        if key in seen:
            continue
        seen.add(key)
        picked.append((ref, thread))
    if len(picked) < 50:
        raise RuntimeError(f"only {len(picked)} eligible threads; need 50")
    return picked


def _defect_note_for(quality: str, kind: str, n: int) -> str | None:
    if quality != "invented_fact":
        return None
    # Deterministic false specifics the model is told to insert.
    notes = [
        "the CFO already approved a $2.4M budget increase yesterday",
        "Legal signed the amendment at 09:15 this morning",
        "the board voted 7-0 to cancel the project last Friday",
        "finance wired a $75,000 vendor deposit on Tuesday",
        "HR confirmed the hiring freeze ends on December 1",
    ]
    return notes[n % len(notes)]


def defect_key_tokens(note: str) -> list[str]:
    """Numbers, proper roles, times, and dates that must appear in candidate."""
    tokens: list[str] = []
    tokens += re.findall(r"\$[\d,.]+(?:[MmKk])?", note)
    tokens += re.findall(r"\b\d{1,2}:\d{2}\b", note)
    tokens += re.findall(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2}\b",
        note,
        flags=re.I,
    )
    tokens += re.findall(r"\b\d+-\d+\b", note)
    tokens += re.findall(r"\b(?:CFO|Legal|HR|finance|board)\b", note, flags=re.I)
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        key = tok.lower()
        if key not in seen:
            seen.add(key)
            out.append(tok)
    return out


def candidate_has_defect(candidate: str, note: str) -> bool:
    if note.lower() in candidate.lower():
        return True
    tokens = defect_key_tokens(note)
    if not tokens:
        return False
    low = candidate.lower()
    return all(tok.lower() in low for tok in tokens)


def ensure_defect_in_candidate(candidate: str, note: str) -> str:
    """If the model omitted the false detail, append it so the label stays valid."""
    if candidate_has_defect(candidate, note):
        return candidate
    insert = note[:1].upper() + note[1:] if note else note
    return f"{candidate.rstrip()} {insert}."


def _prompt(
    *,
    kind: str,
    quality: str,
    source: str,
    defect_note: str | None,
) -> str:
    if kind == "summary":
        task = "Write a short email-thread summary (3-5 sentences)."
    else:
        task = "Write a short reply draft to the latest message (one email body)."

    instructions = {
        "model_plain": (
            "Be faithful to the thread. Cover the main ask, owners, and any "
            "deadline. Do not invent facts."
        ),
        "vague": (
            "Be intentionally vague: avoid names, amounts, dates, and concrete "
            "asks. Do not invent facts."
        ),
        "wrong_tone": (
            "Use an inappropriate tone (flippant, slangy, or dismissive) that "
            "does not fit a professional reply/summary. Do not invent facts."
        ),
        "invented_fact": (
            f"Write a mostly plausible {kind}, but insert exactly this false "
            f"detail once (it is NOT in the thread): \"{defect_note}\". "
            "Do not invent other facts."
        ),
        "missing_ask": (
            "Mention the topic but omit the primary ask / next step the thread "
            "is waiting on. Do not invent facts."
        ),
    }
    return (
        f"{task}\n"
        f"Constraint: {instructions[quality]}\n"
        "Reply with the summary or draft text only. No preamble, no markdown fence.\n\n"
        f"THREAD:\n{source}"
    )


def _chat(prompt: str) -> str:
    body = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "seed": SEED,
        "stream": False,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    return str(content or "").strip()


def _kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return
    pids = [p.strip() for p in out.split() if p.strip()]
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def _wait_healthy(timeout_s: float = 90.0) -> None:
    deadline = time.time() + timeout_s
    url = f"{BASE}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"llama-server not healthy on {url}")


def _start_server() -> subprocess.Popen[str]:
    if not GGUF.is_file():
        raise FileNotFoundError(f"GGUF missing: {GGUF}")
    _kill_port(PORT)
    log_path = OUT_DIR / "llama-server.regen.log"
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            "llama-server",
            "-m",
            str(GGUF),
            "-c",
            str(CTX),
            "--port",
            str(PORT),
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
        text=True,
    )
    try:
        _wait_healthy()
    except Exception:
        proc.terminate()
        _kill_port(PORT)
        log_f.close()
        raise
    proc._cal_log_f = log_f  # type: ignore[attr-defined]
    return proc


def _stop_server(proc: subprocess.Popen[str] | None) -> None:
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_f = getattr(proc, "_cal_log_f", None)
        if log_f is not None:
            log_f.close()
    _kill_port(PORT)


def build_rows() -> list[dict[str, Any]]:
    threads = _eligible_threads()
    summary_threads = threads[:25]
    draft_threads = threads[25:50]

    rows: list[dict[str, Any]] = []
    next_id = 1

    def add_kind(kind: str, pool: list[tuple[str, dict[str, Any]]]) -> None:
        nonlocal next_id
        slot = 0
        for quality in QUALITIES:
            for j in range(PER_QUALITY):
                ref, thread = pool[slot]
                source = _thread_source(thread)
                defect_note = _defect_note_for(quality, kind, j)
                prompt = _prompt(
                    kind=kind,
                    quality=quality,
                    source=source,
                    defect_note=defect_note,
                )
                print(f"generate {kind} {quality} {next_id}/50 …", flush=True)
                candidate = _chat(prompt)
                if not candidate:
                    raise RuntimeError(f"empty model output for row {next_id}")
                if defect_note is not None:
                    if not candidate_has_defect(candidate, defect_note):
                        # One retry with an explicit insertion order.
                        retry = (
                            f"Rewrite your answer and you MUST include this exact "
                            f"false detail verbatim: \"{defect_note}\".\n\n"
                            f"Previous answer:\n{candidate}"
                        )
                        candidate = _chat(retry) or candidate
                    candidate = ensure_defect_in_candidate(candidate, defect_note)
                row: dict[str, Any] = {
                    "id": f"jcal-{next_id:04d}",
                    "kind": kind,
                    "fixture_ref": ref,
                    "thread_id": thread["thread_id"],
                    "source": source,
                    "candidate": candidate,
                    "intended_quality": quality,
                    "human_grades": None,
                    "rubric": (
                        "summary_faithfulness" if kind == "summary" else "draft_quality"
                    ),
                }
                if defect_note is not None:
                    row["defect_note"] = defect_note
                rows.append(row)
                next_id += 1
                slot += 1

    add_kind("summary", summary_threads)
    add_kind("draft", draft_threads)
    assert len(rows) == 50
    return rows


def validate_rows(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 50:
        raise SystemExit(f"expected 50 rows, got {len(rows)}")
    kinds = {"summary": 0, "draft": 0}
    qualities: dict[str, int] = {}
    refs: set[str] = set()
    for i, row in enumerate(rows, start=1):
        missing = [k for k in REQUIRED_FIELDS if k not in row]
        if missing:
            raise SystemExit(f"row {i} missing fields: {missing}")
        if row["id"] != f"jcal-{i:04d}":
            raise SystemExit(f"row {i} id {row['id']!r} != jcal-{i:04d}")
        if row["human_grades"] is not None:
            raise SystemExit(f"row {i} human_grades must be null")
        if row["kind"] not in kinds:
            raise SystemExit(f"row {i} bad kind")
        kinds[row["kind"]] += 1
        q = row["intended_quality"]
        if q not in QUALITIES:
            raise SystemExit(f"row {i} bad intended_quality {q!r}")
        qualities[q] = qualities.get(q, 0) + 1
        refs.add(row["fixture_ref"])
        if q == "invented_fact":
            note = row.get("defect_note")
            if not isinstance(note, str) or not note.strip():
                raise SystemExit(f"row {i} invented_fact needs non-empty defect_note")
            if not candidate_has_defect(str(row.get("candidate", "")), note):
                raise SystemExit(
                    f"row {i} candidate missing defect_note tokens {defect_key_tokens(note)!r}"
                )
        if "injection" in str(row["thread_id"]).lower():
            raise SystemExit(f"row {i} uses injection thread")
        if row["kind"] == "summary" and row["rubric"] != "summary_faithfulness":
            raise SystemExit(f"row {i} bad rubric")
        if row["kind"] == "draft" and row["rubric"] != "draft_quality":
            raise SystemExit(f"row {i} bad rubric")
    if kinds != {"summary": 25, "draft": 25}:
        raise SystemExit(f"kind counts {kinds}")
    for q in QUALITIES:
        if qualities.get(q, 0) != 10:  # 5 summary + 5 draft
            raise SystemExit(f"quality {q} count {qualities.get(q)}")
    if set(PERSONAS) - refs:
        raise SystemExit(f"missing mailbox coverage: {set(PERSONAS) - refs}")
    print(f"validated {len(rows)} rows OK", flush=True)


def load_rows(path: Path = OUT_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="call local llama-server and rewrite cases.jsonl",
    )
    args = parser.parse_args(argv)

    if not args.regenerate:
        if not OUT_PATH.is_file():
            print(f"missing {OUT_PATH}; run with --regenerate", file=sys.stderr)
            return 1
        validate_rows(load_rows())
        return 0

    proc: subprocess.Popen[str] | None = None
    try:
        print(f"starting llama-server port={PORT} model={GGUF.name}", flush=True)
        proc = _start_server()
        rows = build_rows()
        OUT_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        print(f"wrote {len(rows)} rows -> {OUT_PATH}", flush=True)
        validate_rows(rows)
        return 0
    finally:
        print("stopping llama-server", flush=True)
        _stop_server(proc)

if __name__ == "__main__":
    raise SystemExit(main())
