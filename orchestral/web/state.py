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
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
from orchestral.stats import aggregate, mean, pairing_leaderboard
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

CARD_LENSES: tuple[dict[str, str], ...] = (
    {
        "id": "overall",
        "label": "Best overall",
        "description": "Best observed mechanical outcome; cost breaks ties.",
    },
    {
        "id": "high_cost",
        "label": "Best code · high spend",
        "description": "Best result within the upper half of measured cohort spend.",
    },
    {
        "id": "low_cost",
        "label": "Best code · low spend",
        "description": "Best result within the lower half of measured cost per pass.",
    },
    {
        "id": "sweet_spot",
        "label": "Quality / cost sweet spot",
        "description": "Nearest the leading pass rate at the lowest measured cost per pass.",
    },
    {
        "id": "divergence",
        "label": "Interesting divergence",
        "description": "Largest gap between mechanical and judge-approved rates.",
    },
)
CARD_LENS_IDS = frozenset(lens["id"] for lens in CARD_LENSES)

_CODE_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".css", ".diff", ".go", ".html", ".java", ".js",
    ".json", ".jsx", ".kt", ".md", ".patch", ".php", ".py", ".rb", ".rs",
    ".scss", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".vue", ".yaml", ".yml",
})
_IMAGE_EXTENSIONS = frozenset({".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm"})


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
                    sandbox="docker",
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


