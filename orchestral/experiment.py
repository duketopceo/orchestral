"""Batched A/B experiment driver — baseline vs jev-assist arms.

One matrix YAML expands to ``(task, orchestrator, worker)`` cells. Each
replicate index launches two runs — the same spec under ``jev_assist``
off and on — with arm order alternating per index so provider drift lands
evenly. Reps run in batches; between batches the driver checks the live
``calls``-table spend meter, the batch's infra-error rate, and the
arm-difference confidence interval, then either continues, early-stops a
resolved cell, persists an ``aborted`` state, or stops the experiment on
budget.

Rep targets are priced per cell: ``ceil(cell_budget / est_pair_cost)``
clamped to [FLOOR, CAP]. The estimate is the cell's own billed history
(baseline arm; the jev arm prices at ``JEV_LOAD_FACTOR`` until it has its
own history). A cell with no history runs one calibration replicate and
reprices from what actually billed. The driver never sys.exits — composed
callers get a summary dict back.

The launcher is injected so tests drive the loop without a provider, and
the CLI supplies the real Runner path.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from orchestral.agentexec import DEFAULT_TIMEOUT_SECONDS
from orchestral.config import TaskSpec, find_task, load_task
from orchestral.stats import diff_ci
from orchestral.storage import RunMeta, RunStore

ARMS = ("baseline", "jev")

# Jev-side calls (decide, one replan, possibly one rework worker call) are
# priced as a multiplier on the baseline arm until the cell has jev-arm
# billing history of its own.
JEV_LOAD_FACTOR = 1.3

# A cell's evidence floor (pairs) and the cap that keeps a cheap cell from
# running forever. The floor exists for evidence, not cost — a pair that
# costs more than the cell's budget share still runs FLOOR reps, which is
# why --budget alone is not a spend bound (the live-spend abort is).
REP_FLOOR = 5
REP_CAP = 100

# Task types whose validation executes worker code — they need the
# isolated runtime contract or every run lands a "no runtime" verdict.
ISOLATED_TASK_TYPES = frozenset({"code", "bugfix"})

# Slot-filling statuses: a run that reached any terminal state completed
# its replicate slot (infra-error outcomes stay out of pass denominators
# but still count as delivered work). "running" is the only live status.
TERMINAL_STATUSES = frozenset({"finished", "failed", "cancelled"})

# A "running" row is a corpse only after its longest legitimate silence:
# one executor attempt at the recorded timeout plus a margin for the
# call/judge tail — a window shorter than an in-flight attempt marks a
# live run stale and relaunches a duplicate into its slot. The floor
# covers rows that predate the timeout_seconds config field.
_STALE_MARGIN_SECONDS = 600.0
STALE_RUNNING_SECONDS = DEFAULT_TIMEOUT_SECONDS + _STALE_MARGIN_SECONDS

CellLauncher = Callable[["Cell", str, int, str, int | None], RunMeta]


@dataclass(frozen=True)
class Matrix:
    name: str
    orchestrators: list[str]
    workers: list[str]
    tasks: list[str]

    @property
    def cells(self) -> list[Cell]:
        return [
            Cell(task_id=t, orchestrator=o, worker=w)
            for t in self.tasks
            for o in self.orchestrators
            for w in self.workers
        ]


@dataclass(frozen=True)
class Cell:
    task_id: str
    orchestrator: str
    worker: str

    @property
    def key(self) -> str:
        return f"{self.task_id}:{self.orchestrator}:{self.worker}"

    def group(self, matrix_name: str, arm: str) -> str:
        """Run-group label — indexed, human-diffable, arm-suffixed."""
        return f"{matrix_name}:{self.key}:{arm}"


def load_matrix(path: Path | str) -> Matrix:
    """Parse a matrix spec. Mirrors the grid/batch vocabulary: bare lists
    of slugs/ids under ``orchestrators``/``workers``/``tasks``."""
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"experiment matrix {p} must be a YAML mapping")
    name = str(data.get("name") or p.stem)
    out: dict[str, list[str]] = {}
    for field in ("orchestrators", "workers", "tasks"):
        items = data.get(field)
        if not isinstance(items, list) or not items or not all(
            isinstance(i, str) for i in items
        ):
            raise ValueError(f"experiment matrix {p}: '{field}' must be a non-empty list")
        out[field] = items
    return Matrix(
        name=name,
        orchestrators=out["orchestrators"],
        workers=out["workers"],
        tasks=out["tasks"],
    )


def resolve_matrix_tasks(matrix: Matrix, tasks_dir: str | Path = "tasks") -> dict[str, TaskSpec]:
    """Resolve matrix task ids to specs — ids only, never paths.

    Matrix entries name committed ``tasks/`` specs; a path-shaped id could
    point at a holdout directory whose prompts must never reach the judge
    endpoint, so id syntax is whitelisted before touching the filesystem.
    """
    root = Path(tasks_dir)
    specs: dict[str, TaskSpec] = {}
    for tid in matrix.tasks:
        if "/" in tid or "\\" in tid or ".." in tid or tid.startswith("~"):
            raise ValueError(
                f"matrix task {tid!r} is not a task id — matrices reference "
                "committed tasks/ specs only"
            )
        path = find_task(tid, root)
        if path is None:
            raise ValueError(f"matrix task {tid!r}: no spec under {root}")
        specs[tid] = load_task(path)
    return specs


def isolated_runtime_ready() -> bool:
    """The cube-env contract a code-bearing cell needs to produce real
    verdicts instead of fail-closed 'no runtime' reports."""
    return bool(
        os.environ.get("ORCHESTRAL_CODE_RUNTIME") == "isolated"
        and os.environ.get("E2B_DOMAIN")
        and os.environ.get("E2B_API_KEY")
    )


def cell_runs(
    store: RunStore, matrix_name: str, cell: Cell
) -> dict[str, list[RunMeta]]:
    """This matrix's runs for the cell, split by arm.

    The arm is read from the recorded ``config.jev_assist`` — a run's arm
    membership travels with the run. Non-dry-run rows only.
    """
    prefix = f"{matrix_name}:{cell.key}:"
    arms: dict[str, list[RunMeta]] = {arm: [] for arm in ARMS}
    for meta in store.list_runs(
        task_id=cell.task_id, orchestrator=cell.orchestrator, worker=cell.worker
    ):
        if meta.dry_run or not (meta.run_group or "").startswith(prefix):
            continue
        arm = "jev" if (meta.config or {}).get("jev_assist") else "baseline"
        arms[arm].append(meta)
    return arms


def arm_stats(runs: list[RunMeta]) -> tuple[int, int, int]:
    """(passes, finished_n, infra_errors) for one arm.

    Infra errors (exception/timeout/cancel — any non-finished status) stay
    out of the pass-rate denominator but are counted: asymmetric exclusion
    would let an erroring arm look better by dropping its failures.
    """
    finished = [r for r in runs if r.status == "finished"]
    passes = sum(1 for r in finished if r.passes)
    return passes, len(finished), len(runs) - len(finished)


def status_counts(runs: list[RunMeta]) -> dict[str, int]:
    """Per-status run counts — the honest denominator split behind
    ``infra_errors`` (an orphan crash and a graceful failure used to
    hide in the same bucket)."""
    return dict(Counter(r.status for r in runs))


def dual_pass_rates(runs: list[RunMeta]) -> dict[str, float | None]:
    """Both failure-accounting conventions (the search_evals pattern).

    ``failed_excluded`` — pass rate over finished runs only;
    ``failed_as_zero`` — every non-finished run scored as zero. The
    second is display-only: mixing failure modes into ``diff_ci``
    would conflate task failure with infra failure.
    """
    finished = [r for r in runs if r.status == "finished"]
    passes = sum(1 for r in finished if r.passes)
    return {
        "failed_excluded": passes / len(finished) if finished else None,
        "failed_as_zero": passes / len(runs) if runs else None,
    }


def estimate_pair_cost(store: RunStore, cell: Cell) -> float | None:
    """Estimated cost of one replicate (both arms) for this cell.

    Task-scoped billed history — a task's cost profile dominates a
    pairing's. The jev arm is priced at JEV_LOAD_FACTOR × baseline until
    it has its own finished billing. None when the cell has no history at
    all (calibration handles it); 0.0 is a real unmetered estimate, not
    missing data.
    """
    base = store.mean_cell_cost(cell.task_id, cell.orchestrator, cell.worker, arm="baseline")
    jev = store.mean_cell_cost(cell.task_id, cell.orchestrator, cell.worker, arm="jev")
    if base is None:
        if jev is None:
            return None
        # jev history exists but baseline doesn't — un-inflate as the
        # better-guess baseline rather than falling back to a global mean.
        base = jev / JEV_LOAD_FACTOR
    return base + (jev if jev is not None else base * JEV_LOAD_FACTOR)


def rep_target(store: RunStore, cell: Cell, cell_budget: float) -> tuple[int, float | None]:
    """(target pairs, est pair cost). est=None → provisional FLOOR; the
    driver runs a calibration replicate and reprices. est=0 (unmetered)
    hits CAP — zero cost divides, it doesn't shrink reps."""
    est = estimate_pair_cost(store, cell)
    if est is None:
        return REP_FLOOR, None
    if est <= 0 or cell_budget <= 0:
        return REP_CAP if est == 0 else REP_FLOOR, est
    return min(REP_CAP, max(REP_FLOOR, math.ceil(cell_budget / est))), est


