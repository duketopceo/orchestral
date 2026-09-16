"""Pure domain state for the web observatory — no http imports, unit-testable.

Same rule as ``orchestral.tui.state``: this module owns derivation and the
job lifecycle; the server layer only routes requests to it. Run/event
derivation itself is reused from ``orchestral.tui.state`` (verified
textual-free) so the two surfaces can never disagree about what a run is
doing.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, find_task, load_models, load_task, load_yaml
from orchestral.runner import Runner
from orchestral.stats import pairing_leaderboard
from orchestral.storage import RunStore
from orchestral.tui.state import (
    Job,
    JobStatus,
    event_detail,
    event_row,
    filter_runs,
    fmt_elapsed,
    live_totals,
    run_phase,
    sort_leaderboard,
    tail_events,
    worker_states,
)

# Fields the launch form may set — anything else in a POST is rejected.
LAUNCH_FIELDS = frozenset({
    "task", "orchestrator", "worker", "judge", "replicates", "seed", "dry_run",
})


class JobRegistry:
    """Tracks serve-launched runs. Mirrors the TUI's job model: each job
    carries a cancel_event handed to the Runner, so cancel is the same
    mechanism — only runs this process started are cancellable.
    """

    def __init__(self, runs_dir: Path, tasks_dir: Path, models_dir: Path, store: RunStore):
        self.runs_dir = Path(runs_dir)
        self.tasks_dir = Path(tasks_dir)
        self.models_dir = Path(models_dir)
        self.store = store
        self.jobs: list[Job] = []
        self._lock = threading.Lock()

    def launch(self, spec: dict[str, Any]) -> Job:
        """Validate the launch spec and start a runner thread."""
        unknown = set(spec) - LAUNCH_FIELDS
        if unknown:
            raise ValueError(f"unknown launch fields: {sorted(unknown)}")
        for required in ("task", "orchestrator", "worker"):
            if not spec.get(required):
                raise ValueError(f"missing launch field: {required}")
        replicates = int(spec.get("replicates") or 1)
        if replicates < 1:
            raise ValueError("replicates must be >= 1")
        seed = spec.get("seed")
        seed = int(seed) if seed not in (None, "") else None

        label = f"{spec['task']}·{spec['worker'].split('/')[-1]}"
        if replicates > 1:
            label += f"×{replicates}"
        job = Job(label=label)
        with self._lock:
            self.jobs.append(job)
        threading.Thread(
            target=self._execute, args=(job, spec, replicates, seed),
            name=f"web-job-{label}", daemon=True,
        ).start()
        return job

    def cancel_run(self, run_id: str) -> bool:
        """Cancel the job that produced run_id. False if unknown/finished."""
        job = self.job_for_run(run_id)
        return job.cancel() if job else False

    def job_for_run(self, run_id: str) -> Job | None:
        for job in self.jobs:
            if run_id in job.run_ids:
                return job
        return None

    def active(self) -> list[Job]:
        return [j for j in self.jobs if j.active]

    def _execute(self, job: Job, spec: dict[str, Any], replicates: int, seed: int | None) -> None:
        """Runner thread — same launch loop as tui/app.py::_execute."""
        try:
            task_path = find_task(spec["task"], str(self.tasks_dir))
            if task_path is None:
                raise FileNotFoundError(f"task '{spec['task']}' not found in {self.tasks_dir}")
            task = load_task(task_path)
            models = {m.slug: m for m in load_models(self.models_dir)}
            orchestrator = models.get(spec["orchestrator"]) or ModelConfig(
                slug=spec["orchestrator"], name=spec["orchestrator"], role="orchestrator",
                input_price_per_mtok=0.03, output_price_per_mtok=0.10,
            )
            worker = models.get(spec["worker"]) or ModelConfig(
                slug=spec["worker"], name=spec["worker"], role="worker",
                input_price_per_mtok=0.03, output_price_per_mtok=0.10,
            )
            judge = models.get(spec["judge"]) if spec.get("judge") else None
        except Exception as exc:
            self._done(job, JobStatus.FAILED, f"setup failed: {exc}")
            return

        job.transition(JobStatus.RUNNING)
        group = f"web-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}" if replicates > 1 else None
        try:
            for i in range(1, replicates + 1):
                if job.cancel_event.is_set():
                    self._done(job, JobStatus.CANCELLED, "cancelled before all replicates")
                    return
                meta = Runner(
                    dry_run=bool(spec.get("dry_run")),
                    runs_dir=str(self.runs_dir),
                    store=self.store,
                    run_group=group,
                    replicate=i if replicates > 1 else None,
                    seed=(seed + i - 1) if seed is not None else None,
                    cancel_event=job.cancel_event,
                    on_run_created=job.run_ids.append,
                ).run(task, orchestrator, worker, judge)
                if meta.run_id not in job.run_ids:
                    job.run_ids.append(meta.run_id)
                if meta.status == "cancelled":
                    self._done(job, JobStatus.CANCELLED, f"run {meta.run_id} cancelled")
                    return
            self._done(job, JobStatus.SUCCEEDED, f"{len(job.run_ids)} run(s)")
        except Exception as exc:
            self._done(job, JobStatus.FAILED, str(exc)[:120])

    def _done(self, job: Job, status: JobStatus, detail: str) -> None:
        with contextlib.suppress(ValueError):
            job.transition(status, detail)


def read_json(path: Path) -> Any | None:
    """Guarded JSON read — malformed or missing files return None, never raise."""
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def live_payload(run_dir: Path, after: int, started_at: str | None = None,
                 meta: Any = None) -> dict[str, Any]:
    """Poll payload for the live view.

    ``after`` is an event-count cursor: the client has already rendered the
    first ``after`` events, so only events past it are sent. A cursor past
    the file's length means the file was rewritten — resync by resending
    everything. events.jsonl is append-only, so a count cursor is safe and
    lets derivation and the new-events slice come from a single read.
    """
    run_dir = Path(run_dir)
    events, _ = tail_events(run_dir / "events.jsonl", 0)
    new_events = events[after:] if after <= len(events) else events
    cost, tokens = live_totals(events)
    report = read_json(run_dir / "report.json") or {}
    return {
        "rows": [event_row(ev) for ev in new_events],
        "details": [event_detail(ev) for ev in new_events],
        "next": len(events),
        "phase": run_phase(events),
        "workers": worker_states(events),
        "cost_usd": cost,
        "tokens": tokens,
        "elapsed": fmt_elapsed(started_at),
        "status": getattr(meta, "status", None),
        "passes": getattr(meta, "passes", None),
        "score": getattr(meta, "score", report.get("score")),
    }


def run_sections(run_dir: Path) -> dict[str, Any]:
    """Detail-tab contents: events, metrics, report, plan, manifest."""
    run_dir = Path(run_dir)
    events, _ = tail_events(run_dir / "events.jsonl", 0)
    plan_path = run_dir / "plan.md"
    return {
        "events": [event_row(ev) for ev in events],
        "metrics": read_json(run_dir / "metrics.json"),
        "report": read_json(run_dir / "report.json"),
        "plan": plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.exists() else None,
        "manifest": read_json(run_dir / "manifest.json"),
    }


def leaderboard_rows(store: RunStore, sort: str = "cost_per_pass") -> list[dict[str, Any]]:
    rows = pairing_leaderboard(store.list_runs(limit=None))
    return [r.to_dict() for r in sort_leaderboard(rows, sort)]


def overview_payload(store: RunStore, registry: JobRegistry) -> dict[str, Any]:
    """Mission-control data: live jobs, leaderboard top rows, recent runs."""
    runs = store.list_runs(limit=None)
    lb = pairing_leaderboard(runs)
    return {
        "jobs": [
            {
                "label": j.label, "status": str(j.status), "detail": j.detail,
                "run_ids": list(j.run_ids), "cancellable": j.active,
            }
            for j in registry.jobs
        ],
        "leaderboard": [r.to_dict() for r in lb[:10]],
        "recent": [r.to_dict() for r in runs[:10]],
    }


def history_rows(store: RunStore, query: str = "") -> list[Any]:
    return filter_runs(store.list_runs(limit=None), query)


def task_choices(tasks_dir: Path) -> list[str]:
    """Task ids from spec files — same rule as the TUI launch modal."""
    ids: list[str] = []
    for f in sorted(Path(tasks_dir).rglob("*.yaml")):
        try:
            data = load_yaml(f)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("id"):
            ids.append(data["id"])
    return ids


def model_choices(models_dir: Path, role: str) -> list[str]:
    try:
        return sorted(m.slug for m in load_models(models_dir) if m.role == role)
    except Exception:
        return []
