"""Pure domain state for the TUI — no textual imports, unit-testable.

Owns the things views must not: the job lifecycle, run filtering, and
display formatting. Views render this state and dispatch intents back.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
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
