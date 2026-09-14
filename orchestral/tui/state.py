"""Pure domain state for the TUI — no textual imports, unit-testable.

Owns the things views must not: the job lifecycle, run filtering, event
stream derivation, and display formatting. Views render this state and
dispatch intents back.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL: frozenset[JobStatus] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)

# legal transitions — a job never goes backwards
_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(TERMINAL),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


@dataclass
class Job:
    """One launched run (or replicate batch) tracked by the TUI."""

    label: str
    status: JobStatus = JobStatus.QUEUED
    detail: str = ""
    run_ids: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def transition(self, to: JobStatus, detail: str = "") -> None:
        if to not in _TRANSITIONS[self.status]:
            raise ValueError(f"illegal job transition {self.status} -> {to}")
        self.status = to
        if detail:
            self.detail = detail

    def cancel(self) -> bool:
        """Signal cancellation. Returns False if already terminal."""
        if self.status in TERMINAL:
            return False
        self.cancel_event.set()
        self.detail = "cancelling…"
        return True

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL


def filter_runs(runs: list[Any], query: str) -> list[Any]:
    """Substring filter over the fields a user would actually type."""
    q = query.strip().lower()
    if not q:
        return runs
    return [
        r for r in runs
        if q in r.run_id.lower()
        or q in r.task_id.lower()
        or q in r.orchestrator.lower()
        or q in r.worker.lower()
        or q in (r.run_group or "").lower()
        or q in r.status.lower()
        or (r.failure_reason and q in r.failure_reason.lower())
    ]


def fmt_ms(ms: float | None) -> str:
    if not ms:
        return "-"
    return f"{ms / 1000:.1f}s" if ms >= 10_000 else f"{ms:.0f}ms"


def fmt_cost(usd: float | None) -> str:
    return "-" if usd is None else f"${usd:.4f}"


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "-"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def pass_label(passes: bool | None) -> tuple[str, str]:
    """(text, css-class) — text carries the meaning, color only decorates."""
    if passes is True:
        return "pass", "ok"
    if passes is False:
        return "fail", "err"
    return "-", "muted"


def status_label(status: str) -> tuple[str, str]:
    return {
        "finished": ("finished", "ok"),
        "running": ("running", "warn"),
        "failed": ("failed", "err"),
        "cancelled": ("cancelled", "warn"),
    }.get(status, (status, "muted"))


# ---------------------------------------------------------------------------
# Live-run event derivation — the observatory's read model.
# These consume schema-v2 events.jsonl; they never call providers or the runner.
# ---------------------------------------------------------------------------


def tail_events(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Incrementally read new events from a JSONL file.

    Returns (new_events, new_offset). A trailing partial line (the writer is
    mid-flush) is left unread so the next poll picks it up complete; a file
    that shrank (rotation/re-run) restarts from 0. Malformed lines surface as
    {"type": "malformed"} rather than raising.
    """
    if not path.exists():
        return [], 0
    if offset > path.stat().st_size:
        offset = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk:
        return [], offset
    lines = chunk.splitlines()
    new_offset = offset + len(chunk.encode("utf-8", errors="replace"))
    if not chunk.endswith("\n"):
        last_nl = chunk.rfind("\n")
        new_offset = offset + (len(chunk[: last_nl + 1].encode("utf-8", errors="replace")) if last_nl >= 0 else 0)
        lines = lines[:-1]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"type": "malformed", "raw": line[:200]})
    return events, new_offset


_PHASE_BY_EVENT: dict[str, str] = {
    "run.created": "starting",
    "task.loaded": "starting",
    "run.started": "starting",
    "orchestrator.started": "planning",
    "orchestrator.completed": "planning",
    "delegation.created": "delegating",
    "worker.started": "delegating",
    "worker.progress": "delegating",
    "worker.completed": "delegating",
    "worker.failed": "delegating",
    "synthesis.started": "assembling",
    "synthesis.completed": "assembling",
    "evaluation.started": "evaluating",
    "evaluation.completed": "evaluating",
    "usage.recorded": "accounting",
    "run.completed": "finished",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}

TERMINAL_PHASES: frozenset[str] = frozenset({"finished", "failed", "cancelled"})


def run_phase(events: list[dict[str, Any]]) -> str:
    """Coarse phase of the run from the most recent lifecycle event."""
    for ev in reversed(events):
        phase = _PHASE_BY_EVENT.get(ev.get("type", ""))
        if phase:
            return phase
    return "starting"


