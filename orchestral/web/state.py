"""Pure domain state for the web observatory — no http imports, unit-testable.

Same rule as ``orchestral.tui.state``: this module owns derivation and the
job lifecycle; the server layer only routes requests to it. Run/event
derivation itself is reused from ``orchestral.tui.state`` (verified
textual-free) so the two surfaces can never disagree about what a run is
doing.
"""

from __future__ import annotations

import contextlib
import functools
import json
import statistics
import subprocess
import threading
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import yaml

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
from orchestral.format import (
    LOW_N_BEST,
    LOW_N_CELL,
    NULL_GLYPH,
    fmt_duration_ms,
    fmt_money,
    fmt_percent,
    fmt_range_pct,
    fmt_score,
    is_low_n_best,
    is_low_n_cell,
)
from orchestral.judge import DEFAULT_JUDGE
from orchestral.privacy import run_is_holdout
from orchestral.runner import Runner
from orchestral.stats import aggregate, mean, pairing_leaderboard, wilson_interval
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

    def owns(self, run_id: str) -> bool:
        """True when this process started run_id and its job is still active."""
        job = self.job_for_run(run_id)
        return bool(job and job.active)

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
    return f" · {unreadable_n} judge report(s) unreadable, semantic axis incomplete"


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


def _public_run(r: Any) -> dict[str, Any]:
    """`RunMeta.to_public_dict()` plus the billed cost and how it was derived.

    `total_cost_usd` stays (it is the recorded rate-card total); readers show
    `billed_cost_usd`, which includes failed runs' spend.
    """
    d = r.to_public_dict()
    d["billed_cost_usd"] = r.display_cost_usd
    d["cost_basis"] = r.cost_basis or "calibrated"
    return d


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


def _pair_brief(p: Any) -> dict[str, Any]:
    return {"orchestrator": p.orchestrator, "worker": p.worker,
            "pass_rate": p.pass_rate, "cost_per_pass": p.cost_per_pass,
            "runs": p.runs}


def thread_context(store: RunStore, kind: str, target: str,
                   group: str | None = None, lens: str = "overall") -> dict[str, Any]:
    """Leaderboard framing for a publish thread: where the card's subject
    sits among its peers — rank, immediate neighbors, and cost standing.

    Ranks inside the requested lens's own ``ranking`` — the same ordering
    and denominator the leaderboard view prints for that lens — so a posted
    ``rank/board_size`` is the same ``N / M`` a reader sees on the board.
    A thin pairing reports ``low_sample``; a pairing the lens legitimately
    excludes (unmetered rows leave cost lenses, unjudged rows leave the
    divergence lens) reports ``unranked`` with the lens label, not a
    board-invisible number. Scoped to ``group`` when set; group cards
    always scope to their own target cohort (same as ``card_payload``)
    and run cards to their run's group.
    """
    scope = group
    orch = worker = None
    if kind == "pairing" and "|" in target:
        orch, worker = target.split("|", 1)
    elif kind == "group":
        scope = target
    elif kind == "run":
        meta = store.get_run(target)
        if meta is not None:
            orch, worker = meta.orchestrator, meta.worker
            scope = scope or meta.run_group
    metas = _runs_for_group(store, scope)
    unmetered = store.unmetered_workers()
    board = [p for p in pairing_leaderboard(metas, unmetered_workers=unmetered)
             if not p.holdout_only]
    lens_rows = _lens_payloads(board)
    requested = lens if lens in CARD_LENS_IDS else "overall"
    lens_info = next((item for item in lens_rows if item["id"] == requested),
                     lens_rows[0])
    ranking = lens_info.get("ranking") or []
    by_target = {f"{p.orchestrator}|{p.worker}": p for p in board}
    ctx: dict[str, Any] = {"board_size": len(ranking),
                           "lens_label": lens_info.get("label") or requested}

    if orch and worker:
        subject_target = f"{orch}|{worker}"
        subject = by_target.get(subject_target)
        if subject is not None:
            ctx["cost_total"] = subject.cost_total
            ctx["runs"] = subject.runs
            if subject.low_sample or not subject.finished:
                ctx["low_sample"] = True
            if orch in unmetered or worker in unmetered:
                ctx["unmetered"] = True
        idx = ranking.index(subject_target) if subject_target in ranking else None
        if idx is not None:
            ctx["rank"] = idx + 1
            row = by_target.get(ranking[idx])
            if row is not None:
                ctx["cost_per_pass"] = row.cost_per_pass
            if idx:
                ctx["above"] = _pair_brief(by_target[ranking[idx - 1]])
            if idx + 1 < len(ranking):
                ctx["below"] = _pair_brief(by_target[ranking[idx + 1]])
        elif subject is not None and not ctx.get("low_sample"):
            ctx["unranked"] = True
        eligible = sorted(
            (p for p in board if not p.low_sample and p.finished),
            key=lambda p: _pairing_quality_key(p.to_dict()),
        )
        metered = sorted((p for p in eligible if p.cost_per_pass is not None),
                         key=lambda p: cast(float, p.cost_per_pass))
        ci = next((i for i, p in enumerate(metered)
                   if p.orchestrator == orch and p.worker == worker), None)
        if ci is not None:
            ctx["cost_rank"] = ci + 1
            ctx["cost_of"] = len(metered)
            ctx["cheapest_pass"] = _pair_brief(metered[0])
    elif kind == "group" and ranking:
        top = by_target.get(ranking[0])
        if top is not None:
            ctx["top"] = _pair_brief(top)
    return ctx


# KTD8: live is derived, not registered. A `running` index row is live while its
# last event is younger than this; past it the row is `stalled`. Stalled is a
# display state and is never written to the index.
STALL_AFTER_S = 600
_HEARTBEAT_TAIL_BYTES = 16 * 1024


class LivenessRefusal(Exception):
    """A liveness action that must not happen; carries the HTTP status to answer."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def liveness_state(idle_s: float | None) -> str:
    """live while the heartbeat is at most STALL_AFTER_S old; stalled past it or unknown."""
    return "live" if idle_s is not None and idle_s <= STALL_AFTER_S else "stalled"


def last_heartbeat(run_dir: Path | str, started_at: str | None = None) -> datetime | None:
    """Time of the last event in events.jsonl, else the file's mtime, else started_at."""
    path = Path(run_dir) / "events.jsonl"
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - _HEARTBEAT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ts = _parse_ts(ev.get("timestamp")) if isinstance(ev, dict) else None
            if ts:
                return ts
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return _parse_ts(started_at) if started_at else None


def _abandoned_runs(store: RunStore) -> set[str]:
    return {a["target"] for a in store.annotations()
            if a["kind"] == "run" and a["flag"] == "aborted"}


def _event_spend(events: list[dict[str, Any]]) -> float:
    total = 0.0
    for ev in events:
        if ev.get("type") == "llm_call":
            c = ev.get("cost") or {}
            billed = c.get("api_cost_usd")
            total += float(billed if billed is not None else (c.get("usd") or 0))
    return total


def live_runs(store: RunStore, registry: JobRegistry, now: datetime | None = None) -> list[dict[str, Any]]:
    """Everything running right now: index `running` rows (CLI-launched included)
    plus this server's active jobs, each exactly once. Rows the operator marked
    abandoned are terminal and absent. `owned` means this process can cancel it."""
    now = now or _now()
    abandoned = _abandoned_runs(store)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in store.list_runs(limit=None):
        if r.status != "running" or r.run_id in abandoned:
            continue
        seen.add(r.run_id)
        beat = last_heartbeat(r.run_dir, r.started_at) if r.run_dir else _parse_ts(r.started_at)
        idle = max(0.0, (now - beat).total_seconds()) if beat else None
        events, _ = tail_events(Path(r.run_dir) / "events.jsonl", 0) if r.run_dir else ([], 0)
        started = _parse_ts(r.started_at)
        job = registry.job_for_run(r.run_id)
        owned = bool(job and job.active)
        state_ = liveness_state(idle)
        spend = r.display_cost_usd or _event_spend(events)
        rows.append({
            "run_id": r.run_id,
            "label": f"{r.task_id}·{r.worker.split('/')[-1]}",
            "task_id": r.task_id, "orchestrator": r.orchestrator, "worker": r.worker,
            "run_group": r.run_group,
            "phase": run_phase(events),
            "started_at": r.started_at,
            "elapsed_s": int((now - started).total_seconds()) if started else None,
            "elapsed": fmt_elapsed(r.started_at),
            "last_event_at": beat.isoformat() if beat else None,
            "idle_s": int(idle) if idle is not None else None,
            "spend_usd": spend,
            "owned": owned,
            "state": state_,
            "stalled": state_ == "stalled",
            "cancellable": owned,
            "abandonable": state_ == "stalled" and not owned,
            "detail": job.detail if job else "",
        })
    for job in registry.jobs:
        if not job.active or any(rid in seen for rid in job.run_ids):
            continue
        rows.append({
            "run_id": None, "label": job.label, "task_id": None, "orchestrator": None,
            "worker": None, "run_group": None, "phase": "starting", "started_at": None,
            "elapsed_s": None, "elapsed": NULL_GLYPH, "last_event_at": None, "idle_s": None,
            "spend_usd": 0.0, "owned": True, "state": "live", "stalled": False,
            "cancellable": True, "abandonable": False, "detail": job.detail,
        })
    rows.sort(key=lambda row: (row["stalled"], -(row["elapsed_s"] or 0)))
    return rows


