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
import statistics
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.agentexec import ExecutorPreflightError, launch_gate
from orchestral.calibrate import calibration_status
from orchestral.config import (
    ConfigError,
    find_task,
    load_models,
    load_task,
    load_yaml,
    resolve_judge,
    resolve_model,
)
from orchestral.judge import DEFAULT_JUDGE
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

    def __init__(self, runs_dir: Path, tasks_dir: Path, models_dir: Path, store: RunStore,
                 allow_agent_exec: bool = False):
        self.runs_dir = Path(runs_dir)
        self.tasks_dir = Path(tasks_dir)
        self.models_dir = Path(models_dir)
        self.store = store
        self.allow_agent_exec = allow_agent_exec
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

        # Executor launches are a server-start decision (never a POST
        # field): validate the pairing and binary/env readiness before a
        # job exists — a 400 beats a failed run for a launch-time error.
        task_path = find_task(spec["task"], str(self.tasks_dir))
        if task_path is None:
            raise ValueError(f"task '{spec['task']}' not found in {self.tasks_dir}")
        try:
            task_spec = load_task(task_path)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        worker_cfg = resolve_model(spec["worker"], self.models_dir)
        try:
            launch_gate(
                worker_cfg, task_spec,
                allow_agent_exec=self.allow_agent_exec,
                probe=not bool(spec.get("dry_run")),
            )
        except ExecutorPreflightError as exc:
            raise ValueError(str(exc)) from exc

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
            orchestrator = resolve_model(spec["orchestrator"], self.models_dir, role="orchestrator")
            worker = resolve_model(spec["worker"], self.models_dir, role="worker")
            judge = resolve_judge(spec.get("judge") or None, self.models_dir)
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
                    allow_agent_exec=self.allow_agent_exec,
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


