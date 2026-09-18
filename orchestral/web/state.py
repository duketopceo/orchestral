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
from orchestral.stats import aggregate, pairing_leaderboard
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
    """Mission-control data: live jobs, leaderboard top rows, recent runs,
    group summaries, and the failure taxonomy — the SPA's landing view."""
    runs = store.list_runs(limit=None)
    lb = pairing_leaderboard(runs)
    taxonomy: dict[str, int] = {}
    for r in runs:
        if r.failure_reason:
            taxonomy[r.failure_reason] = taxonomy.get(r.failure_reason, 0) + 1
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
        "groups": groups_payload(store)[:8],
        "taxonomy": dict(sorted(taxonomy.items(), key=lambda kv: -kv[1])),
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


def model_choices(models_dir: Path, role: str | None) -> list[str]:
    """Model slugs for the launch form; role=None lists all — the judge field
    offers every model, same as the TUI (any model can judge)."""
    try:
        return sorted(m.slug for m in load_models(models_dir) if role is None or m.role == role)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# SPA API payloads — the rebuilt observatory reads everything through these.


def runs_payload(
    store: RunStore,
    group: str | None = None,
    task: str | None = None,
    status: str | None = None,
    q: str = "",
) -> list[dict[str, Any]]:
    """Run rows for the filterable table. `status` accepts a lifecycle status
    or `passed`/`failed` (verdict filters)."""
    rows = store.list_runs(run_group=group, task_id=task, limit=None)
    if q:
        rows = filter_runs(rows, q)
    if status == "passed":
        rows = [r for r in rows if r.status == "finished" and r.passes]
    elif status == "failed":
        rows = [r for r in rows if r.status == "failed" or (r.status == "finished" and not r.passes)]
    elif status:
        rows = [r for r in rows if r.status == status]
    return [r.to_dict() for r in rows]


_TIMELINE_PHASE_ORDER = ("plan", "delegate", "assemble", "validate", "judge", "review")


def timeline_payload(run_dir: Path) -> list[dict[str, Any]]:
    """The evidence chain: one node per pipeline phase with cost, latency,
    event count, and error count — derived from events.jsonl so a finished
    run and a live one render the same way."""
    run_dir = Path(run_dir)
    events, _ = tail_events(run_dir / "events.jsonl", 0)
    phases: dict[str, dict[str, Any]] = {}
    for ev in events:
        ph = ev.get("phase") or "other"
        node = phases.setdefault(ph, {
            "phase": ph, "events": 0, "cost_usd": 0.0, "latency_ms": 0.0,
            "errors": 0, "first": None, "last": None,
        })
        node["events"] += 1
        cost = ev.get("cost") or {}
        node["cost_usd"] += cost.get("usd") or 0.0
        node["latency_ms"] += ev.get("latency_ms") or 0.0
        if ev.get("error"):
            node["errors"] += 1
        ts = ev.get("timestamp")
        if node["first"] is None:
            node["first"] = ts
        node["last"] = ts
    ordered = [phases[p] for p in _TIMELINE_PHASE_ORDER if p in phases]
    ordered += [v for k, v in phases.items() if k not in _TIMELINE_PHASE_ORDER]
    return ordered


def artifact_info(run_dir: Path) -> dict[str, Any] | None:
    """Metadata for the stored artifact — the SPA decides how to preview it.
    Zip members come as a listing (never bodies — same rule as the judge)."""
    import zipfile

    run_dir = Path(run_dir)
    artifacts = sorted(run_dir.glob("artifact.*"))
    if not artifacts:
        return None
    p = artifacts[0]
    ext = p.suffix.lstrip(".").lower()
    info: dict[str, Any] = {"name": p.name, "ext": ext, "bytes": p.stat().st_size}
    if ext == "zip":
        try:
            with zipfile.ZipFile(p) as zf:
                info["members"] = [
                    {"name": i.filename, "bytes": i.file_size} for i in zf.infolist()
                ]
        except Exception as exc:
            info["error"] = str(exc)
    return info