def cell_state(
    store: RunStore, matrix_name: str, cell: Cell, target: int, diff_eps: float
) -> str:
    """Derived cell state — pending | partial | done | aborted.

    ``aborted`` is the only persisted state (a ``cell-state`` annotation),
    so a killed process is distinguishable from a stopped cell. ``done``
    means reps reached target or the difference CI resolved within
    ``diff_eps`` — evidence, not just count.
    """
    for ann in store.annotations():
        if (
            ann["kind"] == "cell-state"
            and ann["target"] == f"{matrix_name}:{cell.key}"
            and ann["flag"] == "aborted"
        ):
            return "aborted"
    arms = cell_runs(store, matrix_name, cell)
    (a_pass, a_n, _), (b_pass, b_n, _) = (arm_stats(arms[a]) for a in ARMS)
    if a_n == 0 and b_n == 0:
        return "pending"
    pairs = min(a_n, b_n)
    if pairs >= target:
        return "done"
    ci = diff_ci(a_pass, a_n, b_pass, b_n)
    if ci is not None and (ci[1] - ci[0]) / 2 <= diff_eps:
        return "done"
    return "partial"


def _mark_aborted(store: RunStore, matrix_name: str, cell: Cell, reason: str) -> None:
    store.set_annotation(
        "cell-state", f"{matrix_name}:{cell.key}", "aborted", note=reason
    )