def _job_row(row: dict[str, Any]) -> dict[str, Any]:
    """A live_runs row in the overview `jobs` shape (old keys kept)."""
    return {**row, "status": "running",
            "run_ids": [row["run_id"]] if row["run_id"] else []}


def abandon_run(store: RunStore, registry: JobRegistry, run_id: str,
                now: datetime | None = None) -> dict[str, Any]:
    """Retire an orphan by writing the `aborted` run annotation. The run's index
    status and spend are untouched. Idempotent; refuses finished, owned and
    recently active runs."""
    meta = store.get_run(run_id)
    if meta is None:
        raise LivenessRefusal(f"There is no run {run_id}.", 404)
    if meta.status != "running":
        raise LivenessRefusal(
            f"Run {run_id} is already {meta.status}, so there is nothing to abandon.")
    if run_id in _abandoned_runs(store):
        return {"run_id": run_id, "abandoned": True, "already": True}
    if registry.owns(run_id):
        raise LivenessRefusal(
            f"Run {run_id} belongs to a job in this server. Cancel it instead of abandoning it.")
    now = now or _now()
    beat = last_heartbeat(meta.run_dir, meta.started_at) if meta.run_dir else None
    idle = max(0.0, (now - beat).total_seconds()) if beat else None
    if liveness_state(idle) == "live":
        raise LivenessRefusal(
            f"Run {run_id} has a recent event ({int(idle or 0)} seconds ago), so it may still be "
            f"running. It can be abandoned after {STALL_AFTER_S // 60} minutes without events.")
    store.set_annotation("run", run_id, "aborted", note="abandoned: no owner and no recent events")
    return {"run_id": run_id, "abandoned": True, "already": False}


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
        d = _public_run(r)
        d["task_title"] = (tmeta.get(r.task_id) or {}).get("title") or ""
        d["judge_state"], d["judge_reason"] = judge_state(r)
        recent.append(d)
    now = _now()
    live = live_runs(store, registry, now=now)
    needs_look = needs_look_items(store, registry.models_dir, live)
    return {
        "generated_at": now.isoformat(),
        "jobs": [_job_row(r) for r in live],
        "changes": changes_rows(runs, tmeta),
        "needs_look": needs_look[:NEEDS_LOOK_CAP],
        "needs_look_total": len(needs_look),
        "leaderboard": [r.to_dict() for r in lb[:10]],
        "recent": recent,
        "groups": groups_payload(store, groups_file)[:8],
        "taxonomy": dict(sorted(taxonomy.items(), key=lambda kv: -kv[1])),
    }


CHANGES_CAP = 500
CHANGES_WINDOW = timedelta(days=30)
NEEDS_LOOK_CAP = 50
# Failure categories that say the environment failed, not the model's output.
_INFRA_REASONS = frozenset({
    "rate_limit", "auth", "timeout", "transport", "provider_error", "submitted_job",
    "config", "executor_preflight", "executor_exit", "executor_timeout",
    "executor_no_output", "spawn_failed", "workspace",
})


def _is_infra(reason: str | None) -> bool:
    code = (reason or "").removeprefix("exception:")
    return code in _INFRA_REASONS or code.endswith("_timeout")