def run_detail_payload(store: RunStore, run_id: str) -> dict[str, Any] | None:
    """Everything the run detail view needs in one fetch."""
    meta = store.get_run(run_id)
    if meta is None:
        return None
    run_dir = Path(meta.run_dir)
    plan_path = run_dir / "plan.md"
    return {
        "meta": meta.to_dict(),
        "calls": store.calls_for_run(run_id),
        "report": read_json(run_dir / "report.json"),
        "review": read_json(run_dir / "review.json"),
        "manifest": read_json(run_dir / "manifest.json"),
        "plan": plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.exists() else None,
        "timeline": timeline_payload(run_dir),
        "artifact": artifact_info(run_dir),
    }


def groups_payload(store: RunStore) -> list[dict[str, Any]]:
    """One summary row per run_group — the unit comparisons happen on."""
    groups: dict[str, dict[str, Any]] = {}
    for r in store.list_runs(limit=None):
        g = groups.setdefault(r.run_group or "(ungrouped)", {
            "group": r.run_group or "(ungrouped)",
            "runs": 0, "finished": 0, "passed": 0, "cost_usd": 0.0,
            "scores": [], "tasks": set(), "pairings": set(),
            "latest": None,
        })
        g["runs"] += 1
        if r.status == "finished":
            g["finished"] += 1
            g["passed"] += 1 if r.passes else 0
        g["cost_usd"] += r.total_cost_usd or 0.0
        if r.score is not None:
            g["scores"].append(r.score)
        g["tasks"].add(r.task_id)
        g["pairings"].add((r.orchestrator, r.worker))
        if g["latest"] is None or (r.started_at or "") > (g["latest"] or ""):
            g["latest"] = r.started_at
    out = []
    for g in groups.values():
        scores = sorted(g.pop("scores"))
        n = len(scores)
        out.append({
            **g,
            "tasks": len(g["tasks"]),
            "pairings": len(g["pairings"]),
            "pass_rate": (g["passed"] / g["finished"]) if g["finished"] else None,
            "score_median": scores[n // 2] if n else None,
        })
    out.sort(key=lambda x: x["latest"] or "", reverse=True)
    return out


def compare_payload(store: RunStore, group_a: str, group_b: str) -> dict[str, Any]:
    """Cell-by-cell group delta — the same join `report --compare` prints,
    plus a pairing matrix the SPA renders as a grid."""
    def cells(group: str) -> dict[tuple[str, str, str], Any]:
        return {
            (c.task_id, c.orchestrator, c.worker): c
            for c in aggregate(store.list_runs(run_group=group))
        }

    cells_a, cells_b = cells(group_a), cells(group_b)
    keys = sorted(set(cells_a) | set(cells_b))
    rows: list[dict[str, Any]] = []
    for task_id, orch, worker in keys:
        a, b = cells_a.get((task_id, orch, worker)), cells_b.get((task_id, orch, worker))
        pa = a.pass_rate if a else None
        pb = b.pass_rate if b else None
        if pa is None or pb is None:
            verdict = "one-sided"
        elif pb > pa:
            verdict = "improved"
        elif pb < pa:
            verdict = "regressed"
        else:
            verdict = "stable"
        rows.append({
            "task_id": task_id, "orchestrator": orch, "worker": worker,
            "n_a": a.runs if a else 0, "pass_a": pa, "cost_a": a.cost_total if a else None,
            "n_b": b.runs if b else 0, "pass_b": pb, "cost_b": b.cost_total if b else None,
            "verdict": verdict,
            "failures_a": a.failures if a else {},
            "failures_b": b.failures if b else {},
        })
    verdicts: dict[str, int] = {}
    for r in rows:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    return {
        "group_a": group_a, "group_b": group_b, "cells": rows,
        "verdicts": verdicts,
        "cost_a": sum(r["cost_a"] or 0 for r in rows),
        "cost_b": sum(r["cost_b"] or 0 for r in rows),
    }
