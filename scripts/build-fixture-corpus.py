#!/usr/bin/env python3
"""Build the deterministic observatory fixture corpus (KTD14).

Writes a SQLite run index plus run directories (``run.json``,
``events.jsonl``, ``report.json``) through ``RunStore`` with no network and
no provider keys. Every cost, liveness, browser and capture test renders
this data, so it must be identical on every build.

    python3 scripts/build-fixture-corpus.py --out /tmp/corpus-runs [--shape full]

Shapes: ``empty`` (0 runs), ``single`` (1 run), ``full`` (1,000+ runs covering
the 92-character group key, an orphaned ``running`` row, a failed run with
spend, dry and holdout runs, a one-pairing group, an all-low-n group, and
priced call history over several models with differing billed/rate-card
ratios). Rate cards live in ``tests/fixtures/observatory/models/`` and task
specs in ``tests/fixtures/observatory/tasks/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestral.storage import RunMeta, RunStore, _safe_name  # noqa: E402

SEED = 20261002
EPOCH = datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)
FIXTURES = REPO / "tests" / "fixtures" / "observatory"

# billed / rate-card multiple per model. None = the provider never priced it.
RATIOS: dict[str, float | None] = {
    "corpus/orch-a": 2.1,
    "corpus/orch-b": 1.4,
    "corpus/worker-cheap": 0.99,
    "corpus/worker-mid": 3.0,
    "corpus/worker-hot": 13.25,
    "corpus/worker-fresh": 4.0,
    "corpus/worker-local": None,
    "corpus/judge": 1.0,
}
# per-million-token rate cards, mirrored from models/corpus.yaml
RATES: dict[str, tuple[float, float]] = {
    "corpus/orch-a": (0.30, 1.20),
    "corpus/orch-b": (0.60, 2.40),
    "corpus/worker-cheap": (0.10, 0.40),
    "corpus/worker-mid": (0.20, 0.80),
    "corpus/worker-hot": (0.05, 0.20),
    "corpus/worker-fresh": (0.15, 0.60),
    "corpus/worker-local": (0.10, 0.40),
    "corpus/judge": (0.20, 0.80),
}
TASKS = {
    "corpus-landing-page": 1.0, "corpus-code-slugify": 0.8, "corpus-sql-revenue": 1.2,
    "corpus-extract-invoice": 1.4, "corpus-bugfix-lru": 0.9, "corpus-api-order": 1.1,
}
MAIN_PAIRINGS = (
    ("corpus/orch-a", "corpus/worker-cheap", 0.80),
    ("corpus/orch-a", "corpus/worker-mid", 0.65),
    ("corpus/orch-b", "corpus/worker-hot", 0.55),
    ("corpus/orch-b", "corpus/worker-cheap", 0.70),
)
REPS = 40
UNPRICED_RATE = 0.10  # share of ordinary calls the provider returned no cost for
LONG_GROUP = ("matrix-2026-10-02-orchestral-jev-ab-baseline-vs-assist-corpus-orch-a-worker-"
              "cheap-rep-batch-0001")[:92]


class _Builder:
    def __init__(self, root: Path):
        self.store = RunStore(root)
        self.root = root
        self.rng = random.Random(SEED)
        self.n = 0
        self.calls: list[tuple[Any, ...]] = []
        self.manifest: dict[str, Any] = {"runs": 0}

    # -- one run ----------------------------------------------------------

    def run(self, *, run_id: str | None = None, orch: str, worker: str, task: str, group: str,
            status: str = "finished", dry: bool = False, holdout: bool = False,
            replicate: int | None = None, pass_p: float = 0.7, task_factor: float = 1.0,
            fixed_calls: list[tuple[str, str, float, float | None]] | None = None,
            total_cost: float | None = None, started: datetime | None = None,
            unpriced_all: bool = False, judged: bool = False,
            events_until: datetime | None = None, failure: str | None = None) -> str:
        self.n += 1
        run_id = run_id or f"corp{self.n:08d}"
        started = started or EPOCH + timedelta(minutes=7 * self.n)
        run_dir = self.root / _safe_name(orch) / _safe_name(task) / _safe_name(worker) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        jitter = self.rng.lognormvariate(0.0, 0.25) * task_factor

        specs: list[tuple[str, str, int, int, float, float | None]] = []
        if fixed_calls is not None:
            for role, model, cost, api in fixed_calls:
                specs.append((role, model, 1000, 800, cost, api))
        else:
            plan = [("orchestrator", orch, 800, 400), ("worker", worker, 600, 900),
                    ("worker", worker, 600, 900), ("worker", worker, 600, 900),
                    ("orchestrator", orch, 2000, 1500)]
            if status == "failed":
                plan = plan[:3]
            if judged:
                plan.append(("judge", "corpus/judge", 3000, 600))
            for role, model, tin, tout in plan:
                tin_j, tout_j = round(tin * jitter), round(tout * jitter)
                rin, rout = RATES[model]
                cost = round((tin_j * rin + tout_j * rout) / 1e6, 8)
                ratio = RATIOS[model]
                api: float | None
                if dry or unpriced_all or ratio is None or self.rng.random() < UNPRICED_RATE:
                    api = None
                else:
                    api = round(cost * ratio * self.rng.lognormvariate(0.0, 0.05), 8)
                specs.append((role, model, tin_j, tout_j, cost, api))

        events: list[dict[str, Any]] = []

        def ev(seq: int, ts: datetime, typ: str, phase: str, **kw: Any) -> None:
            events.append({
                "event_id": hashlib.sha256(f"{run_id}:{seq}".encode()).hexdigest()[:16],
                "schema_version": "2", "run_id": run_id, "sequence": seq,
                "timestamp": ts.isoformat(), "phase": phase, "step": seq - 3, "type": typ,
                "role": kw.pop("role", "harness"), "worker_id": None,
                "model": kw.pop("model", ""), "input": {}, "output": kw.pop("output", {}),
                "reasoning": "", "cost": kw.pop("cost", {}), "latency_ms": 0.0,
                "error": None, "metadata": {}})

        ev(1, started, "run.created", "init", output={"run_id": run_id, "dry_run": dry})
        ev(2, started + timedelta(seconds=1), "run.started", "init",
           output={"run_id": run_id, "dry_run": dry})
        total_in = total_out = 0
        total_rate_card = 0.0
        for i, (role, model, tin, tout, cost, api) in enumerate(specs):
            ts = started + timedelta(seconds=2 + 2 * i)
            ev(3 + i, ts, "llm_call", "delegate" if role == "worker" else "plan",
               role=role, model=model,
               cost={"input_tokens": tin, "output_tokens": tout, "usd": cost,
                     "api_cost_usd": api, "pricing_source": "api_reported" if api is not None else "none"})
            self.calls.append((
                run_id, "delegate" if role == "worker" else "plan", i + 1, role, model, tin, tout,
                cost, api, "api_reported" if api is not None else "none", 250.0, 1, None, None,
                int(dry), ts.isoformat(), None, i + 1))
            total_in += tin
            total_out += tout
            total_rate_card += cost
        end = started + timedelta(seconds=4 + 2 * len(specs))
        finished_at: str | None = None
        passes: bool | None = None
        score: float | None = None
        if status != "running":
            passes = status == "finished" and self.rng.random() < pass_p
            score = round(self.rng.uniform(0.3, 1.0), 2) if status == "finished" else None
            finished_at = end.isoformat()
            ev(len(events) + 1, end, "run.completed", "end",
               output={"status": status, "passes": passes, "score": score})
        if events_until is not None:
            events = [e for e in events if e["timestamp"] <= events_until.isoformat()]
        (run_dir / "events.jsonl").write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
        if status != "running":
            (run_dir / "report.json").write_text(json.dumps({
                "task_id": task, "artifact_length": 420 + self.n % 97,
                "checks": {"non_empty": status == "finished"},
                "errors": [] if status == "finished" else [failure or "worker_error"],
                "score": score, "delegated": True}, indent=2, sort_keys=True))

        judge_score = round(self.rng.uniform(0.4, 1.0), 2) if judged and status == "finished" else None
        meta = RunMeta(
            run_id=run_id, orchestrator=orch, task_id=task, worker=worker, status=status,
            started_at=started.isoformat(), finished_at=finished_at,
            total_cost_usd=(total_cost if total_cost is not None
                            else (0.0 if status == "failed" else round(total_rate_card, 8))),
            total_input_tokens=total_in, total_output_tokens=total_out, score=score,
            passes=passes, run_dir=str(run_dir),
            config={"dry_run": dry, "holdout": holdout, "jev_assist": False, "planner": "raw"},
            latency_ms=0.0 if status == "running" else round(1500 + 900 * jitter, 1),
            failure_reason=(failure or "worker_error") if status == "failed" else None,
            env={"git_sha": "corpus"}, run_group=group, replicate=replicate, dry_run=dry,
            judge_score=judge_score, judge_passed=(judge_score >= 0.6) if judge_score else None,
            delegated=True)
        (run_dir / "run.json").write_text(json.dumps(meta.to_dict(), indent=2, default=str))
        self.store.index_meta(meta)
        return run_id

    # -- finish -----------------------------------------------------------

    def flush(self) -> None:
        with closing(sqlite3.connect(self.store.db)) as conn, conn:
            conn.executemany(
                "INSERT INTO calls (run_id, phase, step, role, model, input_tokens, output_tokens,"
                " cost_usd, api_cost_usd, pricing_source, latency_ms, attempt, error_category, error,"
                " dry_run, created_at, worker_id, sequence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", self.calls)
            # a fixture is a clean index: nothing awaits a sync push
            conn.execute("DELETE FROM sync_dirty")
        conn = sqlite3.connect(self.store.db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        self.manifest["runs"] = self.n
        self.manifest["calls"] = len(self.calls)


def build_corpus(root: Path, shape: str = "full") -> dict[str, Any]:
    """Write the corpus into ``root`` (a runs directory) and describe it."""
    if shape not in ("empty", "single", "full"):
        raise ValueError(f"unknown shape {shape!r}: expected empty, single or full")
    root = Path(root)
    b = _Builder(root)
    if shape == "single":
        b.run(orch="corpus/orch-a", worker="corpus/worker-cheap", task="corpus-landing-page",
              group="corpus-single", pass_p=1.0)
    elif shape == "full":
        tasks = list(TASKS.items())
        for rep in range(REPS):
            for orch, worker, pass_p in MAIN_PAIRINGS:
                for task, factor in tasks:
                    b.run(orch=orch, worker=worker, task=task, group=f"corpus-main:r{rep // 10}",
                          replicate=rep, pass_p=pass_p, task_factor=factor,
                          status="failed" if b.rng.random() < 0.05 else "finished",
                          judged=b.rng.random() < 0.3)
        for i in range(40):
            b.run(orch="corpus/orch-a", worker="corpus/worker-cheap",
                  task=tasks[i % len(tasks)][0], group="corpus-dry", dry=True, replicate=i)
        for i in range(6):
            b.run(orch="corpus/orch-a", worker="corpus/worker-cheap",
                  task=tasks[i % 3][0], group="corpus-solo", replicate=i)
        for i in range(5):
            b.run(orch="corpus/orch-a", worker="corpus/worker-cheap",
                  task="corpus-landing-page", group=LONG_GROUP, replicate=i)
        for i in range(4):
            b.run(orch="corpus/orch-b", worker="corpus/worker-cheap", task="corpus-landing-page",
                  group="corpus-holdout", holdout=True, replicate=i)
        for i in range(4):
            b.run(orch="corpus/orch-a", worker="corpus/worker-local", task="corpus-code-slugify",
                  group="corpus-unpriced", unpriced_all=True, replicate=i)
        for orch in ("corpus/orch-a", "corpus/orch-b"):
            for i in range(2):
                b.run(orch=orch, worker="corpus/worker-fresh", task="corpus-api-order",
                      group="corpus-thin", pass_p=1.0, replicate=i)
        # a failed run: the index recorded $0.11, the provider billed $0.74
        b.manifest["failed_run_id"] = b.run(
            run_id="corpfail0001", orch="corpus/orch-b", worker="corpus/worker-hot",
            task="corpus-failed-task", group="corpus-failed", status="failed",
            failure="worker_timeout", total_cost=0.11,
            fixed_calls=[("orchestrator", "corpus/orch-b", 0.20, 0.30),
                         ("worker", "corpus/worker-hot", 0.03, 0.24),
                         ("worker", "corpus/worker-hot", 0.02, 0.20)])
        # an orphan: still `running`, last heard from two days before the corpus clock
        b.manifest["orphan_run_id"] = b.run(
            run_id="corporph0930", orch="corpus/orch-a", worker="corpus/worker-cheap",
            task="corpus-landing-page", group="corpus-orphan", status="running",
            started=datetime(2026, 9, 30, 8, 0, 0, tzinfo=UTC),
            events_until=datetime(2026, 9, 30, 8, 7, 0, tzinfo=UTC))
        b.manifest.update(solo_group="corpus-solo", thin_group="corpus-thin",
                          long_group=LONG_GROUP, clock="2026-10-02T12:00:00+00:00")
    b.flush()
    b.manifest["shape"] = shape
    return b.manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="runs directory to write")
    ap.add_argument("--shape", default="full", choices=("empty", "single", "full"))
    args = ap.parse_args()
    manifest = build_corpus(args.out, args.shape)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