def worker_states(events: list[dict[str, Any]]) -> dict[str, str]:
    """worker_id -> latest status, derived from lifecycle events."""
    states: dict[str, str] = {}
    for ev in events:
        wid = ev.get("worker_id")
        if not wid:
            continue
        t = ev.get("type")
        if t == "worker.started":
            states[wid] = "running"
        elif t == "worker.progress":
            attempt = (ev.get("output") or {}).get("attempt", "?")
            states[wid] = f"running a{attempt}"
        elif t == "worker.completed":
            states[wid] = "done"
        elif t == "worker.failed":
            states[wid] = "failed"
    return states


def live_totals(events: list[dict[str, Any]]) -> tuple[float, int]:
    """Running (cost_usd, total_tokens) summed from llm_call events."""
    cost = 0.0
    toks = 0
    for ev in events:
        if ev.get("type") == "llm_call":
            c = ev.get("cost") or {}
            cost += float(c.get("usd") or 0)
            toks += int(c.get("input_tokens") or 0) + int(c.get("output_tokens") or 0)
    return cost, toks


def event_row(ev: dict[str, Any]) -> tuple[str, str, str, str]:
    """(time, type, worker, detail) row for the live trace table."""
    ts = (ev.get("timestamp") or "")[11:19]
    etype = ev.get("type", "?")
    wid = ev.get("worker_id") or "-"
    out = ev.get("output") or {}
    detail = ""
    if etype == "worker.started":
        detail = str(out.get("description", ""))[:60]
    elif etype == "worker.progress":
        detail = f"attempt {out.get('attempt', '?')}"
    elif etype == "worker.completed":
        detail = f"{out.get('attempts', '?')} attempt(s)"
    elif etype == "worker.failed":
        detail = str(out.get("error_category") or out.get("error") or "")[:60]
    elif etype == "delegation.created":
        detail = f"{out.get('subtasks', '?')} subtasks"
    elif etype == "orchestrator.completed":
        detail = fmt_ms(out.get("latency_ms"))
    elif etype == "evaluation.completed":
        detail = f"passes={out.get('passes')} score={out.get('score')}"
    elif etype == "usage.recorded":
        detail = f"${float(out.get('total_cost_usd') or 0):.4f}"
    elif etype == "artifact.saved":
        detail = str(out.get("path") or out.get("name") or "")[:60]
    elif etype == "llm_call":
        c = ev.get("cost") or {}
        detail = f"{ev.get('model', '')} ${float(c.get('usd') or 0):.5f} {fmt_ms(ev.get('latency_ms'))}"
    elif etype == "worker_error":
        detail = (ev.get("error") or "")[:60]
    elif etype == "worker_retry":
        detail = f"retry {out.get('attempt', '?')}/{out.get('max_attempts', '?')}"
    elif etype in ("run.completed", "run.failed", "run.cancelled"):
        detail = str(out.get("status") or out.get("error") or "")[:60]
    else:
        detail = (ev.get("reasoning") or ev.get("error") or "")[:60]
    return ts, etype, wid, detail


def event_detail(ev: dict[str, Any]) -> str:
    """Multi-line rendering of one event for the inspection panel."""
    lines = [f"{ev.get('timestamp', '')}  {ev.get('type', '?')}"]
    if ev.get("worker_id"):
        lines.append(f"worker: {ev['worker_id']}")
    if ev.get("model"):
        lines.append(f"model: {ev['model']}")
    for key in ("input", "output", "reasoning", "error"):
        val = ev.get(key)
        if val:
            text = val if isinstance(val, str) else json.dumps(val, default=str)
            lines.append(f"{key}: {text[:500]}")
    c = ev.get("cost")
    if c:
        lines.append(f"cost: {json.dumps(c)}")
    return "\n".join(lines)


def fmt_elapsed(started_at: str | None, finished_at: str | None = None) -> str:
    """'MM:SS' (or 'H:MM:SS') between ISO timestamps; live if unfinished."""
    if not started_at:
        return "-"
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at) if finished_at else datetime.now(UTC)
        secs = max(0, int((end - start).total_seconds()))
        m, s = divmod(secs, 60)
        return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m:02d}:{s:02d}"
    except (ValueError, TypeError):
        return "-"


LB_SORTS: tuple[str, ...] = ("cost_per_pass", "pass_rate", "score_median", "cost_median", "duration_median_ms")

_LB_DESC: frozenset[str] = frozenset({"pass_rate", "score_median"})


def sort_leaderboard(rows: list[Any], key: str) -> list[Any]:
    """Sort PairingAggregate rows; None metrics always sort last."""
    d = [r.to_dict() if hasattr(r, "to_dict") else r for r in rows]
    if key in _LB_DESC:
        order = sorted(zip(d, rows, strict=True), key=lambda t: (t[0].get(key) is None, -(t[0].get(key) or 0)))
    else:
        order = sorted(zip(d, rows, strict=True), key=lambda t: (t[0].get(key) is None, t[0].get(key) or 0))
    return [r for _d, r in order]