def run_is_stale(meta: RunMeta, now: float) -> bool:
    """Whether a ``running`` row's last activity predates the staleness
    window. ``events.jsonl`` mtime is the heartbeat (a live run appends
    on every call); ``started_at`` is the fallback for a run that never
    logged. No signal at all is stale — a row with nothing to check has
    nothing keeping it honest."""
    candidates: list[float] = []
    if meta.run_dir:
        with suppress(OSError):
            candidates.append(
                (Path(meta.run_dir) / "events.jsonl").stat().st_mtime
            )
    if meta.started_at:
        with suppress(ValueError):
            candidates.append(
                datetime.fromisoformat(meta.started_at).timestamp()
            )
    if not candidates:
        return True
    recorded = float((meta.config or {}).get("timeout_seconds") or 0)
    window = max(STALE_RUNNING_SECONDS, recorded + _STALE_MARGIN_SECONDS)
    return now - max(candidates) > window


def _missing_work(
    arms: dict[str, list[RunMeta]], upto: int, *, now: float | None = None
) -> tuple[dict[int, set[str]], list[RunMeta]]:
    """Replicate indexes 1..upto × arms still missing a terminal-status
    run, plus the orphaned ``running`` rows behind those gaps.

    A ``running`` row fills its slot only while it looks alive — a live
    run in another process must not be raced. A stale one is a corpse:
    its slot reopens for relaunch and the row is returned for the
    caller to mark. Treating it as done (the old behavior) dropped the
    replicate forever *and* could spin the driver loop empty forever
    when every slot in a cell was orphaned.
    """
    ts = time.time() if now is None else now
    done: dict[int, set[str]] = {}
    orphans: list[RunMeta] = []
    for arm in ARMS:
        for r in arms[arm]:
            if not r.replicate:
                continue
            if r.status in TERMINAL_STATUSES or (
                r.status == "running" and not run_is_stale(r, ts)
            ):
                done.setdefault(r.replicate, set()).add(arm)
            elif r.status == "running":
                orphans.append(r)
    missing = {i: set(ARMS) - done.get(i, set()) for i in range(1, upto + 1)}
    return missing, orphans


