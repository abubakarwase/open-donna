"""Eval runner: one long-lived SUT process, JSONL stdin/stdout, scorecard out.

Restart policy: prefer one long-lived SUT. If the process dies mid-run, restart
once for the remaining cases. Per-case timeout (default 60s) terminates the
child when needed; the runner never crashes on bad SUT output.

Also records wall-clock latency per case and samples SUT-process-tree RSS
every 200 ms for observed perf metrics.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from donna_evals.scorecard import build_scorecard, write_scorecard
from donna_evals.validate import _load_tool_schemas

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

EVALS_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = EVALS_ROOT / "golden"
SPEC_ROOT = EVALS_ROOT / "spec"
SUITE_ORDER = ("router", "email", "calendar", "sheet", "safety")
STRIP_KEYS = frozenset({"expected", "split", "tags"})


def load_cases(suite: str, split: str, limit: int | None = None) -> list[dict[str, Any]]:
    suites = list(SUITE_ORDER) if suite == "all" else [suite]
    cases: list[dict[str, Any]] = []
    for name in suites:
        path = GOLDEN_ROOT / name / f"{split}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"golden suite missing: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            obj = json.loads(line)
            obj["_suite"] = name
            cases.append(obj)
    if limit is not None:
        cases = cases[:limit]
    return cases


def case_for_sut(case: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in case.items() if not k.startswith("_") and k not in STRIP_KEYS}


def failed_trace(case_id: str, error: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "route": {"label": "general", "confidence": 0.0},
        "tool_calls": [],
        "drafts": [],
        "escalation": {"escalated": False, "reason": None, "payload_bytes": 0},
        "executed_side_effects": [],
        "steps": 0,
        "latency_ms": 0,
        "peak_rss_bytes": 0,
        "error": error,
    }


def _trace_validator() -> Draft202012Validator:
    schema = json.loads((SPEC_ROOT / "trace.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _validate_trace(validator: Draft202012Validator, obj: object) -> str | None:
    if not isinstance(obj, dict):
        return "invalid_trace"
    try:
        validator.validate(obj)
    except ValidationError:
        return "invalid_trace"
    return None


def _rss_bytes_for_pid(pid: int) -> int:
    """Sum RSS (bytes) for pid and its children."""
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            total = int(proc.memory_info().rss)
            for child in proc.children(recursive=True):
                try:
                    total += int(child.memory_info().rss)
                except (psutil.Error, ProcessLookupError):
                    pass
            return total
        except (psutil.Error, ProcessLookupError):
            pass
    # Fallback: ps -o rss= (KB) over pid + children via pgrep.
    total_kb = 0
    pids = [pid]
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    for p in pids:
        try:
            out = subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(p)],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            kb = out.strip().split()
            if kb:
                total_kb += int(kb[0])
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            pass
    return total_kb * 1024


class _RssSampler:
    def __init__(self, pid: int, interval_s: float = 0.2) -> None:
        self.pid = pid
        self.interval_s = interval_s
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.peak = _rss_bytes_for_pid(self.pid)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.peak = max(self.peak, _rss_bytes_for_pid(self.pid))
            except Exception:
                pass

    def stop(self) -> int:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.peak = max(self.peak, _rss_bytes_for_pid(self.pid))
        except Exception:
            pass
        return int(self.peak)


class _SutProcess:
    def __init__(self, sut: str) -> None:
        self.sut = sut
        self.proc: subprocess.Popen[str] | None = None
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.sut,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for _ in proc.stderr:
                pass
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def write_case(self, payload: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def readline(self, timeout_s: float) -> str | None:
        """Return a line, '' on EOF, or None on timeout."""
        assert self.proc is not None and self.proc.stdout is not None
        holder: list[str] = []
        err: list[BaseException] = []

        def _read() -> None:
            try:
                holder.append(self.proc.stdout.readline())  # type: ignore[union-attr]
            except BaseException as exc:  # noqa: BLE001
                err.append(exc)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            return None
        if err:
            return ""
        if not holder:
            return ""
        return holder[0]

    def kill(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None


def _annotate_obs(trace: dict[str, Any], wall_ms: float, peak_rss: int) -> dict[str, Any]:
    trace["_latency_ms_wall"] = wall_ms
    trace["_peak_rss_bytes_observed"] = peak_rss
    return trace


def _public_trace(trace: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in trace.items() if not k.startswith("_")}


def _run_cases(
    sut_cmd: str,
    cases: list[dict[str, Any]],
    timeout_s: float,
) -> list[dict[str, Any]]:
    validator = _trace_validator()
    traces: list[dict[str, Any]] = []
    sut = _SutProcess(sut_cmd)
    sut.start()
    restarted = False
    i = 0
    while i < len(cases):
        case = cases[i]
        case_id = case["id"]
        if not sut.alive():
            if not restarted:
                sut.kill()
                sut.start()
                restarted = True
                if not sut.alive():
                    traces.append(failed_trace(case_id, "crash"))
                    i += 1
                    continue
            else:
                traces.append(failed_trace(case_id, "crash"))
                i += 1
                continue

        sampler = _RssSampler(sut.pid) if sut.pid is not None else None
        if sampler is not None:
            sampler.start()
        t0 = time.perf_counter()
        try:
            sut.write_case(case_for_sut(case))
        except Exception:
            wall_ms = (time.perf_counter() - t0) * 1000.0
            peak = sampler.stop() if sampler is not None else 0
            traces.append(_annotate_obs(failed_trace(case_id, "crash"), wall_ms, peak))
            sut.kill()
            if not restarted:
                sut.start()
                restarted = True
            i += 1
            continue

        line = sut.readline(timeout_s)
        wall_ms = (time.perf_counter() - t0) * 1000.0
        peak = sampler.stop() if sampler is not None else 0

        if line is None:
            sut.kill()
            traces.append(_annotate_obs(failed_trace(case_id, "timeout"), wall_ms, peak))
            if not restarted:
                sut.start()
                restarted = True
            i += 1
            continue

        if line == "":
            traces.append(_annotate_obs(failed_trace(case_id, "crash"), wall_ms, peak))
            sut.kill()
            if not restarted:
                sut.start()
                restarted = True
            i += 1
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            traces.append(
                _annotate_obs(failed_trace(case_id, "invalid_trace"), wall_ms, peak)
            )
            i += 1
            continue

        err = _validate_trace(validator, obj)
        if err:
            traces.append(_annotate_obs(failed_trace(case_id, err), wall_ms, peak))
            i += 1
            continue

        if obj.get("case_id") != case_id:
            traces.append(
                _annotate_obs(failed_trace(case_id, "invalid_trace"), wall_ms, peak)
            )
            i += 1
            continue

        traces.append(_annotate_obs(obj, wall_ms, peak))
        i += 1

    sut.kill()
    return traces


def _load_sidecars() -> dict[str, Any]:
    slot_path = GOLDEN_ROOT / "calendar" / "slot_truth.json"
    triage_path = GOLDEN_ROOT / "email" / "triage_truth.json"
    return {
        "slot_truth": json.loads(slot_path.read_text(encoding="utf-8"))
        if slot_path.is_file()
        else {},
        "triage_truth": json.loads(triage_path.read_text(encoding="utf-8"))
        if triage_path.is_file()
        else {},
    }


def run_eval(
    *,
    sut: str,
    suite: str,
    split: str,
    limit: int | None,
    timeout_s: float,
    judge_url: str | None,
    judge_model: str | None,
    out_dir: Path,
) -> int:
    cases = load_cases(suite, split, limit)
    traces = _run_cases(sut, cases, timeout_s)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    traces_path = out_dir / "traces.jsonl"
    with traces_path.open("w", encoding="utf-8") as fh:
        for tr in traces:
            fh.write(json.dumps(_public_trace(tr), separators=(",", ":")) + "\n")

    sidecars = _load_sidecars()
    ctx = {
        "tool_schemas": _load_tool_schemas(),
        "slot_truth": sidecars["slot_truth"],
        "triage_truth": sidecars["triage_truth"],
        "judge_url": judge_url,
        "judge_model": judge_model,
        "evals_root": EVALS_ROOT,
    }
    scorecard = build_scorecard(cases, traces, ctx)
    write_scorecard(out_dir, scorecard)
    return 0