def read_json_why(path: Path) -> tuple[Any | None, str]:
    """``read_json`` plus the reason it failed, for claims that must be honest.

    ``read_json`` collapses "missing" and "unparseable" into ``None``, which is
    right for display and wrong for a verdict: a report truncated by a kill
    looks exactly like a report that carries no judge block. Returns
    ``(value, "")`` on success and ``(None, reason)`` on failure.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")), ""
    except FileNotFoundError:
        return None, f"{path.name} is missing"
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is unreadable ({exc.msg} at line {exc.lineno})"
    except OSError as exc:
        return None, f"{path.name} could not be read ({exc.strerror or exc})"


def _mapping(value: Any) -> dict[str, Any]:
    """Return a mapping-shaped JSON value without trusting its shape."""
    return value if isinstance(value, dict) else {}


def _judge_block(run_dir: Path | str) -> tuple[dict[str, Any], str]:
    """A run's judge block, plus the reason the report could not be read.

    ``({}, "")`` means the report read clean and simply has no judge block — a
    real, statable absence. A non-empty reason means the judge axis is
    *unknown*: the judge may well have run and had its verdict lost, which is
    a different claim and must never be counted as zero.
    """
    report, why = read_json_why(Path(run_dir) / "report.json")
    if why:
        return {}, why
    if not isinstance(report, dict):
        return {}, "report.json is not a JSON object"
    return _mapping(report.get("judge")), ""


def _unreadable_caveat(unreadable_n: int) -> str:
    """Trailing qualifier so a partial judge axis is not read as a full one."""
    if not unreadable_n:
        return ""
    return f" · {unreadable_n} judge report(s) unreadable — semantic axis incomplete"


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
    rows = pairing_leaderboard(
        store.list_runs(limit=None),
        unmetered_workers=store.unmetered_workers(),
    )
    return [r.to_dict() for r in sort_leaderboard(rows, sort)]


def _runs_for_group(store: RunStore, group: str | None) -> list[Any]:
    """Resolve a UI group selector to runs; ``(ungrouped)`` means all unlabelled runs."""
    if not group:
        return store.list_runs(limit=None)
    if group == "(ungrouped)":
        return [r for r in store.list_runs(limit=None) if not r.run_group]
    return store.list_runs(run_group=group)


def overview_payload(
    store: RunStore,
    registry: JobRegistry,
    tasks_dir: Path | str | None = None,
    groups_file: Path | str | None = None,
) -> dict[str, Any]:
    """Mission-control data: live jobs, leaderboard top rows, recent runs,
    group summaries, and the failure taxonomy — the SPA's landing view."""
    runs = store.list_runs(limit=None)
    lb = pairing_leaderboard(runs, unmetered_workers=store.unmetered_workers())
    taxonomy: dict[str, int] = {}
    for r in runs:
        if r.failure_reason:
            taxonomy[r.failure_reason] = taxonomy.get(r.failure_reason, 0) + 1
    tmeta = _task_meta(store, tasks_dir)
    recent = []
    for r in runs[:10]:
        d = r.to_dict()
        d["task_title"] = (tmeta.get(r.task_id) or {}).get("title") or ""
        d["judge_state"], d["judge_reason"] = judge_state(r)
        recent.append(d)
    return {
        "jobs": [
            {
                "label": j.label, "status": str(j.status), "detail": j.detail,
                "run_ids": list(j.run_ids), "cancellable": j.active,
            }
            for j in registry.jobs
        ],
        "leaderboard": [r.to_dict() for r in lb[:10]],
        "recent": recent,
        "groups": groups_payload(store, groups_file)[:8],
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
    out.sort(key=lambda d: str(d["slug"]))
    return out


# ---------------------------------------------------------------------------
# SPA API payloads — the rebuilt observatory reads everything through these.


def judge_state(meta) -> tuple[str, str]:
    """Closed taxonomy for the judge axis — (state, one-line reason).

    "Unjudged" was four different situations rendered identically; the UI
    needs to say which: ``judged`` | ``inconclusive`` (a verdict was attempted
    but couldn't be parsed) | ``not_judged`` (never attempted, or the run
    never finished) | ``not_judgeable`` (no artifact survives to score) |
    ``unreadable`` (report.json could not be read, so whether the judge ran is
    genuinely unknown). ``unreadable`` earns its own state because collapsing
    it into ``not_judged`` asserts the judge never ran — a claim about work
    this function cannot see.
    """
    if meta.judge_score is not None:
        return "judged", ""
    j, read_why = _judge_block(meta.run_dir)
    if j:
        reason = str(j.get("reasoning") or "").strip()
        if j.get("inconclusive"):
            return "inconclusive", reason or "the judge returned no usable verdict"
        if any(j.get(key) is not None for key in ("score", "noul", "passed")):
            return "judged", ""
        return "not_judged", reason or "judge ran without a verdict"
    if meta.status != "finished":
        return "not_judged", f"run never finished ({meta.status})"
    if not any(Path(meta.run_dir).glob("artifact.*")):
        return "not_judgeable", "no artifact survives to judge"
    if meta.dry_run:
        return "not_judged", "dry run — nothing real to judge"
    if read_why:
        return "unreadable", f"judge verdict unknown — {read_why}"
    return "not_judged", "judge wasn't run for this run"


def runs_payload(
    store: RunStore,
    group: str | None = None,
    task: str | None = None,
    status: str | None = None,
    q: str = "",
    tasks_dir: Path | str | None = None,
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
    tmeta = _task_meta(store, tasks_dir)
    out = []
    for r in rows:
        d = r.to_dict()
        tm = tmeta.get(r.task_id) or {}
        d["task_title"] = tm.get("title") or ""
        d["judge_state"], d["judge_reason"] = judge_state(r)
        out.append(d)
    return out


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


def run_detail_payload(
    store: RunStore,
    run_id: str,
    tasks_dir: Path | str | None = None,
    groups_file: Path | str | None = None,
) -> dict[str, Any] | None:
    """Everything the run detail view needs in one fetch."""
    meta = store.get_run(run_id)
    if meta is None:
        return None
    run_dir = Path(meta.run_dir)
    plan_path = run_dir / "plan.md"
    tm = _task_meta(store, tasks_dir).get(meta.task_id) or {}
    gm = _groups_meta(groups_file).get(meta.run_group or "") or {}
    jstate, jreason = judge_state(meta)
    return {
        "meta": meta.to_dict(),
        "judge_state": jstate,
        "judge_reason": jreason,
        "task_title": tm.get("title") or "",
        "task_blurb": tm.get("blurb") or "",
        "group_label": gm.get("label") or "",
        "group_description": gm.get("description") or "",
        "calls": store.calls_for_run(run_id),
        "report": read_json(run_dir / "report.json"),
        "review": read_json(run_dir / "review.json"),
        "manifest": read_json(run_dir / "manifest.json"),
        "plan": plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.exists() else None,
        "timeline": timeline_payload(run_dir),
        "artifact": artifact_info(run_dir),
    }


def groups_payload(
    store: RunStore, groups_file: Path | str | None = None
) -> list[dict[str, Any]]:
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
    meta = _groups_meta(groups_file)
    for g in groups.values():
        scores = sorted(g.pop("scores"))
        judge_scores = sorted(g.pop("judge_scores"))
        n, jn = len(scores), len(judge_scores)
        gm = meta.get(g["group"]) or {}
        out.append({
            **g,
            "label": gm.get("label") or "",
            "description": gm.get("description") or "",
            "tasks": len(g["tasks"]),
            "pairings": len(g["pairings"]),
            "pass_rate": (g["passed"] / g["finished"]) if g["finished"] else None,
            "score_median": scores[n // 2] if n else None,
            "judge_score_median": judge_scores[jn // 2] if jn else None,
        })
    out.sort(key=lambda x: x["latest"] or "", reverse=True)
    return out


def task_matrix_payload(
    store: RunStore, tasks_dir: Path | str | None = None
) -> dict[str, Any]:
    """Tasks × pairings heatmap: pass rate + judge mean per cell over
    finished runs. Cells with no runs are simply absent — sparse is honest."""
    runs = store.list_runs(limit=None)
    tmeta = _task_meta(store, tasks_dir)
    cells: dict[tuple[str, str], list] = {}
    for r in runs:
        cells.setdefault((r.task_id, f"{r.orchestrator} → {r.worker}"), []).append(r)
    pairings = sorted({p for _, p in cells})
    rows = []
    for task_id in sorted({t for t, _ in cells}):
        tm = tmeta.get(task_id) or {}
        row: dict[str, Any] = {
            "task_id": task_id,
            "task_title": tm.get("title") or "",
            "task_type": tm.get("type") or "",
            "cells": {},
        }
        for p in pairings:
            cell = cells.get((task_id, p))
            if not cell:
                continue
            fin = [r for r in cell if r.status == "finished"]
            judged = [r.judge_score for r in fin if r.judge_score is not None]
            row["cells"][p] = {
                "n": len(cell),
                "pass_rate": (
                    sum(1 for r in fin if r.passes) / len(fin) if fin else None
                ),
                "judge_mean": mean(judged) if judged else None,
            }
        rows.append(row)
    return {"pairings": pairings, "tasks": rows}


def _task_meta(store: RunStore, tasks_dir: Path | str | None) -> dict[str, dict[str, str]]:
    """task_id → {type, title, blurb}, resolved from specs on disk. Missing
    specs are skipped so the pairing breakdown never crashes on a pruned task."""
    if not tasks_dir:
        return {}
    from orchestral.config import load_task
    out: dict[str, dict[str, str]] = {}
    for path in Path(tasks_dir).rglob("*.yaml"):
        try:
            spec = load_task(path)
            out[spec.id] = {"type": spec.type, "title": spec.title, "blurb": spec.blurb}
        except Exception:
            continue
    return out


def _task_types(store: RunStore, tasks_dir: Path | str | None) -> dict[str, str]:
    """task_id → task type only (see `_task_meta`)."""
    return {tid: m["type"] for tid, m in _task_meta(store, tasks_dir).items()}


def _groups_meta(path: Path | str | None = None) -> dict[str, dict[str, str]]:
    """Run-group label/description from groups.yaml — empty map when absent."""
    from orchestral.config import load_groups
    try:
        return load_groups(path or "groups.yaml")
    except Exception:
        return {}


def _pairing_dict(row: Any) -> dict[str, Any]:
    return row if isinstance(row, dict) else row.to_dict()


def _pairing_target(row: dict[str, Any]) -> str:
    return f"{row.get('orchestrator', '')}|{row.get('worker', '')}"


def _pairing_quality_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Existing leaderboard semantics: pass, then cost, then judge as a tie-break."""
    pass_rate = row.get("pass_rate")
    judge_score = row.get("judge_score_median")
    cost = row.get("cost_per_pass")
    return (
        -(float(pass_rate) if pass_rate is not None else -1.0),
        cost if cost is not None else float("inf"),
        -(float(judge_score) if judge_score is not None else -1.0),
        -int(row.get("finished") or 0),
        str(row.get("orchestrator") or ""),
        str(row.get("worker") or ""),
    )


def _lens_judge_rate(row: dict[str, Any]) -> float:
    value = row.get("judge_pass_rate")
    if value is not None:
        return float(value)
    score = row.get("judge_score_median")
    return float(score) if score is not None else 0.0


def _lens_payloads(rows: list[Any]) -> list[dict[str, Any]]:
    """Return deterministic named lens selections over existing pairing rows.

    Lenses are filters/rankings, never a new composite score. Thin-sample rows
    never enter a ranking, and unmetered rows never enter a cost lens.
    """
    dict_rows = [_pairing_dict(row) for row in rows]
    eligible = [row for row in dict_rows if not row.get("low_sample") and row.get("finished")]
    measured = [row for row in eligible if row.get("cost_total", 0) > 0]
    cost_per_pass = [float(row["cost_per_pass"]) for row in eligible if row.get("cost_per_pass") is not None]
    out: list[dict[str, Any]] = []

    def ranked(candidates: list[dict[str, Any]], key=_pairing_quality_key) -> list[dict[str, Any]]:
        return sorted(candidates, key=key)

    def result(lens: dict[str, str], candidates: list[dict[str, Any]], reason: str,
               empty_reason: str, key=_pairing_quality_key) -> None:
        ordered = ranked(candidates, key)
        selected = ordered[0] if ordered else None
        out.append({
            **lens,
            "selected_target": _pairing_target(selected) if selected else "",
            "ranking": [_pairing_target(row) for row in ordered],
            "eligible": len(ordered),
            "reason": reason.format(**selected) if selected and reason else "",
            "empty_reason": "" if selected else empty_reason,
        })

    result(
        CARD_LENSES[0], eligible,
        "{orchestrator} → {worker} leads on observed pass rate; cost breaks ties.",
        "no pairing has three finished runs yet",
    )

    high_spend = [row for row in measured if row.get("cost_total", 0) >= statistics.median(
        [float(r.get("cost_total") or 0) for r in measured]
    )] if measured else []
    result(
        CARD_LENSES[1], high_spend,
        "{orchestrator} → {worker} is strongest in the upper measured-spend half.",
        "no metered pairing has enough finished evidence",
    )

    low_spend = [row for row in eligible if row.get("cost_per_pass") is not None and
                 float(row["cost_per_pass"]) <= statistics.median(cost_per_pass)] if cost_per_pass else []
    result(
        CARD_LENSES[2], low_spend,
        "{orchestrator} → {worker} is strongest in the lower measured-cost half.",
        "no measured cost per pass is available",
    )

    best_pass = max((float(row.get("pass_rate") or 0) for row in eligible), default=0.0)
    sweet = [row for row in eligible if row.get("cost_per_pass") is not None and
             float(row.get("pass_rate") or 0) >= best_pass - 0.10]
    result(
        CARD_LENSES[3], sweet,
        "{orchestrator} → {worker} is within 10 points of the pass leader at the lowest measured cost.",
        "no metered pairing is within 10 points of the leading pass rate",
        key=lambda row: (
            float(row.get("cost_per_pass") or float("inf")),
            -(float(row.get("pass_rate") or 0)),
            str(row.get("orchestrator") or ""),
            str(row.get("worker") or ""),
        ),
    )

    divergent = [row for row in eligible if row.get("judged") and row.get("pass_rate") is not None and
                 (row.get("judge_score_median") is not None or row.get("judge_pass_rate") is not None)]
    result(
        CARD_LENSES[4], divergent,
        "{orchestrator} → {worker} has the largest mechanical-versus-judge gap.",
        "no pairing has a judged semantic axis to compare",
        key=lambda row: (
            -abs(float(row.get("pass_rate") or 0) - _lens_judge_rate(row)),
            -int(row.get("finished") or 0),
            str(row.get("orchestrator") or ""),
            str(row.get("worker") or ""),
        ),
    )
    return out


def pairings_payload(
    store: RunStore,
    tasks_dir: Path | str | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """Heavy leaderboard data: per-pairing stats with honest uncertainty,
    per-task-type strength/weakness, the dominant failure class, the groups
    each pairing appears in, and an orchestrator×worker matrix."""
    metas = _runs_for_group(store, group)
    rows = pairing_leaderboard(metas, unmetered_workers=store.unmetered_workers())
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
            "target": f"{r.orchestrator}|{r.worker}",
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
    return {"rows": enriched, "matrix": matrix, "lenses": _lens_payloads(enriched)}


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


def _cohort_payload(metas: list[Any]) -> dict[str, Any]:
    """Composition facts for a card's originating scope, never a synthetic score."""
    finished = [m for m in metas if m.status == "finished"]
    orchestrators = sorted({m.orchestrator for m in metas})
    workers = sorted({m.worker for m in metas})
    tasks = sorted({m.task_id for m in metas})
    pairings = sorted({(m.orchestrator, m.worker) for m in metas})
    return {
        "runs": len(metas),
        "finished": len(finished),
        "orchestrators": len(orchestrators),
        "workers": len(workers),
        "tasks": len(tasks),
        "pairings": len(pairings),
        "repeats": sum(1 for m in metas if m.replicate is not None),
        "dry_runs": sum(1 for m in metas if m.dry_run),
        "latest": max((m.started_at or "" for m in metas), default=""),
        "models": {"orchestrators": orchestrators, "workers": workers},
        "pairing_list": [{"orchestrator": o, "worker": w} for o, w in pairings],
    }


def _media_kind(ext: str) -> str:
    ext = "." + ext.lower().lstrip(".")
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if ext in _CODE_EXTENSIONS:
        return "code" if ext not in {".html", ".json", ".md", ".txt", ".yaml", ".yml"} else "text"
    return "binary"


def _preferred_artifact_member(names: list[str]) -> str | None:
    """Prefer a small, inspectable source member over archive boilerplate."""
    def safe(name: str) -> bool:
        normalized = name.replace("\\", "/")
        return (
            bool(name)
            and "\x00" not in name
            and not normalized.startswith("/")
            and ".." not in Path(normalized).parts
        )

    candidates = [name for name in names if safe(name) and not name.endswith("/") and "__MACOSX" not in name]
    for extension in (".diff", ".patch", ".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".sql", ".json"):
        for name in candidates:
            if name.lower().endswith(extension):
                return name
    return sorted(candidates)[0] if candidates else None


def _artifact_reference(run_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Reference-only artifact metadata; bytes are loaded by the evidence endpoint."""
    artifacts = sorted(run_dir.glob("artifact.*"))
    if not artifacts:
        return None
    artifact = artifacts[0]
    if artifact.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(artifact) as archive:
                infos = archive.infolist()
                member = _preferred_artifact_member([i.filename for i in infos])
                member_info = archive.getinfo(member) if member else None
        except (OSError, zipfile.BadZipFile):
            member_info = None
            member = None
        if not member or member_info is None or member_info.is_dir():
            return None
        ext = Path(member).suffix.lstrip(".").lower()
        return {
            "name": member,
            "ext": ext,
            "kind": _media_kind(ext),
            "media_type": "archive",
            "bytes": max(0, int(member_info.file_size)),
            "url": f"/api/run/{quote(run_id, safe='')}/artifact/{quote(member, safe='')}",
        }
    ext = artifact.suffix.lstrip(".").lower()
    try:
        size = artifact.stat().st_size
    except OSError:
        size = 0
    return {
        "name": artifact.name,
        "ext": ext,
        "kind": _media_kind(ext),
        "media_type": _media_kind(ext),
        "bytes": size,
        "url": f"/api/run/{quote(run_id, safe='')}/artifact",
    }


def _bounded_text(text: str, *, max_bytes: int, max_lines: int, tail: bool = True) -> str:
    """Bound untrusted evidence before it enters a card or API response."""
    text = str(text or "").replace("\x00", "")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:] if tail else lines[:max_lines]
    bounded = "\n".join(lines)
    encoded = bounded.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        encoded = encoded[-max_bytes:] if tail else encoded[:max_bytes]
        bounded = encoded.decode("utf-8", errors="ignore")
    return bounded


def _event_transcript(run_dir: Path) -> str:
    """Render a short terminal-like view from lifecycle events, never raw prompts."""
    events, _ = tail_events(run_dir / "events.jsonl", 0)
    wanted = {
        "evaluation.completed", "worker_error", "worker.failed", "worker_retry",
        "artifact.saved", "run.completed", "run.failed", "run.cancelled",
    }
    rows: list[str] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in wanted:
            continue
        output = event.get("output")
        output = output if isinstance(output, dict) else {}
        safe_event = dict(event)
        safe_event["output"] = output
        timestamp, event_type, worker, detail = event_row(safe_event)
        if event_type == "artifact.saved":
            artifact_name = str(output.get("name") or output.get("path") or "")
            artifact_name = Path(artifact_name).name if artifact_name else ""
            detail = f"artifact {artifact_name}" if artifact_name else "artifact saved"
        extra = ""
        if event_type == "evaluation.completed":
            extra = f" passes={output.get('passes')} score={output.get('score')}"
        elif event_type == "worker_error":
            extra = f" {str(event.get('error') or '')[:160]}"
        row = f"{timestamp} {event_type:<22} {worker:<10} {detail}{extra}".rstrip()
        # Event records occasionally carry an absolute harness path. Keep
        # the evidence endpoint useful without returning local filesystem
        # layout to the browser or a writer prompt.
        row = row.replace(str(run_dir), "[run]").replace(str(run_dir.resolve()), "[run]")
        rows.append(row)
    return "\n".join(rows)


def _proof_reference(meta: Any) -> dict[str, Any]:
    """Bounded-proof status and links for one run, with no local paths."""
    run_dir = Path(meta.run_dir)
    report = _mapping(read_json(run_dir / "report.json"))
    execution = _mapping(report.get("execution"))
    output = str(execution.get("output_tail") or "")
    transcript_text = _bounded_text(output, max_bytes=6000, max_lines=80) if output else ""
    transcript_label = "test transcript" if transcript_text else "event transcript"
    if not transcript_text:
        transcript_text = _bounded_text(_event_transcript(run_dir), max_bytes=6000, max_lines=80)
    artifact = _artifact_reference(run_dir, meta.run_id)
    has_transcript = bool(transcript_text)
    has_artifact = bool(artifact and int(artifact.get("bytes") or 0) > 0)
    status = "available" if has_transcript and has_artifact else "partial" if has_transcript or has_artifact else "unavailable"
    return {
        "status": status,
        "run_id": meta.run_id,
        "task_id": meta.task_id,
        "inspect_url": f"/#/run/{quote(meta.run_id, safe='')}",
        "evidence_url": f"/api/run/{quote(meta.run_id, safe='')}/evidence",
        "transcript": {
            "available": has_transcript,
            "label": transcript_label,
            "url": f"/api/run/{quote(meta.run_id, safe='')}/evidence",
        } if has_transcript else None,
        "artifact": artifact,
    }


def _representative_run(metas: list[Any], signals: list[dict[str, Any]] | None = None) -> Any | None:
    """Pick stable evidence: complete proof first, then the most informative run."""
    if not metas:
        return None
    preferred_tasks = {
        str(signal.get("evidence", {}).get("task_id"))
        for signal in (signals or [])
        if signal.get("evidence", {}).get("task_id")
    }

    def key(meta: Any) -> tuple[Any, ...]:
        proof = _proof_reference(meta)
        divergence = (
            meta.passes is not None and meta.judge_passed is not None
            and bool(meta.passes) != bool(meta.judge_passed)
        )
        return (
            meta.task_id in preferred_tasks,
            proof["status"] == "available",
            proof["status"] != "unavailable",
            meta.status == "finished",
            bool(meta.passes),
            divergence,
            meta.judge_score is not None,
            meta.started_at or "",
            meta.run_id,
        )

    return max(metas, key=key)


def _task_evidence_refs(
    metas: list[Any], tasks_meta: dict[str, dict[str, str]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    cells: dict[str, list[Any]] = {}
    for meta in metas:
        if meta.status == "finished":
            cells.setdefault(meta.task_id, []).append(meta)
    if not cells:
        return None, None
    rows: list[dict[str, Any]] = []
    for task_id, cell in cells.items():
        judge_scores = [m.judge_score for m in cell if m.judge_score is not None]
        representative = _representative_run(cell)
        rows.append({
            "task_id": task_id,
            "title": (tasks_meta.get(task_id) or {}).get("title") or task_id,
            "passed": sum(1 for m in cell if m.passes),
            "finished": len(cell),
            "pass_rate": sum(1 for m in cell if m.passes) / len(cell),
            "judge_score": mean(judge_scores) if judge_scores else None,
            "run_id": representative.run_id if representative else None,
            "inspect_url": f"/#/run/{quote(representative.run_id, safe='')}" if representative else "",
        })
    best = sorted(rows, key=lambda row: (-row["pass_rate"], -row["finished"], row["task_id"]))[0]
    worst = sorted(rows, key=lambda row: (row["pass_rate"], -row["finished"], row["task_id"]))[0]
    return best, worst


def _confidence_level(n: int, ci: list[float] | None) -> str:
    width = (ci[1] - ci[0]) if ci else 1.0
    if n < 3 or width >= 0.5:
        return "low"
    if n < 10 or width >= 0.25:
        return "medium"
    return "strong"


def _confidence_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("kind") == "run":
        mechanical_n = 1 if payload.get("status") == "finished" else 0
        mechanical = {
            "n": mechanical_n,
            "point": 1.0 if payload.get("passes") else 0.0 if payload.get("passes") is not None else None,
            "ci": _wilson(1, 1) if mechanical_n else None,
            "level": "low" if mechanical_n else "unavailable",
        }
        judge = {
            "n": 1 if payload.get("judge_state") == "judged" else 0,
            "point": payload.get("judge_score") if payload.get("judge_state") == "judged" else payload.get("judge_noul"),
            "ci": None,
            "level": "low" if payload.get("judge_state") == "judged" else "unavailable",
        }
    else:
        finished = int(payload.get("finished") or 0)
        ci = payload.get("pass_ci")
        mechanical = {
            "n": finished,
            "point": payload.get("pass_rate"),
            "ci": ci,
            "level": _confidence_level(finished, ci),
        }
        judge = {
            "n": int(payload.get("judged") or 0),
            "point": payload.get("judge_score_mean") if payload.get("judged") else None,
            "ci": None,
            "level": _confidence_level(int(payload.get("judged") or 0), None) if payload.get("judged") else "unavailable",
        }
    return {"mechanical": mechanical, "judge": judge}


def _story_signals(
    payload: dict[str, Any],
    metas: list[Any],
    selected_row: dict[str, Any] | None,
    peer_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compute the shared, deterministic finding set used by card and thread."""
    signals: list[dict[str, Any]] = []
    if payload.get("kind") == "run":
        if payload.get("passes") is not None and payload.get("judge_passed") is not None and bool(payload["passes"]) != bool(payload["judge_passed"]):
            signals.append({
                "id": "axis_divergence",
                "label": "axis divergence",
                "tone": "info",
                "claim": "The mechanical gate and judge disagree on this run.",
                "evidence": {"mechanical": bool(payload.get("passes")), "judge": bool(payload.get("judge_passed"))},
            })
        if payload.get("passes") and (payload.get("judge_score") or 0) >= 0.8:
            signals.append({
                "id": "high_quality",
                "label": "high quality",
                "tone": "pass",
                "claim": "The run passed its mechanical gate with a strong judge score.",
                "evidence": {"judge_score": payload.get("judge_score")},
            })
        if payload.get("judge_state") == "unreadable":
            signals.append({
                "id": "judge_axis_unknown",
                "label": "judge axis unknown",
                "tone": "warn",
                "claim": ("The judge verdict could not be read, so whether the judge ran "
                          "is unknown — not absent."),
                "evidence": {"judge_state": "unreadable"},
            })
        elif payload.get("judge_state") != "judged":
            signals.append({
                "id": "weak_confidence",
                "label": "weak confidence",
                "tone": "warn",
                "claim": "Only the mechanical axis is available; no usable judge verdict is present.",
                "evidence": {"judge_state": payload.get("judge_state")},
            })
        return signals

    pr = payload.get("pass_rate")
    jp = payload.get("judge_pass_rate")
    if pr is not None and jp is not None and (float(pr) - float(jp) > 0.15 or float(jp) - float(pr) > 0.05):
        signals.append({
            "id": "axis_divergence",
            "label": "axis divergence",
            "tone": "info",
            "claim": f"Checks pass {round(float(pr) * 100)}% while the judge approves {round(float(jp) * 100)}%.",
            "evidence": {"mechanical": pr, "judge": jp},
        })

    split = payload.get("type_split") or {}
    if split:
        split_rows = [
            (task, values, task)
            for task, values in split.items()
            if values.get("finished", 0) >= 2 and values.get("finished", 0)
        ]
    else:
        split_rows = [
            (row.get("task_id", ""), row, row.get("title") or row.get("task_title") or row.get("task_id", ""))
            for row in payload.get("task_rows") or []
            if row.get("finished", 0) >= 2
        ]
    typed = [(label, values, task_id) for task_id, values, label in split_rows]
    if len(typed) >= 2:
        best_type, best_values, best_task_id = max(
            typed, key=lambda item: item[1]["passed"] / item[1]["finished"])
        worst_type, worst_values, _ = min(
            typed, key=lambda item: item[1]["passed"] / item[1]["finished"])
        best_rate = best_values["passed"] / best_values["finished"]
        worst_rate = worst_values["passed"] / worst_values["finished"]
        if best_rate - worst_rate >= 0.20:
            signals.append({
                "id": "task_specialist",
                "label": "task specialist",
                "tone": "info",
                "claim": f"Task types split: {round(best_rate * 100)}% on {best_type} versus {round(worst_rate * 100)}% on {worst_type}.",
                "evidence": {"best_type": best_type, "worst_type": worst_type, "task_id": best_task_id},
            })

    if selected_row and selected_row.get("cost_per_pass") is not None:
        peer_dicts = peer_rows or [selected_row]
        peer_passes = [float(row.get("pass_rate") or 0) for row in peer_dicts
                       if row.get("pass_rate") is not None]
        peer_costs = [float(row["cost_per_pass"]) for row in peer_dicts
                      if row.get("cost_per_pass") is not None]
        selected_pass = float(selected_row.get("pass_rate") or 0)
        selected_cost = float(selected_row["cost_per_pass"])
        if (peer_passes and peer_costs and selected_pass >= max(peer_passes) - 0.10
                and selected_cost <= statistics.median(peer_costs)):
            signals.append({
                "id": "cost_frontier",
                "label": "cost frontier",
                "tone": "pass",
                "claim": f"The selected setup is at ${selected_cost:.4f} per successful finish.",
                "evidence": {"cost_per_pass": selected_cost, "pass_rate": selected_pass},
            })

    if pr is not None and int(payload.get("finished") or 0) >= 3 and float(pr) >= 0.80:
        signals.append({
            "id": "high_quality",
            "label": "high quality",
            "tone": "pass",
            "claim": f"Observed mechanical pass is {round(float(pr) * 100)}% across {payload.get('finished')} finished runs.",
            "evidence": {"pass_rate": pr, "n": payload.get("finished")},
        })

    ci = payload.get("pass_ci") or []
    wide = len(ci) == 2 and float(ci[1]) - float(ci[0]) >= 0.40
    unreadable = int(payload.get("judge_reports_unreadable") or 0)
    if int(payload.get("finished") or 0) < 3 or wide or not payload.get("judged"):
        if unreadable:
            # A read failure outranks the statistical hedges: it is a fact
            # about the data, and it must never read as "nothing judged yet".
            reason = (f"{unreadable} judge report(s) could not be read, so the "
                      "judge verdicts behind this card are unknown")
        elif int(payload.get("finished") or 0) < 3:
            reason = "the sample is thin"
        elif wide:
            reason = "the confidence interval is wide"
        else:
            reason = "no judge verdicts are available"
        signals.append({
            "id": "weak_confidence",
            "label": "weak confidence",
            "tone": "warn",
            "claim": f"Treat this as directional evidence: {reason}.",
            "evidence": {"finished": payload.get("finished"), "ci": payload.get("pass_ci"),
                         "judge_reports_unreadable": unreadable},
        })
    return signals


def _story_caption(payload: dict[str, Any], claim: str) -> str:
    if payload.get("kind") == "run":
        context = f"Status {payload.get('status') or 'unknown'} · ${float(payload.get('cost_usd') or 0):.4f} · {payload.get('latency_ms') or 0:.0f}ms"
    else:
        ci = payload.get("pass_ci")
        context = f"{payload.get('finished', 0)}/{payload.get('runs', 0)} finished"
        if ci:
            context += f" · 95% CI {round(float(ci[0]) * 100)}–{round(float(ci[1]) * 100)}%"
        unreadable = int(payload.get("judge_reports_unreadable") or 0)
        context += (f" · {payload.get('judged', 0)} judge-reviewed"
                    + (f", {unreadable} report(s) unreadable" if unreadable else ""))
    return _bounded_text(f"{claim} {context}.", max_bytes=270, max_lines=4)


def _attach_story(
    payload: dict[str, Any],
    metas: list[Any],
    *,
    lens: str = "overall",
    peer_rows: list[Any] | None = None,
    tasks_meta: dict[str, dict[str, str]] | None = None,
    representative: bool = True,
) -> dict[str, Any]:
    """Attach one shared story envelope to any card scope."""
    tasks_meta = tasks_meta or {}
    requested = lens if lens in CARD_LENS_IDS else "overall"
    peer_dicts = [_pairing_dict(row) for row in (peer_rows or [])]
    lens_rows = _lens_payloads(peer_dicts)
    lens_info = next((item for item in lens_rows if item["id"] == requested), lens_rows[0])
    if payload.get("kind") == "group":
        selected_target = lens_info["selected_target"]
    elif payload.get("kind") == "pairing":
        selected_target = payload.get("target", "")
    else:
        selected_target = payload.get("target", "")
    selected_row = next((row for row in peer_dicts if _pairing_target(row) == selected_target), None)
    if payload.get("kind") == "run":
        selected_row = None
    signals = _story_signals(payload, metas, selected_row, peer_dicts)
    proof_metas = metas
    if payload.get("kind") == "group" and selected_target:
        selected_pair = selected_target.split("|", 1)
        proof_metas = [
            meta for meta in metas
            if (meta.orchestrator, meta.worker) == tuple(selected_pair)
        ] or metas
    representative_meta = _representative_run(proof_metas, signals) if proof_metas else None
    proof = _proof_reference(representative_meta) if representative_meta else {
        "status": "unavailable", "run_id": None, "task_id": payload.get("task_id"),
        "inspect_url": "", "evidence_url": "", "transcript": None, "artifact": None,
    }
    proof["representative"] = representative
    best_task, worst_task = _task_evidence_refs(metas, tasks_meta)
    confidence = _confidence_payload(payload)
    claim = signals[0]["claim"] if signals else str(payload.get("verdict_line") or "Evidence is still incomplete.")
    caveats: list[str] = []
    if representative and proof.get("run_id"):
        caveats.append("Proof is one representative stored run, not the aggregate result.")
    elif representative:
        caveats.append("No representative stored proof is available for this scope.")
    if confidence["mechanical"]["level"] == "low":
        caveats.append("Mechanical confidence is low because the sample is thin or the interval is wide.")
    if not payload.get("judged") and payload.get("kind") != "run":
        caveats.append("No judge verdicts are present in this scope.")
    payload["lens"] = {
        "id": requested,
        "label": lens_info["label"],
        "description": lens_info["description"],
        "selected_target": selected_target,
        "reason": (
            "Direct run evidence; the selected lens is retained for continuity."
            if payload.get("kind") == "run"
            else lens_info.get("reason", "") if selected_target == lens_info.get("selected_target")
            else f"This card was opened directly; {lens_info['label']} currently selects {lens_info.get('selected_target') or 'no eligible row'}."
        ),
    }
    payload["story"] = {
        "scope": payload.get("kind"),
        "claim": claim,
        "caption": _story_caption(payload, claim),
        "lens": payload["lens"],
        "cohort": _cohort_payload(metas),
        "confidence": confidence,
        "signals": signals,
        "metrics": _story_metrics(payload),
        "proof": proof,
        "best_task": best_task,
        "worst_task": worst_task,
        "caveats": caveats,
        "provenance": {
            "suite": payload.get("suite"),
            "source": "orchestral observatory",
            "group": (
                payload.get("run_group") or next(iter(payload.get("groups") or []), None)
                if payload.get("kind") == "pairing"
                else payload.get("run_group")
            ),
        },
    }
    return payload


def _story_metrics(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("kind") == "run":
        verdict = "PASS" if payload.get("passes") else "FAIL" if payload.get("passes") is False else "—"
        judge_value = (
            f"{float(payload['judge_noul']):.2f}" if payload.get("judge_noul") is not None
            else f"{float(payload['judge_score']):.2f}" if payload.get("judge_score") is not None
            else "—"
        )
        return [
            {"id": "mechanical", "label": "mechanical", "value": verdict, "detail": payload.get("failure_reason") or "execution gate", "tone": "mech"},
            {"id": "judge", "label": "judge axis", "value": judge_value, "detail": payload.get("judge_state") or "not judged", "tone": "judge"},
            {"id": "cost", "label": "cost", "value": f"${float(payload.get('cost_usd') or 0):.4f}", "detail": f"{payload.get('latency_ms') or 0:.0f}ms", "tone": "cost"},
        ]
    judged = int(payload.get("judged") or 0)
    judge_value = f"{payload.get('judge_approved', 0)}/{judged}" if judged else "—"
    return [
        {
            "id": "mechanical",
            "label": "mechanical pass",
            "value": f"{payload.get('passed', 0)}/{payload.get('finished', 0)}",
            "detail": f"{round(float(payload['pass_rate']) * 100) if payload.get('pass_rate') is not None else '—'}% observed",
            "tone": "mech",
        },
        {
            "id": "judge",
            "label": "judge approved",
            "value": judge_value,
            "detail": f"{payload.get('judged', 0)} judged" if judged else "no judge evidence",
            "tone": "judge",
        },
        {
            "id": "cost",
            "label": "metered spend",
            "value": f"${float(payload.get('cost_usd') or 0):.4f}",
            "detail": "observed provider cost",
            "tone": "cost",
        },
    ]


def card_payload(
    store: RunStore,
    kind: str,
    target: str,
    group: str | None = None,
    tasks_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
    groups_file: Path | str | None = None,
    lens: str = "overall",
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
        scope_metas = _runs_for_group(store, group)
        metas = [m for m in scope_metas if m.orchestrator == orch and m.worker == worker]
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
        judged_n = 0
        unreadable_n = 0
        judge_passed_n = 0
        judge_models: set[str] = set()
        for m in cell:
            if m.failure_reason:
                failures[m.failure_reason] = failures.get(m.failure_reason, 0) + 1
            if m.status == "finished":
                st = per_type.setdefault(types.get(m.task_id, "?"), [0, 0])
                st[1] += 1
                st[0] += 1 if m.passes else 0
                j, read_why = _judge_block(m.run_dir)
                if read_why:
                    unreadable_n += 1
                if any(j.get(key) is not None for key in ("score", "noul", "passed")):
                    judged_n += 1
                if j.get("score") is not None:
                    judged_scores.append(float(j["score"]))
                    per_type_judge.setdefault(types.get(m.task_id, "?"), []).append(float(j["score"]))
                if j.get("passed") is True:
                    judge_passed_n += 1
                if j.get("model"):
                    judge_models.add(j["model"])
        pr = passed / len(finished) if finished else None
        ci = _wilson(passed, len(finished))
        top_failure = max(failures.items(), key=lambda kv: kv[1])[0] if failures else None
        groups = sorted({m.run_group for m in cell if m.run_group})
        strong = sorted(((t, p, n) for t, (p, n) in per_type.items() if n),
                        key=lambda x: (-(x[1] / x[2]), x[0]))
        best, worst = (strong[0] if strong else None), (strong[-1] if strong else None)
        if unreadable_n and not judged_n:
            # The semantic axis is unknown, not absent. "nothing judged yet"
            # would assert the judge never ran — a claim the data cannot make.
            line = (f"judge verdict unknown for {unreadable_n} of {len(finished)} "
                    "finished run(s) — report.json could not be read")
        else:
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
            line += _unreadable_caveat(unreadable_n)
        payload = {
            "kind": "pairing", "target": target, "suite": SUITE_VERSION,
            "target_pair": target,
            "orchestrator": orch, "worker": worker,
            "runs": len(cell), "finished": len(finished), "passed": passed,
            "pass_rate": pr, "pass_ci": ci, "verdict_line": line,
            "score_mean": round(sum(scores) / len(scores), 3) if scores else None,
            "judged": judged_n,
            "judge_reports_unreadable": unreadable_n,
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
        return _attach_story(
            payload,
            cell,
            lens=lens,
            peer_rows=pairing_leaderboard(scope_metas, unmetered_workers=store.unmetered_workers()),
            tasks_meta=_task_meta(store, tasks_dir),
        )
    if kind == "group":
        g = next((x for x in groups_payload(store, groups_file) if x["group"] == target), None)
        if g is None:
            return None
        metas = _runs_for_group(store, target)
        cells = aggregate(metas)
        pairings = sorted({(c.orchestrator, c.worker) for c in cells})
        tmeta = _task_meta(store, tasks_dir)
        # judge aggregates — read each finished run's report for the
        # semantic axis; unjudged runs contribute nothing, honestly
        judge_scores: list[float] = []
        judge_nouls: list[float] = []
        card_judge_models: set[str] = set()
        judged_n = 0
        unreadable_n = 0
        judge_passed_n = 0
        # comparable rows — per-task split is always meaningful; per-pairing
        # rows matter when the eval set ran more than one pairing
        per_task: dict[str, list[int]] = {}
        task_judge: dict[str, list[float]] = {}
        per_pair: dict[tuple[str, str], list[int]] = {}
        pair_judge: dict[tuple[str, str], list[float]] = {}
        pair_jpassed: dict[tuple[str, str], int] = {}
        pair_judged: dict[tuple[str, str], int] = {}
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
            j, read_why = _judge_block(m.run_dir)
            if read_why:
                unreadable_n += 1
            has_judge = any(j.get(key) is not None for key in ("score", "noul", "passed"))
            if has_judge:
                judged_n += 1
                pair_judged[key] = pair_judged.get(key, 0) + 1
            if j.get("score") is not None:
                judge_scores.append(float(j["score"]))
                pair_judge.setdefault(key, []).append(float(j["score"]))
                task_judge.setdefault(m.task_id, []).append(float(j["score"]))
            if j.get("noul") is not None:
                judge_nouls.append(float(j["noul"]))
            if j.get("model"):
                card_judge_models.add(j["model"])
            if j.get("passed") is True:
                judge_passed_n += 1
                pair_jpassed[key] = pair_jpassed.get(key, 0) + 1
        jp_rate = judge_passed_n / judged_n if judged_n else None
        ci = _wilson(g["passed"], g["finished"])
        failed_n = sum(1 for m in metas if m.status == "failed")
        running_n = sum(1 for m in metas if m.status == "running")
        if not card_judge_models and judged_n:
            card_judge_models = set(store.judge_slugs({m.task_id for m in metas}))
        if unreadable_n and not judged_n:
            # The semantic axis is unknown, not absent. "nothing judged yet"
            # would assert the judge never ran — a claim the data cannot make.
            line = (f"judge verdict unknown for {unreadable_n} of {g['finished']} "
                    "finished run(s) — report.json could not be read")
        else:
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
            line += _unreadable_caveat(unreadable_n)
        payload = {
            "kind": "group", "target": target, "suite": SUITE_VERSION,
            "runs": g["runs"], "finished": g["finished"], "passed": g["passed"],
            "failed": failed_n, "running": running_n,
            "pass_rate": g["pass_rate"], "score_median": g["score_median"],
            "judge_score_median": g["judge_score_median"],
            "pass_ci": ci, "verdict_line": line,
            "judged": judged_n, "judge_reports_unreadable": unreadable_n,
            "judge_approved": judge_passed_n,
            "judge_pass_rate": jp_rate,
            "judge_score_mean": (round(sum(judge_scores) / len(judge_scores), 3)
                                 if judge_scores else None),
            "judge_noul_mean": (round(sum(judge_nouls) / len(judge_nouls), 3)
                                if judge_nouls else None),
            "card_judge_models": sorted(card_judge_models),
            "judge_calibration": _calibration_map(reports_dir, card_judge_models),
            "cost_usd": g["cost_usd"], "tasks": g["tasks"],
            "pairings": [{"orchestrator": o, "worker": w} for o, w in pairings],
            "pairing_rows": [
                {"orchestrator": o, "worker": w,
                 "finished": n, "passed": p,
                 "pass_rate": round(p / n, 3),
                 "judged": pair_judged.get(key, 0),
                 "judge_approved": pair_jpassed.get(key, 0),
                 "judge_score_mean": (round(sum(pair_judge[key]) / len(pair_judge[key]), 3)
                                      if pair_judge.get(key) else None),
                 "cost_usd": round(pair_cost[key], 4)}
                for (o, w), (p, n) in sorted(
                    per_pair.items(), key=lambda kv: (-(kv[1][0] / kv[1][1]), kv[0][0]))
                for key in [(o, w)]
            ],
            "task_rows": [
                {"task_id": t,
                 "title": (tmeta.get(t) or {}).get("title") or "",
                 "task_title": (tmeta.get(t) or {}).get("title") or "",
                 "blurb": (tmeta.get(t) or {}).get("blurb") or "",
                 "passed": p, "finished": n,
                 "pass_rate": round(p / n, 3),
                 "judge_score": (round(sum(task_judge[t]) / len(task_judge[t]), 3)
                                 if task_judge.get(t) else None)}
                for t, (p, n) in sorted(
                    per_task.items(), key=lambda kv: (-(kv[1][0] / kv[1][1]), kv[0]))
            ],
            "group_label": g.get("label") or "",
            "group_description": g.get("description") or "",
            "latest": g["latest"],
            "explainer": _explainer("group", {}),
            "flag": ann.get("flag", ""), "note": ann.get("note", ""),
        }
        payload["description"] = _eval_description(payload, "group")
        return _attach_story(
            payload,
            metas,
            lens=lens,
            peer_rows=pairing_leaderboard(metas, unmetered_workers=store.unmetered_workers()),
            tasks_meta=tmeta,
        )
    if kind == "run":
        meta = store.get_run(target)
        if meta is None:
            return None
        report = _mapping(read_json(Path(meta.run_dir) / "report.json"))
        judge = _mapping(report.get("judge"))
        plan = _mapping(read_json(Path(meta.run_dir) / "plan.json"))
        plan_summary = str(plan.get("plan") or "").strip() if isinstance(plan, dict) else ""
        judge_reason = str(judge.get("reasoning") or "").strip()
        if judge_reason:
            description, description_by = judge_reason, "judge"
        elif plan_summary:
            description, description_by = plan_summary, "orchestrator"
        else:
            description, description_by = "", ""
        tm = _task_meta(store, tasks_dir).get(meta.task_id) or {}
        gm = _groups_meta(groups_file).get(meta.run_group or "") or {}
        jstate, jstate_reason = judge_state(meta)
        # comparables: every pairing that has attempted this same task —
        # the run's numbers mean more next to how others did on it
        sib: dict[tuple[str, str], list] = {}
        for r in store.list_runs(task_id=meta.task_id):
            sib.setdefault((r.orchestrator, r.worker), []).append(r)
        pair_rows = []
        for (o, w), rs in sorted(sib.items()):
            fin = [r for r in rs if r.status == "finished"]
            js = [r.judge_score for r in fin if r.judge_score is not None]
            pair_rows.append({
                "orchestrator": o, "worker": w,
                "n": len(rs), "finished": len(fin),
                "passed": sum(1 for r in fin if r.passes),
                "pass_rate": (sum(1 for r in fin if r.passes) / len(fin)) if fin else None,
                "judge_score": mean(js) if js else None,
                "cost_usd": sum(r.total_cost_usd or 0 for r in rs),
                "self": (o, w) == (meta.orchestrator, meta.worker),
            })
        pair_rows.sort(key=lambda x: (-float(x["pass_rate"] or -1), x["orchestrator"]))
        payload = {
            "kind": "run", "target": target, "suite": SUITE_VERSION,
            "task_id": meta.task_id, "orchestrator": meta.orchestrator,
            "task_title": tm.get("title") or "", "task_blurb": tm.get("blurb") or "",
            "group_label": gm.get("label") or "",
            "judge_state": jstate, "judge_state_reason": jstate_reason,
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
            "pair_rows": pair_rows,
            "explainer": _explainer("run", {}),
            "flag": ann.get("flag", ""), "note": ann.get("note", ""),
        }
        return _attach_story(
            payload,
            [meta],
            lens=lens,
            tasks_meta={meta.task_id: tm},
            representative=False,
        )
    return None


def run_evidence_payload(
    store: RunStore,
    run_id: str,
    *,
    max_bytes: int = 6000,
    max_lines: int = 80,
) -> dict[str, Any] | None:
    """Return bounded terminal and artifact previews for the card proof panel."""
    meta = store.get_run(run_id)
    if meta is None:
        return None
    max_bytes = max(256, min(int(max_bytes), 16000))
    max_lines = max(4, min(int(max_lines), 160))
    run_dir = Path(meta.run_dir)
    report = _mapping(read_json(run_dir / "report.json"))
    execution = _mapping(report.get("execution"))
    output = str(execution.get("output_tail") or "")
    raw_transcript = output or _event_transcript(run_dir)
    transcript_text = _bounded_text(raw_transcript, max_bytes=max_bytes, max_lines=max_lines)
    proof = _proof_reference(meta)
    transcript = proof.get("transcript")
    if transcript:
        transcript = {
            **transcript,
            "text": transcript_text,
            "truncated": len(raw_transcript) > len(transcript_text),
        }
    artifact = proof.get("artifact")
    if artifact:
        artifact = dict(artifact)
        artifact["preview"] = None
        full_size = int(artifact.get("bytes") or 0)
        if artifact.get("kind") not in {"image", "video"}:
            member = artifact.get("name") if artifact.get("media_type") == "archive" else None
            body = b""
            try:
                if member:
                    with zipfile.ZipFile(run_dir / "artifact.zip") as archive:
                        info = archive.getinfo(member)
                        full_size = max(0, int(info.file_size))
                        if (
                            not info.is_dir()
                            and not Path(member.replace("\\", "/")).is_absolute()
                            and ".." not in Path(member.replace("\\", "/")).parts
                        ):
                            with archive.open(info) as source_file:
                                body = source_file.read(max_bytes * 4)
                else:
                    artifact_path = run_dir / str(artifact["name"])
                    if artifact_path.is_file():
                        full_size = artifact_path.stat().st_size
                        with artifact_path.open("rb") as source_file:
                            body = source_file.read(max_bytes * 4)
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                body = b""
            artifact["bytes"] = full_size
            if body:
                preview = body.decode("utf-8", errors="replace")
                artifact["preview"] = _bounded_text(
                    preview, max_bytes=max_bytes, max_lines=max_lines, tail=False,
                )
                artifact["truncated"] = (
                    full_size > len(body) or len(preview) > len(artifact["preview"])
                )
            else:
                artifact["truncated"] = False
    proof["transcript"] = transcript
    proof["artifact"] = artifact
    return proof


def card_catalog_payload(
    store: RunStore,
    *,
    tasks_dir: Path | str | None = None,
    reports_dir: Path | str | None = None,
    groups_file: Path | str | None = None,
    group: str | None = None,
    scope: str = "all",
    lens: str = "overall",
    flagged: bool = False,
) -> dict[str, Any]:
    """Build a bounded gallery of card previews from the same payloads as cards."""
    if scope not in {"all", "group", "pairing"}:
        scope = "all"
    groups = groups_payload(store, groups_file)
    if group:
        groups = [row for row in groups if row["group"] == group]
    cards: list[dict[str, Any]] = []
    if scope in {"all", "group"}:
        for row in groups:
            card = card_payload(
                store, "group", row["group"], lens=lens,
                tasks_dir=tasks_dir, reports_dir=reports_dir, groups_file=groups_file,
            )
            if card:
                cards.append(card)
    if scope in {"all", "pairing"}:
        pairings = pairings_payload(store, tasks_dir=tasks_dir, group=group or None)
        seen: set[str] = set()
        for row in pairings["rows"]:
            target = row["target"]
            if target in seen:
                continue
            seen.add(target)
            card = card_payload(
                store, "pairing", target, group=group or None, lens=lens,
                tasks_dir=tasks_dir, reports_dir=reports_dir, groups_file=groups_file,
            )
            if card:
                cards.append(card)
    if flagged:
        cards = [card for card in cards if card.get("flag")]
    # Recency is the default within each annotation state; flagged stories
    # remain pinned above unflagged stories without hiding newer evidence.
    cards.sort(
        key=lambda card: (card.get("story") or {}).get("cohort", {}).get("latest", "")
        or card.get("latest") or "",
        reverse=True,
    )
    cards.sort(key=lambda card: {"interesting": 0, "not": 1}.get(card.get("flag", ""), 2))
    return {
        "cards": cards[:60],
        "groups": groups_payload(store, groups_file),
        "lenses": list(CARD_LENSES),
        "scopes": [
            {"id": "all", "label": "All stories"},
            {"id": "group", "label": "Run groups"},
            {"id": "pairing", "label": "Pairings"},
        ],
        "filters": {"group": group or "", "scope": scope, "lens": lens, "flagged": flagged},
    }


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
