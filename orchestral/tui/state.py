"""Pure domain state for the TUI — no textual imports, unit-testable.

Owns the things views must not: the job lifecycle, run filtering, event
stream derivation, and display formatting. Views render this state and
dispatch intents back.
"""

from __future__ import annotations

import json
import locale
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from rich.text import Text

from orchestral.format import NULL_GLYPH, fmt_duration_ms, fmt_money
from orchestral.format import fmt_tokens as fmt_tokens  # re-export
from orchestral.glyphs import GLYPHS, glyph

# Fields a launch spec may carry — must equal web.state.LAUNCH_FIELDS
# (the parity test asserts it; divergence fails in CI, not production).
# Executor opt-in is deliberately absent: it is a launch-context flag,
# never a per-request field.
LAUNCH_SPEC_FIELDS = frozenset({
    "task", "orchestrator", "worker", "judge", "replicates", "seed", "dry_run",
})


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


# Shared formatter (U5): one contract across web, TUI and CLI.
fmt_ms = fmt_duration_ms
fmt_cost = fmt_money


def fmt_usd_range(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "Unknown"
    return f"{fmt_money(lo)} to {fmt_money(hi)}"


def format_spend_estimate(est: dict[str, Any]) -> str:
    """The launch-estimate payload as the confirm panel's lines.

    Row for row what the web confirm dialog shows (``ui/js/views/new.js``): runs,
    estimated cost ("about" a figure, ``$0.00`` for a dry run, "Unknown" when
    there is no billed history, never $0), range, basis, month-to-date billed
    spend against the eval cap, then the caveat.
    """
    runs = est.get("replicates") or 1
    total = est.get("total_usd")
    if total is None:
        cost = "Unknown"
    elif total == 0:
        cost = fmt_money(0)
    else:
        cost = f"about {fmt_money(total)}"
    lines = [
        f"Runs: {runs}",
        f"Estimated cost: {cost}",
        f"Range: {fmt_usd_range(est.get('total_low_usd'), est.get('total_high_usd'))}",
        f"Estimate basis: {est.get('basis_label') or NULL_GLYPH}",
    ]
    if est.get("month_to_date_billed_usd") is not None:
        lines.append(
            f"Spent this month: ${float(est['month_to_date_billed_usd']):.2f} of "
            f"${float(est.get('monthly_cap_usd') or 0):.0f} eval cap "
            "(billed spend recorded in this index)")
    if est.get("caveat"):
        lines.append(str(est["caveat"]))
    return "\n".join(lines)


def use_ascii() -> bool:
    """ASCII glyphs unless the terminal can show Unicode (same rules as the CLI).

    Unicode needs a UTF-8 stream, ``TERM`` other than ``dumb`` and ``ORCH_ASCII``
    unset. ``NO_COLOR`` is not consulted: it drops colour only. The TUI is
    always on a terminal, so there is no isatty test here.
    """
    if os.environ.get("ORCH_ASCII") or os.environ.get("TERM") == "dumb":
        return True
    enc = getattr(sys.stdout, "encoding", None) or locale.getpreferredencoding(False)
    return "utf" not in str(enc).lower()


Label = tuple[str, str, str]  # (glyph, word, colour role from ui/tokens.css)


def _label(name: str, word: str | None, ascii_only: bool | None) -> Label:
    row = GLYPHS[name]
    return (glyph(name, ascii_only=use_ascii() if ascii_only is None else ascii_only),
            word or row.word, row.role)


def pass_label(passes: bool | None, ascii_only: bool | None = None) -> Label:
    """(glyph, word, role). The word always travels with the glyph; colour only decorates."""
    if passes is True:
        return _label("pass", None, ascii_only)
    if passes is False:
        return _label("fail", None, ascii_only)
    return ("", NULL_GLYPH, "ink-3")


def status_label(status: str, ascii_only: bool | None = None) -> Label:
    """A run status as (glyph, word, role); ``finished`` is a word with no verdict glyph."""
    if status == "running":
        return _label("live", "running", ascii_only)
    if status == "failed":
        return _label("fail", "failed", ascii_only)
    if status == "cancelled":
        return _label("cancelled", None, ascii_only)
    if status == "finished":
        return ("", "finished", "ink-2")
    return ("", status or NULL_GLYPH, "ink-3")


def label_plain(label: Label) -> str:
    return f"{label[0]} {label[1]}".strip()


def verdict_cell(label: Label, tokens: dict[str, str]) -> Text:
    """A table cell: glyph and word, coloured by role unless ``NO_COLOR`` is set."""
    text = label_plain(label)
    if os.environ.get("NO_COLOR"):
        return Text(text)
    colour = tokens.get(f"--{label[2]}-text") or tokens.get(f"--{label[2]}")
    return Text(text, style=colour or "")


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
        detail = fmt_money(float(out.get('total_cost_usd') or 0))
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
        return NULL_GLYPH
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at) if finished_at else datetime.now(UTC)
        secs = max(0, int((end - start).total_seconds()))
        m, s = divmod(secs, 60)
        return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m:02d}:{s:02d}"
    except (ValueError, TypeError):
        return NULL_GLYPH


LB_SORTS: tuple[str, ...] = ("cost_per_pass", "pass_rate", "judge_score_median", "cost_median", "duration_median_ms")

_LB_DESC: frozenset[str] = frozenset({"pass_rate", "judge_score_median"})


def sort_leaderboard(rows: list[Any], key: str) -> list[Any]:
    """Sort PairingAggregate rows; None metrics always sort last.

    Low-sample rows never rank — they tail every ordering, so a 1-run
    pairing can't claim a podium slot on any sort key.
    """
    d = [r.to_dict() if hasattr(r, "to_dict") else r for r in rows]
    if key in _LB_DESC:
        order = sorted(zip(d, rows, strict=True), key=lambda t: (bool(t[0].get("low_sample")), t[0].get(key) is None, -(t[0].get(key) or 0)))
    else:
        order = sorted(zip(d, rows, strict=True), key=lambda t: (bool(t[0].get("low_sample")), t[0].get(key) is None, t[0].get(key) or 0))
    return [r for _d, r in order]