def run_experiment(
    store: RunStore,
    matrix: Matrix,
    *,
    budget: float,
    daily_cap: float,
    batch_size: int = 5,
    diff_eps: float = 0.15,
    seed: int | None = None,
    jobs: int = 1,
    tasks: dict[str, TaskSpec] | None = None,
    launch: CellLauncher,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the matrix. Returns a per-cell summary; never sys.exits.

    ``launch(cell, arm, replicate, run_group, seed) -> RunMeta`` performs
    one run. Budget/cap exhaustion sets a shared stop event — remaining
    cells end at their next batch boundary and stay resumable.
    """
    tasks = tasks or {}
    cells = matrix.cells
    cell_budget = budget / len(cells) if budget > 0 and cells else 0.0
    stop = threading.Event()
    stop_reason: list[str] = []
    isolated_ok = isolated_runtime_ready()
    summary: dict[str, Any] = {}

    def _spend_blown() -> bool:
        if budget > 0 and store.group_spend(f"{matrix.name}:") >= budget:
            stop_reason.append(f"experiment spend ≥ ${budget:.2f} budget")
            return True
        if daily_cap > 0 and store.spend_today() >= daily_cap:
            stop_reason.append(f"today's spend ≥ ${daily_cap:.2f} daily cap")
            return True
        return False

    def _run_cell(cell: Cell) -> None:
        if stop.is_set():
            summary[cell.key] = {"state": "pending", "note": "not started — budget stop"}
            return
        spec = tasks.get(cell.task_id)
        if spec is not None and spec.type in ISOLATED_TASK_TYPES and not isolated_ok:
            _mark_aborted(
                store, matrix.name, cell,
                "code-type task with no isolated runtime (ORCHESTRAL_CODE_RUNTIME/"
                "E2B_DOMAIN/E2B_API_KEY) — refused, no spend",
            )
            emit(f"[refused] {cell.key}: code task, no isolated runtime")
            summary[cell.key] = {"state": "aborted", "note": "no isolated runtime"}
            return
        # the priced target decides done-ness — REP_FLOOR alone would
        # "complete" a 22-pair cell that was interrupted at 5
        target, est = rep_target(store, cell, cell_budget)
        state = cell_state(store, matrix.name, cell, target, diff_eps)
        if state in ("done", "aborted"):
            emit(f"[skip] {cell.key}: already {state}")
            summary[cell.key] = {"state": state, "note": "resumed — skipped"}
            return

        emit(
            f"[cell] {cell.key}: target {target} pairs"
            + (f" (est ${est:.4f}/pair)" if est is not None else " (calibrating)")
        )
        calibrated = est is not None
        batch_infra_errors = 0
        batch_runs = 0
        pairs = 0
        marked_orphans: set[str] = set()
        # corpses keep status="running" — the aborted annotation is the
        # cross-process record that one was already repaired and marked,
        # so later driver invocations don't re-repair and re-dirty it
        aborted_runs = {
            a["target"] for a in store.annotations()
            if a["kind"] == "run" and a["flag"] == "aborted"
        }

        while not stop.is_set():
            arms = cell_runs(store, matrix.name, cell)
            a_pass, a_n, _ = arm_stats(arms["baseline"])
            b_pass, b_n, _ = arm_stats(arms["jev"])
            pairs = min(a_n, b_n)
            if pairs >= target:
                summary[cell.key] = {"state": "done", "reps": pairs, "reason": "target reached"}
                return
            ci = diff_ci(a_pass, a_n, b_pass, b_n)
            if ci is not None and (ci[1] - ci[0]) / 2 <= diff_eps:
                summary[cell.key] = {
                    "state": "done", "reps": pairs,
                    "reason": f"difference CI resolved (hw {(ci[1]-ci[0])/2:.3f} ≤ {diff_eps})",
                }
                emit(f"[done] {cell.key}: CI resolved at {pairs} pairs")
                return

            # a cell with no billing history runs one calibration pair,
            # then reprices from what actually billed
            upto = pairs + 1 if not calibrated else min(target, pairs + batch_size)
            missing, orphans = _missing_work(arms, upto)
            for orphan in orphans:
                if orphan.run_id in marked_orphans or orphan.run_id in aborted_runs:
                    continue
                # the aborted annotation is the cross-process claim on the
                # slot — it has to be atomic or a racing driver/recover
                # launches a second replacement into the same replicate
                try:
                    claimed = store.claim_aborted(
                        orphan.run_id,
                        "orphaned 'running' row — presumed process death",
                    )
                except Exception as exc:
                    emit(
                        f"[warn] {cell.key}: abort claim for "
                        f"{orphan.run_id} failed ({type(exc).__name__}: {exc}) "
                        "— retrying next pass"
                    )
                    continue
                if not claimed:
                    continue
                marked_orphans.add(orphan.run_id)
                # hydrate the corpse's billed calls into runs.* before it
                # goes on the books — killed runs would otherwise meter $0
                # to coverage and spend reports. Best-effort: a transient
                # store failure must not take the whole cell summary down.
                try:
                    store.repair_orphan_costs(orphan.run_id)
                except Exception as exc:
                    emit(
                        f"[warn] {cell.key}: orphan cost repair for "
                        f"{orphan.run_id} failed ({type(exc).__name__}: {exc})"
                    )
                emit(
                    f"[recover] {cell.key} rep {orphan.replicate}: orphan "
                    f"{orphan.run_id} marked aborted — slot reopened"
                )
            if not any(missing.values()):
                # every slot is held by a terminal row or a live run —
                # nothing launchable this pass; breaking keeps an
                # all-orphan cell from spinning the loop forever
                break
            for i in sorted(missing):
                if stop.is_set():
                    break
                for arm in (ARMS if i % 2 == 1 else ARMS[::-1]):
                    if arm not in missing[i] or stop.is_set():
                        continue
                    rep_seed = seed + i - 1 if seed is not None else None
                    try:
                        launch(cell, arm, i, cell.group(matrix.name, arm), rep_seed)
                    except Exception as exc:
                        batch_infra_errors += 1
                        emit(f"[fail] {cell.key} {arm} rep {i}: {type(exc).__name__}: {exc}")
                    batch_runs += 1

            if batch_runs and batch_infra_errors > batch_runs / 2:
                reason = f"{batch_infra_errors}/{batch_runs} runs errored in batch"
                _mark_aborted(store, matrix.name, cell, reason)
                emit(f"[aborted] {cell.key}: {reason}")
                summary[cell.key] = {"state": "aborted", "reason": reason}
                return
            batch_infra_errors = batch_runs = 0

            if not calibrated:
                est = estimate_pair_cost(store, cell)
                calibrated = True
                if est is not None:
                    target = min(
                        REP_CAP,
                        max(REP_FLOOR, math.ceil(cell_budget / est)) if cell_budget > 0 else REP_FLOOR,
                    )
                    emit(f"[priced] {cell.key}: ${est:.4f}/pair → target {target} pairs")
                else:
                    target = REP_FLOOR  # still unmetered — floor only
            elif est is not None and est > 0 and cell_budget > 0:
                # one-directional re-target: a hotter-than-history cell
                # shrinks; cheaper-than-history does not grow
                new_est = estimate_pair_cost(store, cell)
                if new_est and new_est > est:
                    target = min(target, max(REP_FLOOR, math.ceil(cell_budget / new_est)))
                    est = new_est

            if _spend_blown():
                stop.set()

        summary[cell.key] = {
            "state": cell_state(store, matrix.name, cell, target, diff_eps),
            "reps": pairs,
            "note": (
                "stopped — budget/daily cap" if stop.is_set()
                # every slot is held by a terminal row or a live run
                # elsewhere — nothing launchable, nothing owed
                else "parked — slots held by terminal or live runs"
            ),
        }

    if jobs > 1 and len(cells) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(_run_cell, cells))
    else:
        for cell in cells:
            _run_cell(cell)

    cells_by_key = {cell.key: cell for cell in cells}
    missing_by_group = store.missing_cost_counts_by_group(f"{matrix.name}:")
    for key in summary:
        prefix = f"{matrix.name}:{key}:"
        summary[key]["missing_cost"] = sum(
            n for g, n in missing_by_group.items() if g.startswith(prefix)
        )
        arms = cell_runs(store, matrix.name, cells_by_key[key])
        summary[key]["arms"] = {
            arm: {
                "status": status_counts(arms[arm]),
                **dual_pass_rates(arms[arm]),
            }
            for arm in ARMS
        }
    return {
        "matrix": matrix.name,
        "cells": summary,
        "spend": store.group_spend(f"{matrix.name}:"),
        "stopped": stop_reason[0] if stop_reason else None,
    }