def model_choices(models_dir: Path, role: str | None) -> list[dict[str, Any]]:
    """Model options for the launch form — slug plus the metadata the UI
    needs for executor badges and capability/modality filtering.

    role=None lists all; role="judge" also lists all (any model can judge,
    same as the TUI). Both offer DEFAULT_JUDGE — a `~` decisions-engine
    slug load_models skips — flagged ``default`` so the form can prefill it.
    """
    try:
        models = [
            m for m in load_models(models_dir)
            if role in (None, "judge") or m.role == role
        ]
    except Exception:
        models = []
    out = [
        {
            "slug": m.slug,
            "executor": bool(m.metadata.get("executor")),
            "capabilities": sorted(m.metadata.get("capabilities") or []),
            "modalities": sorted(m.metadata.get("modalities") or []),
            "default": False,
        }
        for m in models
    ]
    if role in (None, "judge") and DEFAULT_JUDGE not in {m.slug for m in models}:
        out.append({"slug": DEFAULT_JUDGE, "executor": False,
                    "capabilities": [], "modalities": ["text"], "default": True})
    out.sort(key=lambda d: d["slug"])
    return out


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
            "scores": [], "judge_scores": [], "tasks": set(), "pairings": set(),
            "latest": None,
        })
        g["runs"] += 1
        if r.status == "finished":
            g["finished"] += 1
            g["passed"] += 1 if r.passes else 0
        g["cost_usd"] += r.total_cost_usd or 0.0
        if r.score is not None:
            g["scores"].append(r.score)
        if r.judge_score is not None:
            g["judge_scores"].append(r.judge_score)
        g["tasks"].add(r.task_id)
        g["pairings"].add((r.orchestrator, r.worker))
        if g["latest"] is None or (r.started_at or "") > (g["latest"] or ""):
            g["latest"] = r.started_at
    out = []
    for g in groups.values():
        scores = sorted(g.pop("scores"))
        judge_scores = sorted(g.pop("judge_scores"))
        n, jn = len(scores), len(judge_scores)
        out.append({
            **g,
            "tasks": len(g["tasks"]),
            "pairings": len(g["pairings"]),
            "pass_rate": (g["passed"] / g["finished"]) if g["finished"] else None,
            "score_median": scores[n // 2] if n else None,
            "judge_score_median": judge_scores[jn // 2] if jn else None,
        })
    out.sort(key=lambda x: x["latest"] or "", reverse=True)
    return out


def _task_types(store: RunStore, tasks_dir: Path | str | None) -> dict[str, str]:
    """task_id → task type, resolved from specs on disk. Missing specs map to
    '?' so the pairing breakdown never crashes on a pruned task."""
    if not tasks_dir:
        return {}
    from orchestral.config import load_task
    out: dict[str, str] = {}
    for path in Path(tasks_dir).rglob("*.yaml"):
        try:
            spec = load_task(path)
            out[spec.id] = spec.type
        except Exception:
            continue
    return out


def pairings_payload(
    store: RunStore,
    tasks_dir: Path | str | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """Heavy leaderboard data: per-pairing stats with honest uncertainty,
    per-task-type strength/weakness, the dominant failure class, the groups
    each pairing appears in, and an orchestrator×worker matrix."""
    metas = store.list_runs(run_group=group) if group else store.list_runs(limit=None)
    rows = pairing_leaderboard(metas)
    types = _task_types(store, tasks_dir)

    by_pair: dict[tuple[str, str], list[Any]] = {}
    for m in metas:
        by_pair.setdefault((m.orchestrator, m.worker), []).append(m)

    enriched: list[dict[str, Any]] = []
    for r in rows:
        cell = by_pair.get((r.orchestrator, r.worker), [])
        # per-task-type breakdown → "strong on X, weak on Y"
        per_type: dict[str, list[int]] = {}
        for m in cell:
            t = types.get(m.task_id, "?")
            st = per_type.setdefault(t, [0, 0])
            if m.status == "finished":
                st[1] += 1
                st[0] += 1 if m.passes else 0
        strengths = sorted(
            ((t, p, n) for t, (p, n) in per_type.items() if n),
            key=lambda x: (-(x[1] / x[2]), x[0]),
        )
        best = strengths[0] if strengths else None
        worst = strengths[-1] if strengths else None
        top_failure = max(r.failures.items(), key=lambda kv: kv[1])[0] if r.failures else None
        groups = sorted({m.run_group for m in cell if m.run_group})
        d = r.to_dict()
        d.update({
            "pass_ci": _wilson(r.passed, r.finished),
            "top_failure": top_failure,
            "groups": groups,
            "type_split": {t: {"passed": p, "finished": n} for t, (p, n) in per_type.items()},
            "best_type": best[0] if best else None,
            "worst_type": worst[0] if worst else None,
            "why": _pairing_why(r, best, worst, top_failure),
        })
        enriched.append(d)

    orchs = sorted({r.orchestrator for r in rows})
    workers = sorted({r.worker for r in rows})
    by_key = {(d["orchestrator"], d["worker"]): d for d in enriched}
    matrix = {
        "orchestrators": orchs,
        "workers": workers,
        "cells": [
            {
                "orchestrator": o, "worker": w,
                "pass_rate": (c["pass_rate"] if c else None),
                "runs": (c["runs"] if c else 0),
                "score_mean": (c["score_mean"] if c else None),
                "low_sample": (c["low_sample"] if c else False),
            }
            for o in orchs for w in workers
            for c in [by_key.get((o, w))]
        ],
    }
    return {"rows": enriched, "matrix": matrix}


def _pairing_why(r: Any, best: Any, worst: Any, top_failure: str | None) -> str:
    """One-line 'why this pairing placed here' — computed, not vibes."""
    if r.runs == 0:
        return "no runs"
    bits: list[str] = []
    if r.low_sample:
        bits.append("thin sample")
    if best and best[1] / max(best[2], 1) >= 0.8 and best[2] >= 2:
        bits.append(f"strong on {best[0]} ({best[1]}/{best[2]})")
    if worst and worst[1] == 0 and worst[2] >= 2:
        bits.append(f"fails {worst[0]} (0/{worst[2]})")
    if top_failure:
        bits.append(f"top failure: {top_failure}")
    if r.cost_per_pass is not None and r.cost_per_pass < 0.01:
        bits.append("cheap per pass")
    return " · ".join(bits) or "mid-pack on every axis"


def _wilson(passes: int, n: int) -> list[float] | None:
    """Wilson 95% interval on a binomial pass rate — the honest uncertainty
    a share card owes its audience when n is small."""
    if n <= 0:
        return None
    z, p = 1.96, passes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return [round(max(0.0, center - margin), 3), round(min(1.0, center + margin), 3)]


def _verdict_line(mech_pass: bool | None, judge_passed: bool | None,
                  status: str | None) -> str:
    """One-line verdict in plain words — the card's subtitle hook."""
    if status != "finished":
        return f"run {status or 'unknown'} — no verdict yet"
    if judge_passed is None:
        return ("mechanical pass — unjudged" if mech_pass
                else "mechanical fail — unjudged")
    if mech_pass and judge_passed:
        return "passes both axes — structure and semantics"
    if mech_pass:
        return "well-formed but semantically rejected"
    if judge_passed:
        return "mechanical reject, semantic rescue — inspect"
    return "rejected on both axes"


def _explainer(kind: str, card: dict[str, Any]) -> str:
    """What the card measures, for a mild-AI-knowledge audience — one
    sentence, no jargon. Mirrors the thread drafter's wording."""
    if kind == "group":
        return (
            "Each run: a planner AI breaks a real task into steps, worker AIs "
            "execute them in parallel, and the final result is graded two "
            "ways — automated checks that actually run/verify the output, "
            "plus a second AI that reviews whether it's genuinely good."
        )
    if kind == "pairing":
        return (
            f"One AI pairing: {str(card.get('orchestrator','?')).split('/')[-1]} plans the work, "
            f"{str(card.get('worker','?')).split('/')[-1]} executes it. Every run is graded by "
            "automated checks and an independent AI reviewer."
        )
    return (
        "One eval run: a planner AI broke the task into steps, a worker AI "
        "executed them, and the result was graded by automated checks plus "
        "an AI reviewer."
    )


def _eval_description(d: dict[str, Any], kind: str) -> str:
    """Pre-made 'how the eval set did' line — composed deterministically
    from the card's real numbers, so a card never needs a model call to
    carry a one-sentence summary."""
    def pct(x: float | None) -> str:
        return f"{round(x * 100)}%" if x is not None else "—"
    if kind == "group":
        bits = [
            f"{d['finished']}/{d['runs']} runs finished",
            f"{pct(d['pass_rate'])} mechanical pass",
        ]
        if d.get("judged"):
            jp = pct(d.get("judge_pass_rate"))
            bits.append(f"{jp} judge-approved over {d['judged']} judged")
        else:
            bits.append("unjudged")
        cost = d.get("cost_usd")
        if cost is not None:
            bits.append(f"${cost:.4f} total")
        pr, jr = d.get("pass_rate"), d.get("judge_pass_rate")
        note = ""
        if pr is not None and jr is not None and pr - jr > 0.15:
            note = " — the judge is stricter than the checks"
        elif jr is not None and pr is not None and jr - pr > 0.05:
            note = " — the judge rescues runs the checks reject"
        return f"{d['tasks']} tasks, {len(d.get('pairings') or [])} pairing(s): " + ", ".join(bits) + note + "."
    if kind == "pairing":
        o = str(d.get("orchestrator", "?")).split("/")[-1]
        w = str(d.get("worker", "?")).split("/")[-1]
        bits = [
            f"{d['finished']}/{d['runs']} runs finished",
            f"{pct(d['pass_rate'])} mechanical pass",
        ]
        if d.get("judged"):
            bits.append(f"judge mean {d.get('judge_score_mean')}")
        cost = d.get("cost_usd")
        if cost is not None:
            bits.append(f"${cost:.4f} total")
        tail = ""
        best, worst = d.get("best_type"), d.get("worst_type")
        if best and worst and best != worst:
            tail = f" — strongest on {best}, weakest on {worst}"
        return f"{o} plans, {w} executes, {d['tasks']} tasks: " + ", ".join(bits) + tail + "."
    return ""


def _calibration_map(
    reports_dir: Path | str, judge_models: set[str],
) -> dict[str, Any] | None:
    """Judge slug -> persisted calibration state, minus local paths."""
    if not judge_models:
        return None
    out = {}
    for slug in sorted(judge_models):
        s = calibration_status(reports_dir, slug)
        out[slug] = {
            "calibrated": s["calibrated"],
            "kappa": s["kappa"],
            "verdict_pairs": s["verdict_pairs"],
        }
    return out


def card_payload(
    store: RunStore,
    kind: str,
    target: str,
    group: str | None = None,
    tasks_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """Share-card data — the engineered summary an X post needs: flagship
    numbers, both verdict axes, the annotation flag, and caveat inputs
    (suite version, judge provenance, sample size).

    ``kind`` is ``group``, ``run``, or ``pairing`` (target = ``orch|worker``).
    """
    from orchestral import SUITE_VERSION

    reports_dir = reports_dir or Path("reports")
    flags = {(a["kind"], a["target"]): a for a in store.annotations()}
    ann = flags.get((kind, target)) or {}
    if kind == "pairing":
        if "|" not in target:
            return None
        orch, worker = target.split("|", 1)
        metas = store.list_runs(run_group=group) if group else store.list_runs(limit=None)
        cell = [m for m in metas if m.orchestrator == orch and m.worker == worker]
        if not cell:
            return None
        finished = [m for m in cell if m.status == "finished"]
        passed = sum(1 for m in finished if m.passes)
        scores = [m.score for m in finished if m.score is not None]
        costs = [m.total_cost_usd for m in finished]
        lat = [m.latency_ms for m in finished if m.latency_ms]
        types = _task_types(store, tasks_dir)
        per_type: dict[str, list[int]] = {}
        per_type_judge: dict[str, list[float]] = {}
        failures: dict[str, int] = {}
        judged_scores: list[float] = []
        judge_passed_n = 0
        judge_models: set[str] = set()
        for m in cell:
            if m.failure_reason:
                failures[m.failure_reason] = failures.get(m.failure_reason, 0) + 1
            if m.status == "finished":
                st = per_type.setdefault(types.get(m.task_id, "?"), [0, 0])
                st[1] += 1
                st[0] += 1 if m.passes else 0
                j = (read_json(Path(m.run_dir) / "report.json") or {}).get("judge") or {}
                if j.get("score") is not None:
                    judged_scores.append(float(j["score"]))
                    per_type_judge.setdefault(types.get(m.task_id, "?"), []).append(float(j["score"]))
                if j.get("passed"):
                    judge_passed_n += 1
                if j.get("model"):
                    judge_models.add(j["model"])
        judged_n = len(judged_scores)
        pr = passed / len(finished) if finished else None
        ci = _wilson(passed, len(finished))
        top_failure = max(failures.items(), key=lambda kv: kv[1])[0] if failures else None
        groups = sorted({m.run_group for m in cell if m.run_group})
        strong = sorted(((t, p, n) for t, (p, n) in per_type.items() if n),
                        key=lambda x: (-(x[1] / x[2]), x[0]))
        best, worst = (strong[0] if strong else None), (strong[-1] if strong else None)
        if judged_n and pr is not None:
            jp = judge_passed_n / judged_n
            if pr - jp > 0.15:
                line = f"{round(pr * 100)}% pass structure, {round(jp * 100)}% survive semantic review"
            elif jp - pr > 0.05:
                line = f"{round(pr * 100)}% clear the full gate — judge alone approves {round(jp * 100)}%"
            else:
                line = "mechanical and judge axes agree"
        elif judged_n:
            line = f"{judged_n} runs judged — semantic axis active"
        else:
            line = "mechanical grading only — nothing judged yet"
        payload = {
            "kind": "pairing", "target": target, "suite": SUITE_VERSION,
            "orchestrator": orch, "worker": worker,
            "runs": len(cell), "finished": len(finished), "passed": passed,
            "pass_rate": pr, "pass_ci": ci, "verdict_line": line,
            "score_mean": round(sum(scores) / len(scores), 3) if scores else None,
            "judged": judged_n,
            "judge_approved": judge_passed_n,
            "judge_pass_rate": judge_passed_n / judged_n if judged_n else None,
            "judge_score_mean": round(sum(judged_scores) / len(judged_scores), 3) if judged_scores else None,
            "cost_usd": round(sum(costs), 4),
            "cost_median": round(statistics.median(costs), 4) if costs else None,
            "latency_median_ms": round(statistics.median(lat)) if lat else None,
            "tasks": len({m.task_id for m in cell}),
            "type_split": {t: {"passed": p, "finished": n} for t, (p, n) in per_type.items()},
            "type_rows": [
                {"type": t, "passed": p, "finished": n,
                 "pass_rate": round(p / n, 3),
                 "judge_score": (round(sum(per_type_judge[t]) / len(per_type_judge[t]), 3)
                                 if per_type_judge.get(t) else None)}
                for t, (p, n) in sorted(
                    per_type.items(), key=lambda kv: (-(kv[1][0] / kv[1][1]), kv[0]))
            ],
            "best_type": best[0] if best else None,
            "worst_type": worst[0] if worst else None,
            "top_failure": top_failure,
            "groups": groups,
            "judge_models": sorted(judge_models),
            "judge_calibration": _calibration_map(reports_dir, judge_models),
            "explainer": _explainer("pairing", {"orchestrator": orch, "worker": worker}),
            "flag": ann.get("flag", ""), "note": ann.get("note", ""),
        }
        payload["description"] = _eval_description(payload, "pairing")
        return payload
    if kind == "group":
        g = next((x for x in groups_payload(store) if x["group"] == target), None)
        if g is None:
            return None
        metas = store.list_runs(run_group=target)
        cells = aggregate(metas)
        pairings = sorted({(c.orchestrator, c.worker) for c in cells})
        # judge aggregates — read each finished run's report for the
        # semantic axis; unjudged runs contribute nothing, honestly
        judge_scores: list[float] = []
        judge_nouls: list[float] = []
        judge_models: set[str] = set()
        judge_passed_n = 0
        # comparable rows — per-task split is always meaningful; per-pairing
        # rows matter when the eval set ran more than one pairing
        per_task: dict[str, list[int]] = {}
        task_judge: dict[str, list[float]] = {}
        per_pair: dict[tuple[str, str], list[int]] = {}
        pair_judge: dict[tuple[str, str], list[float]] = {}
        pair_jpassed: dict[tuple[str, str], int] = {}
        pair_cost: dict[tuple[str, str], float] = {}
        for m in metas:
            if m.status != "finished":
                continue
            st = per_task.setdefault(m.task_id, [0, 0])
            st[1] += 1
            st[0] += 1 if m.passes else 0
            key = (m.orchestrator, m.worker)
            ps = per_pair.setdefault(key, [0, 0])
            ps[1] += 1
            ps[0] += 1 if m.passes else 0
            pair_cost[key] = pair_cost.get(key, 0.0) + (m.total_cost_usd or 0.0)
            j = (read_json(Path(m.run_dir) / "report.json") or {}).get("judge") or {}
            if not j or (j.get("score") is None and j.get("noul") is None):
                continue
            if j.get("score") is not None:
                judge_scores.append(float(j["score"]))
                pair_judge.setdefault(key, []).append(float(j["score"]))
                task_judge.setdefault(m.task_id, []).append(float(j["score"]))
            if j.get("noul") is not None:
                judge_nouls.append(float(j["noul"]))
            if j.get("model"):
                judge_models.add(j["model"])
            if j.get("passed"):
                judge_passed_n += 1
                pair_jpassed[key] = pair_jpassed.get(key, 0) + 1
        judged_n = len(judge_scores) or len(judge_nouls)
        jp_rate = judge_passed_n / judged_n if judged_n else None
        ci = _wilson(g["passed"], g["finished"])
        failed_n = sum(1 for m in metas if m.status == "failed")
        running_n = sum(1 for m in metas if m.status == "running")
        if not judge_models and judged_n:
            judge_models = set(store.judge_slugs({m.task_id for m in metas}))
        if judged_n and jp_rate is not None and g["pass_rate"] is not None:
            mech_pct, jp_pct = round(g["pass_rate"] * 100), round(jp_rate * 100)
            if g["pass_rate"] - jp_rate > 0.15:
                line = f"{mech_pct}% pass structure, {jp_pct}% survive semantic review"
            elif jp_rate - g["pass_rate"] > 0.05:
                line = f"{mech_pct}% clear the full gate — judge alone approves {jp_pct}%"
            else:
                line = "mechanical and judge axes agree"
        elif judged_n:
            line = f"{judged_n} runs judged — semantic axis active"
        else:
            line = "mechanical grading only — nothing judged yet"
        payload = {
            "kind": "group", "target": target, "suite": SUITE_VERSION,
            "runs": g["runs"], "finished": g["finished"], "passed": g["passed"],
            "failed": failed_n, "running": running_n,
            "pass_rate": g["pass_rate"], "score_median": g["score_median"],
            "judge_score_median": g["judge_score_median"],
            "pass_ci": ci, "verdict_line": line,
            "judged": judged_n, "judge_approved": judge_passed_n,
            "judge_pass_rate": jp_rate,
            "judge_score_mean": (round(sum(judge_scores) / len(judge_scores), 3)
                                 if judge_scores else None),
            "judge_noul_mean": (round(sum(judge_nouls) / len(judge_nouls), 3)
                                if judge_nouls else None),
            "judge_models": sorted(judge_models),
            "judge_calibration": _calibration_map(reports_dir, judge_models),
            "cost_usd": g["cost_usd"], "tasks": g["tasks"],
            "pairings": [{"orchestrator": o, "worker": w} for o, w in pairings],
            "pairing_rows": [
                {"orchestrator": o, "worker": w,
                 "finished": n, "passed": p,
                 "pass_rate": round(p / n, 3),
                 "judged": len(pair_judge.get(key, [])),
                 "judge_approved": pair_jpassed.get(key, 0),
                 "judge_score_mean": (round(sum(pair_judge[key]) / len(pair_judge[key]), 3)
                                      if pair_judge.get(key) else None),
                 "cost_usd": round(pair_cost[key], 4)}
                for (o, w), (p, n) in sorted(
                    per_pair.items(), key=lambda kv: (-(kv[1][0] / kv[1][1]), kv[0][0]))
                for key in [(o, w)]
            ],
            "task_rows": [
                {"task_id": t, "passed": p, "finished": n,
                 "pass_rate": round(p / n, 3),
                 "judge_score": (round(sum(task_judge[t]) / len(task_judge[t]), 3)
                                 if task_judge.get(t) else None)}
                for t, (p, n) in sorted(
                    per_task.items(), key=lambda kv: (-(kv[1][0] / kv[1][1]), kv[0]))
            ],
            "latest": g["latest"],
            "explainer": _explainer("group", {}),
            "flag": ann.get("flag", ""), "note": ann.get("note", ""),
        }
        payload["description"] = _eval_description(payload, "group")
        return payload
    if kind == "run":
        meta = store.get_run(target)
        if meta is None:
            return None
        report = read_json(Path(meta.run_dir) / "report.json") or {}
        judge = report.get("judge") or {}
        plan = read_json(Path(meta.run_dir) / "plan.json") or {}
        plan_summary = str(plan.get("plan") or "").strip() if isinstance(plan, dict) else ""
        judge_reason = str(judge.get("reasoning") or "").strip()
        if judge_reason:
            description, description_by = judge_reason, "judge"
        elif plan_summary:
            description, description_by = plan_summary, "orchestrator"
        else:
            description, description_by = "", ""
        return {
            "kind": "run", "target": target, "suite": SUITE_VERSION,
            "task_id": meta.task_id, "orchestrator": meta.orchestrator,
            "worker": meta.worker, "status": meta.status,
            "passes": meta.passes, "score": meta.score,
            "cost_usd": meta.total_cost_usd, "latency_ms": meta.latency_ms,
            "failure_reason": meta.failure_reason,
            "run_group": meta.run_group, "replicate": meta.replicate,
            "started_at": meta.started_at,
            "judge_engine": judge.get("engine"), "judge_noul": judge.get("noul"),
            "judge_passed": judge.get("passed"),
            "judge_model": judge.get("model"),
            "judge_calibration": _calibration_map(
                reports_dir, {judge["model"]} if judge.get("model") else set()),
            "judge_reasoning": judge_reason[:280],
            "description": description[:600],
            "description_by": description_by,
            "description_model": (judge.get("model") if description_by == "judge"
                                  else meta.orchestrator) if description else "",
            "verdict_line": _verdict_line(
                bool(meta.passes),
                judge.get("passed") if judge else None, meta.status),
            "explainer": _explainer("run", {}),
            "flag": ann.get("flag", ""), "note": ann.get("note", ""),
        }
    return None


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