def changes_rows(runs: list[Any], tmeta: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Terminal real runs, newest first, for the client-side "changed since you
    last looked" band. The watermark lives on the device, so the server only
    supplies a bounded window. Running rows belong to Live; dry runs are not
    results."""
    cutoff = _now() - CHANGES_WINDOW
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for r in runs:
        if r.status == "running" or r.dry_run or not r.finished_at:
            continue
        done = _parse_ts(r.finished_at)
        if done is None or done < cutoff:
            continue
        rows.append((done, {
            "run_id": r.run_id, "task_id": r.task_id,
            "task_title": (tmeta.get(r.task_id) or {}).get("title") or "",
            "status": r.status, "passes": r.passes, "failure_reason": r.failure_reason,
            "cost_usd": r.display_cost_usd, "finished_at": r.finished_at,
        }))
    rows.sort(key=lambda t: t[0], reverse=True)
    return [row for _, row in rows[:CHANGES_CAP]]


def needs_look_items(store: RunStore, models_dir: Path | str,
                     live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What deserves a human glance: stalled runs, infra errors, inconclusive
    judges, flagged items and pricing drift. Each item names its target and
    links to it; none of them is a verdict about the model."""
    from orchestral.pricing import pricing_drift

    items: list[dict[str, Any]] = []
    for row in live:
        if row["stalled"]:
            items.append({
                "kind": "stalled", "run_id": row["run_id"], "title": f"{row['label']} has gone quiet",
                "detail": "No events for a while. Check whether it is still running.",
                "href": f"#/run/{row['run_id']}" if row["run_id"] else "#/runs"})
    for r in store.list_runs(limit=None):
        if r.dry_run or r.status == "running":
            continue
        if r.status == "failed" and _is_infra(r.failure_reason):
            items.append({
                "kind": "infra_error", "run_id": r.run_id,
                "title": f"{r.task_id} hit an infrastructure error",
                "detail": f"{r.failure_reason}: the run did not get a fair attempt.",
                "href": f"#/run/{r.run_id}"})
        elif r.status == "finished" and r.judge_score is None and judge_state(r)[0] == "inconclusive":
            items.append({
                "kind": "inconclusive_judge", "run_id": r.run_id,
                "title": f"The judge was inconclusive on {r.task_id}",
                "detail": judge_state(r)[1], "href": f"#/run/{r.run_id}"})
    for a in store.annotations():
        if a["flag"] != "interesting":
            continue
        href = (f"#/runs?group={quote(a['target'], safe='')}" if a["kind"] == "group"
                else f"#/run/{quote(a['target'], safe='')}" if a["kind"] == "run" else "#/runs")
        items.append({
            "kind": "flagged", "run_id": a["target"] if a["kind"] == "run" else None,
            "title": f"You flagged {a['kind']} {a['target']}", "detail": a["note"] or "Flagged as interesting.",
            "href": href})
    models = _configured_models(Path(models_dir))
    for d in pricing_drift(store.calls_pricing_summary(), models):
        if d.drifted and d.ratio is not None:
            items.append({
                "kind": "pricing_drift", "run_id": None,
                "title": f"{d.model} bills {d.ratio:.2f}x its rate card",
                "detail": f"Across {d.api_calls} provider-reported calls. The configured price may be stale.",
                "href": "#/models"})
    return items


# What the local observatory can do that the hosted mirror cannot (R13). The
# SPA reads these flags; it never infers a capability from the HTTP status.
CAPABILITY_FLAGS = ("launch", "cancel", "flag_write", "thread", "png_capture", "live_stream")


@functools.cache
def _source_commit() -> str | None:
    """Short commit of the checkout serving the UI; None outside a git tree."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


def meta_payload(
    mode: str = "local",
    synced_at: str | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    """The capabilities document (KTD5): mode, freshness, capability flags and
    the sample-size thresholds, so the SPA picks its adapter from one place."""
    if mode not in {"local", "hosted"}:
        raise ValueError(f"meta mode must be 'local' or 'hosted', got {mode!r}")
    local = mode == "local"
    return {
        "mode": mode,
        "synced_at": synced_at,
        "source_commit": source_commit if source_commit is not None or not local else _source_commit(),
        "capabilities": dict.fromkeys(CAPABILITY_FLAGS, local),
        "low_n": {"cell": LOW_N_CELL, "best": LOW_N_BEST},
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
# Spend estimates — shown before any paid action so a click never spends
# blind. They come from billed history (`calls.api_cost_usd`), with calls the
# provider never priced scaled by a per-model billed/rate-card ratio, because
# the configured rate card has run from 0.99x to 13.25x under the real bill
# depending on the model (see orchestral/budget.py and KTD7).

# POST /api/thread sends at most 12,000 characters of card JSON plus the
# prompt template (~4 chars/token) and caps the reply at 6,000 tokens.
THREAD_INPUT_TOKENS_MAX = 4_000
THREAD_OUTPUT_TOKENS_MAX = 6_000

# The dedicated eval key's monthly cap, shown beside month-to-date spend. The
# index only knows what this machine recorded, so the label says so.
EVAL_MONTHLY_CAP_USD = 50.0

SPEND_CAVEAT = ("Based on past billed runs, not a quote. The range is the middle 80% "
                "of what those runs cost.")
THREAD_CAVEAT = ("An upper bound for one call. The bill follows the length of the reply, "
                 "which is capped.")
SPEND_CAVEAT_UNKNOWN = ("No billed history for this pairing, so the cost is unknown, "
                        "not zero. It can still bill.")


def _configured_models(models_dir: Path) -> dict[str, Any]:
    try:
        return {m.slug: m for m in load_models(models_dir)}
    except Exception:
        return {}


def _rate_card(cfg: Any) -> dict[str, float] | None:
    if cfg is None:
        return None
    return {"input_per_mtok": float(cfg.input_price_per_mtok),
            "output_per_mtok": float(cfg.output_price_per_mtok)}


def _ratio_rows(store: Any, slugs: list[str]) -> list[dict[str, Any]]:
    """The billed/rate-card ratio chosen for each model and where it came from."""
    cal = store.cost_calibration()
    rows = []
    for slug in dict.fromkeys(s for s in slugs if s):
        choice = cal.ratio_for(slug)
        rows.append({"model": slug, "ratio": choice.ratio, "source": choice.source, "n": choice.n})
    return rows


def _ratio_sentence(rows: list[dict[str, Any]]) -> str:
    parts = []
    for r in rows:
        if r["ratio"] is None:
            parts.append(f"{r['model']}: no priced calls yet")
        else:
            parts.append(f"{r['model']} {r['ratio']:.2f}x ({r['source']}, n={r['n']})")
    if not parts:
        return ""
    return ("Unpriced calls are scaled by billed/rate-card ratios: " + "; ".join(parts)
            + ". Own means the model's priced calls, global means all priced calls.")


def _spend_context(store: Any) -> dict[str, Any]:
    return {"month_to_date_billed_usd": store.month_to_date_billed_usd(),
            "monthly_cap_usd": EVAL_MONTHLY_CAP_USD,
            "month_to_date_note": "Billed spend recorded in this index this month."}


def launch_estimate(store: RunStore, models_dir: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Estimated billed cost of a New run launch, with a range and its basis.

    Prefers the same task + pairing, then the pairing on any task, from billed
    history that includes failed runs (``RunStore.billed_estimate``). With no
    history the estimate is ``None`` and the UI must say "unknown" rather than
    imply $0. Dry runs are $0.
    """
    try:
        replicates = max(1, int(spec.get("replicates") or 1))
    except (TypeError, ValueError):
        replicates = 1
    task = str(spec.get("task") or "")
    orch = str(spec.get("orchestrator") or "")
    worker = str(spec.get("worker") or "")
    judge = str(spec.get("judge") or "")
    if spec.get("dry_run"):
        return {"dry_run": True, "per_run_usd": 0.0, "low_usd": 0.0, "high_usd": 0.0,
                "total_usd": 0.0, "total_low_usd": 0.0, "total_high_usd": 0.0,
                "replicates": replicates, "basis": "dry_run",
                "basis_label": "Dry run: stub models, no API calls", "caveat": "",
                "ratios": [], **_spend_context(store)}
    est = store.billed_estimate(task, orch, worker) if orch and worker else None
    ratios = _ratio_rows(store, [orch, worker, judge])
    per_run = low = high = None
    basis = "unknown"
    label = "There are no past billed runs of this pairing to estimate from."
    if est is not None and est.per_run_usd is not None:
        per_run, low, high, basis = est.per_run_usd, est.low_usd, est.high_usd, est.basis
        scope = ("of this task with this pairing" if basis == "task_pairing"
                 else "of this pairing on other tasks")
        label = (f"Mean billed cost of past runs {scope} (n={est.n}, failed runs included). "
                 + _ratio_sentence(ratios))
    models = _configured_models(models_dir)
    rates = {role: _rate_card(models.get(slug))
             for role, slug in (("orchestrator", orch), ("worker", worker), ("judge", judge))
             if slug}

    def times(v: float | None) -> float | None:
        return v * replicates if v is not None else None

    return {
        "dry_run": False,
        "per_run_usd": per_run,
        "low_usd": low,
        "high_usd": high,
        "total_usd": times(per_run),
        "total_low_usd": times(low),
        "total_high_usd": times(high),
        "replicates": replicates,
        "basis": basis,
        "basis_label": label.strip(),
        "ratios": ratios,
        "rates": rates,
        "caveat": SPEND_CAVEAT if per_run is not None else SPEND_CAVEAT_UNKNOWN,
        **_spend_context(store),
    }


def thread_estimate(models_dir: Path, slug: str, *, provider_ready: bool,
                    store: RunStore | None = None) -> dict[str, Any]:
    """Upper-bound cost of one Write thread call with writer ``slug``.

    ``max_usd`` is the rate-card bound; ``high_usd`` scales it by the model's
    billed/rate-card ratio when the index has one. No paid call happens without
    a configured provider key, so ``will_spend`` is False then and the server
    falls back to templates.
    """
    slug = slug.strip()
    ctx = _spend_context(store) if store is not None else {}
    if not slug:
        return {"model": "", "will_spend": False, "max_usd": 0.0, "high_usd": 0.0,
                "basis_label": "No writer model: posts come from templates, no API call.",
                "caveat": "", **ctx}
    cfg = _configured_models(models_dir).get(slug)
    max_usd: float | None = None
    high_usd: float | None = None
    ratios: list[dict[str, Any]] = []
    if cfg is not None:
        max_usd = (THREAD_INPUT_TOKENS_MAX * cfg.input_price_per_mtok
                   + THREAD_OUTPUT_TOKENS_MAX * cfg.output_price_per_mtok) / 1_000_000
        high_usd = max_usd
        label = (f"Up to {THREAD_INPUT_TOKENS_MAX:,} input and {THREAD_OUTPUT_TOKENS_MAX:,} "
                 "output tokens at the configured rate card.")
        if store is not None:
            ratios = _ratio_rows(store, [slug])
            ratio = ratios[0]["ratio"]
            if ratio is not None:
                high_usd = max_usd * max(ratio, 1.0)
                label += (f" Scaled by the billed/rate-card ratio {ratio:.2f}x "
                          f"({ratios[0]['source']}, n={ratios[0]['n']}).")
    else:
        label = "This model is not in models/, so its price is unknown."
    if not provider_ready:
        label = "No API key is set for this model's provider: posts come from templates, no API call."
    return {"model": slug, "will_spend": provider_ready, "max_usd": max_usd,
            "high_usd": high_usd, "ratios": ratios,
            "rates": _rate_card(cfg), "basis_label": label,
            "caveat": THREAD_CAVEAT if provider_ready else "", **ctx}


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
        return "not_judged", "dry run: nothing real to judge"
    if read_why:
        return "unreadable", f"judge verdict unknown: {read_why}"
    return "not_judged", "judge wasn't run for this run"


# Sortable Runs columns: query value -> (row key, default direction).
RUN_SORTS: dict[str, tuple[str, str]] = {
    "started": ("started_at", "desc"), "cost": ("billed_cost_usd", "desc"),
    "duration": ("latency_ms", "desc"), "tokens": ("tokens", "desc"),
    "task": ("task_id", "asc"), "status": ("status", "asc"),
}


def _sort_runs(rows: list[dict[str, Any]], sort: str | None, direction: str | None) -> list[dict[str, Any]]:
    """Order rows by a named column. Unknown sort names keep the default (newest
    first); a missing value sorts last in either direction."""
    if sort not in RUN_SORTS:
        return rows
    key, default_dir = RUN_SORTS[sort]
    desc = (direction if direction in ("asc", "desc") else default_dir) == "desc"
    known = [r for r in rows if r.get(key) not in (None, "")]
    unknown = [r for r in rows if r.get(key) in (None, "")]
    known.sort(key=lambda r: r[key], reverse=desc)
    return known + unknown


def runs_payload(
    store: RunStore,
    group: str | None = None,
    task: str | None = None,
    status: str | None = None,
    q: str = "",
    tasks_dir: Path | str | None = None,
    *,
    pairing: str | None = None,
    judge: str | None = None,
    type: str | None = None,
    difficulty: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
    groups_file: Path | str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Run rows for the filterable table, newest first unless `sort` says otherwise.

    `status` accepts a lifecycle status, `passed`/`failed` (verdict filters) or
    `stalled` (a `running` row with no event for STALL_AFTER_S, KTD8; derived,
    never stored). `pairing` is `orchestrator|worker` (the arrow form is a legacy alias). Every row carries
    `stalled`, `type`, `difficulty`, `group_label` and `tokens`. Filtering,
    sorting and paging are repeated client-side for the hosted snapshot
    (`filterRuns` in ui/js/data.js); keep the two in step.
    """
    now = now or _now()
    rows = store.list_runs(run_group=group, task_id=task, limit=None)
    if q:
        rows = filter_runs(rows, q)
    if pairing:
        # canonical `orch|worker`; the matrix key `orch → worker` is a legacy alias
        orch, _, worker = (pairing if "|" in pairing else pairing.replace(" \u2192 ", "|", 1)).partition("|")
        rows = [r for r in rows if r.orchestrator == orch and r.worker == worker]
    abandoned = _abandoned_runs(store) if any(r.status == "running" for r in rows) else set()

    def is_stalled(r: Any) -> bool:
        if r.status != "running" or r.run_id in abandoned:
            return False
        beat = last_heartbeat(r.run_dir, r.started_at) if r.run_dir else _parse_ts(r.started_at)
        idle = max(0.0, (now - beat).total_seconds()) if beat else None
        return liveness_state(idle) == "stalled"

    stalled = {r.run_id for r in rows if is_stalled(r)}
    if status == "passed":
        rows = [r for r in rows if r.status == "finished" and r.passes]
    elif status == "failed":
        rows = [r for r in rows if r.status == "failed" or (r.status == "finished" and not r.passes)]
    elif status == "stalled":
        rows = [r for r in rows if r.run_id in stalled]
    elif status:
        rows = [r for r in rows if r.status == status]
    tmeta = _task_meta(store, tasks_dir)
    if type:
        rows = [r for r in rows if (tmeta.get(r.task_id) or {}).get("type") == type]
    if difficulty:
        rows = [r for r in rows if (tmeta.get(r.task_id) or {}).get("difficulty") == difficulty]
    labels = _groups_meta(groups_file)
    out = []
    for r in rows:
        d = _public_run(r)
        tm = tmeta.get(r.task_id) or {}
        d["task_title"] = tm.get("title") or ""
        d["type"] = tm.get("type") or ""
        d["difficulty"] = tm.get("difficulty") or ""
        d["group_label"] = (labels.get(r.run_group or "") or {}).get("label") or ""
        d["stalled"] = r.run_id in stalled
        d["tokens"] = (r.total_input_tokens or 0) + (r.total_output_tokens or 0)
        d["judge_state"], d["judge_reason"] = judge_state(r)
        if judge and d["judge_state"] != judge:
            continue
        out.append(d)
    return _sort_runs(out, sort, direction)


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

    Zip members come as a listing and never as bodies: the member table is read
    with `zf.infolist()`, which yields names and uncompressed sizes out of the
    central directory, so no member content is decompressed or returned. The
    SPA fetches an artifact body on its own via `/api/run/<id>/artifact`. Same
    rule as the judge and the transcript: bound the read, do not bound the
    response afterwards.
    """
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


def resolve_run_dir(store: Any, meta: Any) -> Path:
    """The directory a run's files live in.

    The index records `run_dir` as it was when the run started, which is relative
    to the working directory for any run launched with a relative runs path. Read
    from another directory, or from a copied or moved runs dir, that path points
    nowhere and every file looks missing. When it does not exist, the recorded
    `orch/task/worker/run_id` tail is rebased under the store's root, the same
    rule `RunStore.backfill_calls` uses."""
    p = Path(meta.run_dir)
    if p.exists():
        return p
    root = getattr(store, "root", None)
    if root:
        cand = Path(root).joinpath(*p.parts[-4:])
        if cand.exists():
            return cand
    return p


_ERROR_TYPE_MARKERS = (".failed", "_error", "worker_error")


def _is_error_event(ev: dict[str, Any]) -> bool:
    typ = str(ev.get("type") or "")
    return bool(ev.get("error")) or typ.endswith(_ERROR_TYPE_MARKERS) or typ in ("error", "run.failed")


def _first_failing_check(report: Any) -> str | None:
    checks = report.get("checks") if isinstance(report, dict) else None
    if isinstance(checks, dict):
        for name, ok in checks.items():
            if ok is False:
                return str(name)
    return None


def _failure_summary(meta: Any, events: list[dict[str, Any]], report: Any,
                     report_exists: bool) -> dict[str, Any] | None:
    """Why a failed run failed, from what the run dir holds: the taxonomy reason,
    the first failing check, and the event to read first (the first error event, or
    the last event when none carries an error). With no events at all the run died
    before it started recording, and the summary says so."""
    if meta.status != "failed":
        return None
    idx: int | None = next((i for i, ev in enumerate(events) if _is_error_event(ev)), None)
    kind = "error"
    if idx is None and events:
        idx, kind = len(events) - 1, "last"
    event: dict[str, Any] | None = None
    if idx is not None:
        ev = events[idx]
        event = {"index": idx, "kind": kind, "type": str(ev.get("type") or "?"),
                 "phase": str(ev.get("phase") or ""),
                 "summary": event_row(ev)[3] or str(ev.get("error") or "")[:160]}
    errors = report.get("errors") if isinstance(report, dict) else None
    return {
        "reason": meta.failure_reason or "",
        "failing_check": _first_failing_check(report),
        "errors": [str(e)[:300] for e in errors[:3]] if isinstance(errors, list) else [],
        "event": event,
        "no_detail": not events,
        "report_available": report_exists,
    }


def _lane_for(ev: dict[str, Any], worker: str | None) -> tuple[str, str] | None:
    phase, role = ev.get("phase"), ev.get("role")
    if role == "judge" or phase == "judge":
        return "judge", "judge"
    if phase == "assemble":
        return "assemble", "assemble"
    if phase == "validate":
        return "validate", "validate"
    if phase == "plan":
        return "orchestrator", "orchestrator"
    if phase == "delegate":
        wid = ev.get("worker_id") or worker or "worker"
        return str(wid), str(wid)
    return None


_LANE_ORDER = ("orchestrator", "worker", "assemble", "validate", "judge")


def lanes_payload(events: list[dict[str, Any]], running: bool = False) -> dict[str, Any]:
    """Lane timeline data (DESIGN 6.9 #7): one lane per orchestrator, worker, assemble,
    validate and judge, one bar per call with its start offset, length (latency), cost
    and a verdict tick. `event` is the index of the call's event in the stream."""
    stamps = [t for t in (_parse_ts(e.get("timestamp")) for e in events) if t]
    if not stamps:
        return {"span_ms": 0, "lanes": [], "live": None}
    t0, t1 = min(stamps), max(stamps)
    lanes: dict[str, dict[str, Any]] = {}
    worker: str | None = None
    for i, ev in enumerate(events):
        typ = ev.get("type")
        if typ == "worker.started":
            worker = ev.get("worker_id") or worker
        lane = _lane_for(ev, worker)
        timed = typ in ("llm_call", "worker_error") or (ev.get("latency_ms") or 0) > 0
        end = _parse_ts(ev.get("timestamp"))
        if lane is None or not timed or end is None:
            continue
        dur = float(ev.get("latency_ms") or 0.0)
        start = max(0.0, (end - t0).total_seconds() * 1000 - dur)
        bucket = lanes.setdefault(lane[0], {"id": lane[0], "label": lane[1], "bars": []})
        cost = ev.get("cost") or {}
        bucket["bars"].append({
            "start_ms": start, "dur_ms": dur, "event": i, "type": str(typ),
            "verdict": "fail" if _is_error_event(ev) else "ok",
            "cost_usd": cost.get("api_cost_usd") if cost.get("api_cost_usd") is not None else cost.get("usd"),
            "model": str(ev.get("model") or ""),
        })

    def order(item: dict[str, Any]) -> tuple[int, str]:
        k = item["id"]
        return (_LANE_ORDER.index(k) if k in _LANE_ORDER else 1, k)
    ordered = sorted(lanes.values(), key=order)
    live = None
    if running and events:
        live = (_lane_for(events[-1], worker) or (None, None))[0]
    return {"span_ms": max((t1 - t0).total_seconds() * 1000, 1.0), "lanes": ordered, "live": live}


def run_liveness(store: RunStore, registry: JobRegistry | None, meta: Any,
                 now: datetime | None = None) -> dict[str, Any]:
    """live, stalled, abandoned or done for one run, plus which action applies.
    Cancel only for runs this server owns; abandon only for stalled unowned ones."""
    if meta.status != "running":
        return {"state": "done", "owned": False, "cancellable": False, "abandonable": False,
                "idle_s": None}
    if meta.run_id in _abandoned_runs(store):
        return {"state": "abandoned", "owned": False, "cancellable": False, "abandonable": False,
                "idle_s": None}
    now = now or _now()
    beat = last_heartbeat(resolve_run_dir(store, meta), meta.started_at)
    idle = max(0.0, (now - beat).total_seconds()) if beat else None
    owned = bool(registry and registry.owns(meta.run_id))
    st = liveness_state(idle)
    return {"state": st, "owned": owned, "cancellable": owned,
            "abandonable": st == "stalled" and not owned,
            "idle_s": int(idle) if idle is not None else None}


_SECTION_TABS = ("artifact", "events", "calls", "report", "review", "plan", "manifest")
_HOLDOUT_REASON = ("This run belongs to the holdout arm, so its task text, answer key and outputs "
                   "are not published. Open it on the machine that ran it.")


def _section(state_: str, reason: str = "", count: int | None = None) -> dict[str, Any]:
    return {"state": state_, "reason": reason, "count": count}


def _file_section(path: Path, running: bool, parsed: Any, what: str,
                  dry: bool = False, stopped: bool = False) -> dict[str, Any]:
    if parsed is not None:
        return _section("ok")
    if path.exists():
        return _section("missing", f"{what} could not be read: it is unreadable or truncated.")
    if stopped:
        return _section("missing", f"This run stopped reporting before it wrote {what.lower()}.")
    if running:
        return _section("not_yet", f"The run is still working. {what} is written when it finishes.")
    if dry:
        return _section("empty_by_design", f"A dry run does not write {what.lower()}.")
    return _section("missing", f"This run did not write {what.lower()}.")


def run_detail_payload(
    store: RunStore,
    run_id: str,
    tasks_dir: Path | str | None = None,
    groups_file: Path | str | None = None,
    *,
    registry: JobRegistry | None = None,
    hosted: bool = False,
    raw_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Everything the run detail view needs in one fetch.

    Beyond the stored files it derives, so the view never guesses: `failure` (the
    summary for a failed run), `sections` (one evidence state per tab: ok, not_yet,
    empty_by_design, missing or withheld), `lanes` (timeline bars), `plan_json`
    (the subtask list) and `liveness` (state and which action applies). `hosted`
    marks the public mirror: holdout runs are withheld whole, `plan.md` (found in
    `raw_dir`, the unscrubbed run dir) and zip artifacts are withheld."""
    meta = store.get_run(run_id)
    if meta is None:
        return None
    run_dir = resolve_run_dir(store, meta)
    tm = _task_meta(store, tasks_dir).get(meta.task_id) or {}
    gm = _groups_meta(groups_file).get(meta.run_group or "") or {}
    jstate, jreason = judge_state(meta)
    holdout = bool((meta.config or {}).get("holdout")) or run_is_holdout(run_dir)
    out: dict[str, Any] = {
        "meta": _public_run(meta),
        "judge_state": jstate,
        "judge_reason": jreason,
        "task_title": tm.get("title") or "",
        "task_blurb": tm.get("blurb") or "",
        "group_label": gm.get("label") or "",
        "group_description": gm.get("description") or "",
        "holdout": holdout,
        "liveness": run_liveness(store, registry, meta, now),
    }
    if hosted and holdout:
        out.update(
            calls=[], report=None, review=None, manifest=None, plan=None, plan_json=None,
            timeline=[], artifact=None, failure=None,
            lanes={"span_ms": 0, "lanes": [], "live": None},
            sections={t: _section("withheld", _HOLDOUT_REASON) for t in _SECTION_TABS})
        return out

    # `running` means the run is live and may still write these; a run that is stalled or
    # abandoned is not "not yet", it has stopped.
    running = meta.status == "running" and out["liveness"]["state"] == "live"
    stopped = meta.status == "running" and not running
    dry = bool(meta.dry_run)
    events, _ = tail_events(run_dir / "events.jsonl", 0)
    calls = store.call_previews(run_id)
    report = read_json(run_dir / "report.json")
    review = read_json(run_dir / "review.json")
    manifest = read_json(run_dir / "manifest.json")
    plan_path = run_dir / "plan.md"
    plan = plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.exists() else None
    plan_json = read_json(run_dir / "plan.json")
    if not isinstance(plan_json, dict):
        plan_json = None
    artifact = artifact_info(run_dir)
    if artifact:
        # the run-relative path (orch/task/worker/run_id/name), never an absolute one
        artifact["path"] = "/".join([*Path(meta.run_dir).parts[-4:], artifact["name"]])
    out.update(
        calls=calls, report=report, review=review, manifest=manifest, plan=plan, plan_json=plan_json,
        timeline=timeline_payload(run_dir), artifact=artifact,
        lanes=lanes_payload(events, running),
        failure=_failure_summary(meta, events, report, (run_dir / "report.json").exists()))

    sections: dict[str, dict[str, Any]] = {}
    if artifact:
        sections["artifact"] = _section("ok")
    elif stopped:
        sections["artifact"] = _section("missing", "This run stopped reporting before it stored an artifact.")
    elif running:
        sections["artifact"] = _section("not_yet", "The run is still working. The artifact is stored when it finishes.")
    elif dry:
        sections["artifact"] = _section("empty_by_design", "A dry run calls no model, so it stores no artifact.")
    else:
        why = meta.failure_reason or jreason or ""
        sections["artifact"] = _section(
            "missing", "This run did not store an artifact." + (f" Reason on record: {why}." if why else ""))
    if events:
        sections["events"] = _section("ok", count=len(events))
    elif running:
        sections["events"] = _section("not_yet", "No event has been written yet.")
    elif stopped:
        sections["events"] = _section("missing", "This run never wrote an event.", 0)
    else:
        sections["events"] = _section("missing", "This run recorded no events.", 0)
    if calls:
        sections["calls"] = _section("ok", count=len(calls))
    elif running or stopped:
        sections["calls"] = (_section("not_yet", "No model call has finished yet.", 0) if running
                             else _section("missing", "This run stopped reporting before a call was recorded.", 0))
    elif dry:
        sections["calls"] = _section(
            "empty_by_design", "This was a dry run: no model was called, so there are no calls to list.", 0)
    else:
        sections["calls"] = _section("missing", "No calls were recorded for this run.", 0)
    sections["report"] = _file_section(run_dir / "report.json", running, report, "A report", stopped=stopped)
    sections["review"] = _file_section(run_dir / "review.json", running, review, "A review", stopped=stopped)
    sections["manifest"] = _file_section(run_dir / "manifest.json", running, manifest, "A manifest",
                                         stopped=stopped)
    if plan_json is not None or plan is not None:
        subtasks = plan_json.get("subtasks") if plan_json else None
        sections["plan"] = _section("ok", count=len(subtasks) if isinstance(subtasks, list) else None)
    else:
        sections["plan"] = _file_section(run_dir / "plan.json", running, None, "A plan", dry, stopped)
    if hosted:
        if plan_path.exists() or (raw_dir is not None and (Path(raw_dir) / "plan.md").exists()):
            out["plan"] = None
            sections["plan"] = _section("withheld", "The plan carries the task prompt, so it is not published on the hosted copy.")
        if artifact and artifact.get("ext") == "zip":
            sections["artifact"] = _section(
                "withheld", "Archive contents are not published on the hosted copy. The member list is shown.")
    out["sections"] = sections
    return out


_ARMS = ("baseline", "jev")


def auto_group_label(group: str) -> str:
    """Human label for an experiment driver's group key
    (`matrix:task:orchestrator:worker:arm`) as `experiment · task · orch / worker · arm`.
    Any other group has no auto-label (empty string)."""
    parts = group.split(":")
    if len(parts) != 5 or parts[4] not in _ARMS:
        return ""
    matrix, task, orch, worker, arm = parts
    return f"{matrix} · {task} · {orch.split('/')[-1]} / {worker.split('/')[-1]} · {arm}"


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
        g["cost_usd"] += r.display_cost_usd or 0.0
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
            "display_label": gm.get("label") or auto_group_label(g["group"]) or g["group"],
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
            "difficulty": tm.get("difficulty") or "",
            "archetype": tm.get("archetype") or "",
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
            md = spec.metadata or {}
            out[spec.id] = {
                "type": spec.type, "title": spec.title, "blurb": spec.blurb,
                "difficulty": str(md.get("difficulty") or ""),
                "archetype": str(md.get("archetype") or ""),
            }
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
        "No pairing has three finished runs yet",
    )

    high_spend = [row for row in measured if row.get("cost_total", 0) >= statistics.median(
        [float(r.get("cost_total") or 0) for r in measured]
    )] if measured else []
    result(
        CARD_LENSES[1], high_spend,
        "{orchestrator} → {worker} is strongest in the upper measured-spend half.",
        "No metered pairing has enough finished evidence",
    )

    low_spend = [row for row in eligible if row.get("cost_per_pass") is not None and
                 float(row["cost_per_pass"]) <= statistics.median(cost_per_pass)] if cost_per_pass else []
    result(
        CARD_LENSES[2], low_spend,
        "{orchestrator} → {worker} is strongest in the lower measured-cost half.",
        "No measured cost per pass is available",
    )

    best_pass = max((float(row.get("pass_rate") or 0) for row in eligible), default=0.0)
    sweet = [row for row in eligible if row.get("cost_per_pass") is not None and
             float(row.get("pass_rate") or 0) >= best_pass - 0.10]
    result(
        CARD_LENSES[3], sweet,
        "{orchestrator} → {worker} is within 10 points of the pass leader at the lowest measured cost.",
        "No metered pairing is within 10 points of the leading pass rate",
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
        "No pairing has a judged semantic axis to compare",
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
    default_group = None if group else default_pairing_group(metas)

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
            "low_n_best": is_low_n_best(r.finished),
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
                "finished": (c["finished"] if c else 0),
                "score_mean": (c["score_mean"] if c else None),
                "low_sample": (c["low_sample"] if c else False),
            }
            for o in orchs for w in workers
            for c in [by_key.get((o, w))]
        ],
    }
    summary = {
        "pairings": len(enriched),
        "metered": sum(1 for d in enriched if (d.get("cost_total") or 0) > 0),
        "unmetered": sum(1 for d in enriched if not (d.get("cost_total") or 0) > 0),
        "best_eligible": sum(1 for d in enriched if not d["low_n_best"]),
        "no_pass": sum(1 for d in enriched if d.get("finished") and not d.get("passed")),
    }
    return {"rows": enriched, "matrix": matrix, "lenses": _lens_payloads(enriched),
            "summary": summary, "default_group": default_group}


def default_pairing_group(metas: list[Any]) -> str:
    """The run group Pairings opens on: the most recent labelled group holding
    at least three pairings, else ``""`` (all groups). A one-pairing cohort is
    never the default story."""
    pairs: dict[str, set[tuple[str, str]]] = {}
    latest: dict[str, str] = {}
    for m in metas:
        group = getattr(m, "run_group", "") or ""
        if not group:
            continue
        pairs.setdefault(group, set()).add((m.orchestrator, m.worker))
        started = getattr(m, "started_at", "") or ""
        if started > latest.get(group, ""):
            latest[group] = started
    big = [g for g, p in pairs.items() if len(p) >= 3]
    return max(big, key=lambda g: (latest.get(g, ""), g)) if big else ""


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
    text = " · ".join(bits) or "mid-pack on every axis"
    return text[0].upper() + text[1:]


def _wilson(passes: int, n: int) -> list[float] | None:
    """Wilson 95% interval on a binomial pass rate — the honest uncertainty
    a share card owes its audience when n is small."""
    ci = wilson_interval(passes, n)
    return [round(ci[0], 3), round(ci[1], 3)] if ci else None


def _verdict_line(mech_pass: bool | None, judge_passed: bool | None,
                  status: str | None) -> str:
    """One-line verdict in plain words — the card's subtitle hook."""
    if status != "finished":
        return f"run {status or 'unknown'}: no verdict yet"
    if judge_passed is None:
        return ("mechanical pass, unjudged" if mech_pass
                else "mechanical fail, unjudged")
    if mech_pass and judge_passed:
        return "passes both axes: structure and semantics"
    if mech_pass:
        return "well-formed but semantically rejected"
    if judge_passed:
        return "mechanical reject, semantic rescue: inspect"
    return "rejected on both axes"


def _explainer(kind: str, card: dict[str, Any]) -> str:
    """What the card measures, for a mild-AI-knowledge audience — one
    sentence, no jargon."""
    if kind == "group":
        return (
            "Each run: a planner model breaks a real task into steps, worker models "
            "execute them in parallel, and the final result is graded two "
            "ways: automated checks that actually run and verify the output, "
            "plus a second model that reviews whether it's genuinely good."
        )
    if kind == "pairing":
        return (
            f"One pairing: {str(card.get('orchestrator','?')).split('/')[-1]} plans the work, "
            f"{str(card.get('worker','?')).split('/')[-1]} executes it. Every run is graded by "
            "automated checks and an independent judge model."
        )
    return (
        "One eval run: a planner model broke the task into steps, a worker model "
        "executed them, and the result was graded by automated checks plus "
        "a judge model."
    )


def _eval_description(d: dict[str, Any], kind: str) -> str:
    """Pre-made 'how the eval set did' line — composed deterministically
    from the card's real numbers, so a card never needs a model call to
    carry a one-sentence summary."""
    def pct(x: float | None) -> str:
        return fmt_percent(x)
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
            bits.append(f"{fmt_money(cost)} total")
        pr, jr = d.get("pass_rate"), d.get("judge_pass_rate")
        note = ""
        if pr is not None and jr is not None and pr - jr > 0.15:
            note = ": the judge is stricter than the checks"
        elif jr is not None and pr is not None and jr - pr > 0.05:
            note = ": the judge rescues runs the checks reject"
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
            bits.append(f"{fmt_money(cost)} total")
        tail = ""
        best, worst = d.get("best_type"), d.get("worst_type")
        if best and worst and best != worst:
            tail = f": strongest on {best}, weakest on {worst}"
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


def _renderable_member(names: list[str]) -> str | None:
    """Pick the member an iframe should render: index.html first (shallowest
    wins), then any HTML file. Returns None for non-HTML archives."""
    normalized = {n.replace("\\", "/"): n for n in names}
    html = [
        orig for norm, orig in normalized.items()
        if norm and not norm.startswith("/") and ".." not in Path(norm).parts
        and not norm.endswith("/") and "__MACOSX" not in norm
        and norm.lower().endswith((".html", ".htm"))
    ]
    if not html:
        return None
    index = [n for n in html if Path(n).name.lower() == "index.html"]
    pool = index or html
    return min(pool, key=lambda n: (n.count("/"), n))


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
                render_member = _renderable_member(
                    [i.filename for i in infos])
        except (OSError, zipfile.BadZipFile):
            member_info = None
            member = None
            render_member = None
        if not member or member_info is None or member_info.is_dir():
            return None
        ext = Path(member).suffix.lstrip(".").lower()
        ref: dict[str, Any] = {
            "name": member,
            "ext": ext,
            "kind": _media_kind(ext),
            "media_type": "archive",
            "bytes": max(0, int(member_info.file_size)),
            "url": f"/api/run/{quote(run_id, safe='')}/artifact/{quote(member, safe='')}",
        }
        if render_member:
            ref["render_url"] = (
                f"/api/run/{quote(run_id, safe='')}/artifact/"
                f"{quote(render_member, safe='')}"
            )
            ref["render_name"] = render_member
        return ref
    ext = artifact.suffix.lstrip(".").lower()
    try:
        size = artifact.stat().st_size
    except OSError:
        size = 0
    ref = {
        "name": artifact.name,
        "ext": ext,
        "kind": _media_kind(ext),
        "media_type": _media_kind(ext),
        "bytes": size,
        "url": f"/api/run/{quote(run_id, safe='')}/artifact",
    }
    if ext in {"html", "htm"}:
        ref["render_url"] = ref["url"]
        ref["render_name"] = artifact.name
    return ref


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
    """Render a short terminal-like view from lifecycle events, never raw prompts.

    "Never raw prompts" is enforced in two places, and both have to hold for
    the promise to mean anything. Here: the wanted event types are fixed, each
    rendered field is read off `output` and sliced to 60 chars, and an
    absolute harness path is rewritten to `[run]`. In the call ledger, the
    other place prompts could reach a browser: `RunStore.call_previews` is
    what `/api/run/<id>` reads, and it caps each body at
    `CALL_PREVIEW_MAX_BYTES` inside the SQL query. If you add a prompt source
    to a payload, cap it there too — a guarantee that lives only in a
    docstring is not one.
    """
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
    transcript_label = "Test transcript" if transcript_text else "Event transcript"
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
                "label": "Axis divergence",
                "tone": "info",
                "claim": "The mechanical gate and judge disagree on this run.",
                "evidence": {"mechanical": bool(payload.get("passes")), "judge": bool(payload.get("judge_passed"))},
            })
        if payload.get("passes") and (payload.get("judge_score") or 0) >= 0.8:
            signals.append({
                "id": "high_quality",
                "label": "High quality",
                "tone": "pass",
                "claim": "The run passed its mechanical gate with a strong judge score.",
                "evidence": {"judge_score": payload.get("judge_score")},
            })
        if payload.get("judge_state") == "unreadable":
            signals.append({
                "id": "judge_axis_unknown",
                "label": "Judge axis unknown",
                "tone": "warn",
                "claim": ("The judge verdict could not be read, so whether the judge ran "
                          "is unknown, not absent."),
                "evidence": {"judge_state": "unreadable"},
            })
        elif payload.get("judge_state") != "judged":
            signals.append({
                "id": "weak_confidence",
                "label": "Weak confidence",
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
            "label": "Axis divergence",
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
                "label": "Task specialist",
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
                "label": "Cost frontier",
                "tone": "pass",
                "claim": f"The selected setup is at {fmt_money(selected_cost)} per successful finish.",
                "evidence": {"cost_per_pass": selected_cost, "pass_rate": selected_pass},
            })

    if pr is not None and int(payload.get("finished") or 0) >= 3 and float(pr) >= 0.80:
        signals.append({
            "id": "high_quality",
            "label": "High quality",
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
            "label": "Weak confidence",
            "tone": "warn",
            "claim": f"Treat this as directional evidence: {reason}.",
            "evidence": {"finished": payload.get("finished"), "ci": payload.get("pass_ci"),
                         "judge_reports_unreadable": unreadable},
        })
    return signals


def _story_caption(payload: dict[str, Any], claim: str) -> str:
    if payload.get("kind") == "run":
        context = f"Status {payload.get('status') or 'unknown'} · {fmt_money(float(payload.get('cost_usd') or 0))} · {fmt_duration_ms(payload.get('latency_ms') or 0)}"
    else:
        ci = payload.get("pass_ci")
        context = f"{payload.get('finished', 0)}/{payload.get('runs', 0)} finished"
        if ci:
            context += f" · 95% CI {fmt_range_pct(ci[0], ci[1])}"
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
        verdict = "PASS" if payload.get("passes") else "FAIL" if payload.get("passes") is False else NULL_GLYPH
        judge_value = (
            fmt_score(float(payload["judge_noul"])) if payload.get("judge_noul") is not None
            else fmt_score(float(payload["judge_score"])) if payload.get("judge_score") is not None
            else NULL_GLYPH
        )
        return [
            {"id": "mechanical", "label": "Mechanical", "value": verdict, "detail": payload.get("failure_reason") or "Execution gate", "tone": "mech"},
            {"id": "judge", "label": "Judge", "value": judge_value, "detail": payload.get("judge_state") or "Not judged", "tone": "judge"},
            {"id": "cost", "label": "Cost", "value": fmt_money(float(payload.get('cost_usd') or 0)), "detail": fmt_duration_ms(payload.get('latency_ms') or 0), "tone": "cost"},
        ]
    judged = int(payload.get("judged") or 0)
    judge_value = f"{payload.get('judge_approved', 0)}/{judged}" if judged else NULL_GLYPH
    return [
        {
            "id": "mechanical",
            "label": "Mechanical pass",
            "value": f"{payload.get('passed', 0)}/{payload.get('finished', 0)}",
            "detail": f"{fmt_percent(payload.get('pass_rate'))} observed",
            "tone": "mech",
        },
        {
            "id": "judge",
            "label": "Judge approved",
            "value": judge_value,
            "detail": f"{payload.get('judged', 0)} judged" if judged else "no judge evidence",
            "tone": "judge",
        },
        {
            "id": "cost",
            "label": "Metered spend",
            "value": fmt_money(float(payload.get('cost_usd') or 0)),
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
        costs = [m.display_cost_usd for m in finished]
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
            # would assert the judge never ran, a claim the data cannot make.
            line = (f"judge verdict unknown for {unreadable_n} of {len(finished)} "
                    "finished run(s): report.json could not be read")
        else:
            if judged_n and pr is not None:
                jp = judge_passed_n / judged_n
                if pr - jp > 0.15:
                    line = f"{round(pr * 100)}% pass structure, {round(jp * 100)}% survive semantic review"
                elif jp - pr > 0.05:
                    line = f"{round(pr * 100)}% clear the full gate, judge alone approves {round(jp * 100)}%"
                else:
                    line = "mechanical and judge axes agree"
            elif judged_n:
                line = f"{judged_n} runs judged, semantic axis active"
            else:
                line = "mechanical grading only, nothing judged yet"
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
            pair_cost[key] = pair_cost.get(key, 0.0) + (m.display_cost_usd or 0.0)
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
            # would assert the judge never ran, a claim the data cannot make.
            line = (f"judge verdict unknown for {unreadable_n} of {g['finished']} "
                    "finished run(s): report.json could not be read")
        else:
            if judged_n and jp_rate is not None and g["pass_rate"] is not None:
                mech_pct, jp_pct = round(g["pass_rate"] * 100), round(jp_rate * 100)
                if g["pass_rate"] - jp_rate > 0.15:
                    line = f"{mech_pct}% pass structure, {jp_pct}% survive semantic review"
                elif jp_rate - g["pass_rate"] > 0.05:
                    line = f"{mech_pct}% clear the full gate, judge alone approves {jp_pct}%"
                else:
                    line = "mechanical and judge axes agree"
            elif judged_n:
                line = f"{judged_n} runs judged, semantic axis active"
            else:
                line = "mechanical grading only, nothing judged yet"
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
                "cost_usd": sum(r.display_cost_usd or 0 for r in rs),
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
            "cost_usd": meta.display_cost_usd, "cost_basis": meta.cost_basis,
            "latency_ms": meta.latency_ms,
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
    """Cell-by-cell group delta, baseline (a) first against candidate (b).

    A cell is improved or regressed only when the two 95% Wilson intervals do
    not overlap and both sides have at least LOW_N_CELL finished runs;
    otherwise it is "no-clear-difference" (the point delta is still reported).

    Each cell carries both Wilson intervals, the pass-rate delta and the cost
    delta. Rows sort by regression (largest drop first), one-sided cells last.
    ``a == b`` is blocked with a message instead of a vacuous all-stable table."""
    if group_a == group_b:
        return {"group_a": group_a, "group_b": group_b, "cells": [], "verdicts": {},
                "shared": 0, "one_sided": 0, "cost_a": 0, "cost_b": 0, "cost_delta": None,
                "blocked": "Pick two different run groups to compare."}

    def cells(group: str) -> dict[tuple[str, str, str], Any]:
        return {
            (c.task_id, c.orchestrator, c.worker): c
            for c in aggregate(_runs_for_group(store, group) if group else [])
        }

    cells_a, cells_b = cells(group_a), cells(group_b)
    keys = sorted(set(cells_a) | set(cells_b))
    rows: list[dict[str, Any]] = []
    for task_id, orch, worker in keys:
        a, b = cells_a.get((task_id, orch, worker)), cells_b.get((task_id, orch, worker))
        pa = a.pass_rate if a else None
        pb = b.pass_rate if b else None
        side = ""
        ci_a = _wilson(a.passed, a.finished) if a else None
        ci_b = _wilson(b.passed, b.finished) if b else None
        low_n = bool(a and b and (is_low_n_cell(a.finished) or is_low_n_cell(b.finished)))
        if pa is None or pb is None:
            verdict = "one-sided"
            side = "baseline" if pa is not None else "candidate" if pb is not None else "neither"
        elif low_n or ci_a is None or ci_b is None:
            verdict = "no-clear-difference"
        elif ci_a[1] < ci_b[0]:
            verdict = "improved"
        elif ci_b[1] < ci_a[0]:
            verdict = "regressed"
        else:
            verdict = "no-clear-difference"
        cost_a = a.cost_total if a else None
        cost_b = b.cost_total if b else None
        two_sided = verdict != "one-sided"
        rows.append({
            "task_id": task_id, "orchestrator": orch, "worker": worker,
            "n_a": a.runs if a else 0, "pass_a": pa, "cost_a": cost_a,
            "n_b": b.runs if b else 0, "pass_b": pb, "cost_b": cost_b,
            "passed_a": a.passed if a else 0, "finished_a": a.finished if a else 0,
            "passed_b": b.passed if b else 0, "finished_b": b.finished if b else 0,
            "ci_a": ci_a, "ci_b": ci_b, "low_n": low_n,
            "delta": (pb - pa) if two_sided and pa is not None and pb is not None else None,
            "cost_delta": (cost_b or 0) - (cost_a or 0) if two_sided else None,
            "side": side,
            "verdict": verdict,
            "failures_a": a.failures if a else {},
            "failures_b": b.failures if b else {},
        })
    # regressions (largest drop first), improvements (largest gain first),
    # no clear difference (largest point change first), one-sided last
    rank = {"regressed": 0, "improved": 1, "no-clear-difference": 2, "one-sided": 3}
    rows.sort(key=lambda r: (rank[r["verdict"]],
                             {"regressed": r["delta"], "improved": -(r["delta"] or 0),
                              "no-clear-difference": -abs(r["delta"] or 0)}.get(r["verdict"], 0.0),
                             r["task_id"], r["orchestrator"], r["worker"]))
    verdicts: dict[str, int] = {}
    for r in rows:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    cost_a_total = sum(r["cost_a"] or 0 for r in rows)
    cost_b_total = sum(r["cost_b"] or 0 for r in rows)
    one_sided = verdicts.get("one-sided", 0)
    return {
        "group_a": group_a, "group_b": group_b, "cells": rows,
        "verdicts": verdicts,
        "shared": len(rows) - one_sided,
        "one_sided": one_sided,
        "cost_a": cost_a_total,
        "cost_b": cost_b_total,
        "cost_delta": cost_b_total - cost_a_total,
        "blocked": "",
    }


def _jev_interventions(metas: list[Any]) -> dict[str, int]:
    """Count jev plan-gate/output-gate interventions across runs — these
    live in events.jsonl, not the index. Bounded walk, experiment scale."""
    counts = {"replan": 0, "rework": 0}
    for meta in metas:
        run_dir = getattr(meta, "run_dir", "")
        if not run_dir:
            continue
        events, _ = tail_events(Path(run_dir) / "events.jsonl", 0)
        for ev in events:
            et = ev.get("event_type") or ev.get("type") or ""
            if et == "jev_replan":
                counts["replan"] += 1
            elif et == "jev_rework":
                counts["rework"] += 1
    return counts


def _arm_block(runs: list[Any]) -> dict[str, Any]:
    """Per-arm BI block for one experiment cell."""
    from orchestral.experiment import arm_stats
    passes, n, errors = arm_stats(runs)
    ci = _wilson(passes, n)
    cost = sum(r.display_cost_usd or 0.0 for r in runs)
    return {
        "passes": passes,
        "n": n,
        "rate": passes / n if n else None,
        "ci": ci,
        "errors": errors,
        "cost": round(cost, 6),
        "cost_per_pass": round(cost / passes, 6) if passes else None,
        "delegated": sum(1 for r in runs if r.delegated),
    }


def _matrix_budget(path: Path) -> dict[str, Any]:
    """The driver takes --budget on the command line and does not record it, so
    a spec only has one when its author wrote a `budget:` key. Unknown stays
    unknown; the UI says so rather than inventing a ceiling."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")).get("budget")
    except Exception:
        raw = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return {"usd": float(raw), "recorded": True}
    return {"usd": None, "recorded": False}


def experiments_list(store: RunStore, experiments_dir: Path | str,
                     tasks_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """One summary row per matrix spec under `experiments/`, name-sorted."""
    root = Path(experiments_dir)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for spec in sorted(root.glob("*.yaml")):
        try:
            payload = experiment_payload(store, spec, tasks_dir=tasks_dir)
        except Exception:
            continue
        if payload is None:
            continue
        summary = payload["summary"]
        out.append({"name": spec.stem, "matrix": payload["matrix"], "cells": summary["cells"],
                    "states": summary["states"], "posted": summary["posted"],
                    "spend": summary["spend"], "budget": payload["budget"]})
    return out


def experiment_payload(
    store: RunStore, matrix_path: str | Path, *,
    diff_eps: float = 0.15, tasks_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """A/B experiment payload — per-cell arm comparison, honestly framed.

    Mechanical pass is the primary axis. The jev arm is both assisted and
    scored by the same decisions engine, so judge-score deltas between
    arms are self-referential; the payload says so wherever it reports.
    Each cell also carries the task's difficulty band and archetype so the
    board can facet "which pairings survive the expert band".
    """
    from orchestral.coverage import coverage_rows, coverage_summary
    from orchestral.experiment import Cell, cell_runs, load_matrix

    p = Path(matrix_path)
    if not p.exists():
        return None
    matrix = load_matrix(p)
    tmeta = _task_meta(store, tasks_dir or "tasks")
    rows = coverage_rows(store, matrix, diff_eps=diff_eps)
    cells: list[dict[str, Any]] = []
    for row in rows:
        cell = Cell(task_id=row.task_id, orchestrator=row.orchestrator, worker=row.worker)
        arms = cell_runs(store, matrix.name, cell)
        tm = tmeta.get(row.task_id) or {}
        cells.append({
            **row.to_dict(),
            "difficulty": tm.get("difficulty") or "",
            "archetype": tm.get("archetype") or "",
            "baseline": _arm_block(arms["baseline"]),
            "jev": {**_arm_block(arms["jev"]),
                    "interventions": _jev_interventions(arms["jev"])},
        })
    return {
        "matrix": matrix.name,
        "budget": _matrix_budget(p),
        "summary": coverage_summary(rows),
        "cells": cells,
        "primary_axis": "mechanical pass",
        "caveats": [
            "judge-score deltas are self-referential: the decisions engine "
            "assists the jev arm and scores both arms",
            "arms are unpaired statistically, since no seed reaches chat "
            "providers; pairing is spec + replicate-index + interleave",
            "difference intervals at 95% will miss on roughly 1-in-20 cells",
        ],
    }
