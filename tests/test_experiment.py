"""Tests for the batched A/B experiment driver (orchestral/experiment.py)."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import harness
from orchestral.config import TaskSpec
from orchestral.experiment import (
    REP_CAP,
    REP_FLOOR,
    STALE_RUNNING_SECONDS,
    Cell,
    Matrix,
    cell_runs,
    cell_state,
    estimate_pair_cost,
    load_matrix,
    rep_target,
    resolve_matrix_tasks,
    run_experiment,
    run_is_stale,
)
from orchestral.storage import RunMeta, RunStore


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t", "worker": "w/m",
        "status": "finished", "started_at": "2026-01-01T00:00:00",
        "total_cost_usd": 0.01, "total_input_tokens": 10, "total_output_tokens": 20,
        "latency_ms": 100.0, "passes": True, "score": 0.8,
        "run_group": "g", "config": {},
    }
    base.update(kw)
    return RunMeta(**base)


MATRIX = Matrix(name="m", orchestrators=["o/m"], workers=["w/m"], tasks=["t"])
CELL = Cell(task_id="t", orchestrator="o/m", worker="w/m")


def _seed_arm(store: RunStore, arm: str, n: int, *, passes: bool = True,
              cost: float = 0.01, group_prefix: str = "m", offset: int = 0) -> None:
    """Index `n` finished runs for one arm of the default matrix cell,
    replicate indexes offset+1 .. offset+n."""
    jev = arm == "jev"
    for k in range(1, n + 1):
        i = offset + k
        rid = f"{group_prefix}-seed-{arm}-{i}-{passes}"
        store.index_meta(_meta(
            run_id=rid, passes=passes, total_cost_usd=cost,
            run_group=f"{group_prefix}:t:o/m:w/m:{arm}",
            replicate=i, config={"jev_assist": jev},
        ))
        store.record_call(run_id=rid, phase="plan", step=1, role="orchestrator",
                          model="o/m", cost_usd=cost)


class FakeLaunch:
    """Injected launcher: indexes a real RunMeta + call row per launch so
    the driver's own store queries see it — no patched seams."""

    def __init__(self, store: RunStore, *, passes: bool = True, cost: float = 0.01,
                 fail: set[tuple[str, int]] | None = None, api_cost: float | None = None):
        self.store = store
        self.passes = passes
        self.cost = cost
        self.api_cost = api_cost  # what the provider billed, when it differs from the rate card
        self.fail = fail or set()  # {(arm, rep)} that raise
        self.launches: list[tuple[str, str, int, str, int | None]] = []

    def __call__(self, cell: Cell, arm: str, rep: int, group: str, seed):
        self.launches.append((cell.key, arm, rep, group, seed))
        if (arm, rep) in self.fail:
            rid = f"{group}:{rep}"
            self.store.index_meta(_meta(
                run_id=rid, task_id=cell.task_id, orchestrator=cell.orchestrator,
                worker=cell.worker, run_group=group, replicate=rep, status="failed",
                passes=None, score=None, failure_reason="exception:boom",
                config={"jev_assist": arm == "jev"},
            ))
            raise RuntimeError("boom")
        rid = f"{group}:{rep}"
        self.store.index_meta(_meta(
            run_id=rid, task_id=cell.task_id, orchestrator=cell.orchestrator,
            worker=cell.worker, run_group=group, replicate=rep,
            passes=self.passes, total_cost_usd=self.cost,
            config={"jev_assist": arm == "jev"},
        ))
        self.store.record_call(run_id=rid, phase="plan", step=1,
                               role="orchestrator", model="o/m", cost_usd=self.cost,
                               api_cost_usd=self.api_cost)
        return self.store.get_run(rid)


class TestRepTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_history_calibrates_at_floor(self):
        target, est = rep_target(self.store, CELL, cell_budget=1.0)
        self.assertEqual(target, REP_FLOOR)
        self.assertIsNone(est)

    def test_zero_cost_hits_cap(self):
        _seed_arm(self.store, "baseline", 3, cost=0.0)
        target, est = rep_target(self.store, CELL, cell_budget=1.0)
        self.assertEqual((target, est), (REP_CAP, 0.0))

    def test_floor_when_pair_costs_more_than_budget(self):
        _seed_arm(self.store, "baseline", 3, cost=10.0)
        target, _ = rep_target(self.store, CELL, cell_budget=0.05)
        self.assertEqual(target, REP_FLOOR)

    def test_in_range(self):
        _seed_arm(self.store, "baseline", 5, cost=0.01)
        # est pair = 0.01 * (1 + 1.3) = 0.023 → 0.50/0.023 → 22
        target, est = rep_target(self.store, CELL, cell_budget=0.50)
        self.assertAlmostEqual(est, 0.023)
        self.assertEqual(target, 22)

    def test_jev_arm_uses_own_history_once_present(self):
        _seed_arm(self.store, "baseline", 5, cost=0.01)
        _seed_arm(self.store, "jev", 3, cost=0.02)
        self.assertAlmostEqual(estimate_pair_cost(self.store, CELL), 0.03)

    def test_dry_run_rows_excluded_from_estimates(self):
        for i in range(3):
            self.store.index_meta(_meta(
                run_id=f"dry{i}", run_group="m:t:o/m:w/m:baseline",
                replicate=i + 1, dry_run=True, total_cost_usd=5.0,
            ))
        self.assertIsNone(estimate_pair_cost(self.store, CELL))


class TestDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)
        self.spec = TaskSpec(id="t", type="html", prompt="p")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, launch, **kw):
        kw.setdefault("tasks", {"t": self.spec})
        kw.setdefault("emit", lambda *_: None)
        return run_experiment(self.store, MATRIX, launch=launch, **kw)

    def test_arms_interleave_alternating_order(self):
        launch = FakeLaunch(self.store)
        self._run(launch, budget=100.0, daily_cap=0.0, batch_size=2,
                  diff_eps=0.0)  # eps 0 → CI never resolves; floor 5 pairs
        reps: dict[int, list[str]] = {}
        for _key, arm, rep, _g, _s in launch.launches:
            reps.setdefault(rep, []).append(arm)
        self.assertEqual(reps[1], ["baseline", "jev"])
        self.assertEqual(reps[2], ["jev", "baseline"])
        self.assertEqual(reps[3], ["baseline", "jev"])

    def test_run_group_and_seed_wired(self):
        launch = FakeLaunch(self.store)
        self._run(launch, budget=0.0, daily_cap=0.0, batch_size=1,
                  diff_eps=0.0, seed=7)
        _key, _arm, _rep, group, seed = launch.launches[0]
        self.assertEqual(group, "m:t:o/m:w/m:baseline")
        self.assertEqual(seed, 7)  # seed + rep - 1
        # and the run landed under the matrix's group prefix
        arms = cell_runs(self.store, "m", CELL)
        self.assertEqual({a: len(r) for a, r in arms.items()},
                         {"baseline": REP_FLOOR, "jev": REP_FLOOR})

    def test_early_stop_on_resolved_difference(self):
        launch = FakeLaunch(self.store, passes=True)
        # jev always passes, baseline always fails → difference resolves fast
        def split(cell, arm, rep, group, seed):
            launch.passes = arm == "jev"
            return launch(cell, arm, rep, group, seed)
        out = self._run(split, budget=100.0, daily_cap=0.0, batch_size=5,
                        diff_eps=0.5)
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "done")
        self.assertIn("CI resolved", out["cells"]["t:o/m:w/m"]["reason"])
        # calibration pair + one batch of 5 resolved it — no burst to cap
        self.assertEqual(len(launch.launches), 12)

    def test_error_rate_aborts_and_persists(self):
        # >50% of batch runs raise → cell-state annotation, not sys.exit
        launch = FakeLaunch(self.store,
                            fail={("baseline", i) for i in range(1, 6)}
                                 | {("jev", i) for i in range(1, 6)})
        out = self._run(launch, budget=100.0, daily_cap=0.0, batch_size=5,
                        diff_eps=0.0)
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "aborted")
        aborted = [a for a in self.store.annotations()
                   if a["kind"] == "cell-state" and a["flag"] == "aborted"]
        self.assertEqual(len(aborted), 1)
        # a resume leaves it alone
        launch2 = FakeLaunch(self.store)
        out2 = self._run(launch2, budget=100.0, daily_cap=0.0)
        self.assertEqual(launch2.launches, [])
        self.assertEqual(out2["cells"]["t:o/m:w/m"]["state"], "aborted")

    def test_budget_aborts_on_live_calls_meter(self):
        launch = FakeLaunch(self.store, cost=0.02)
        out = self._run(launch, budget=0.03, daily_cap=0.0, batch_size=5,
                        diff_eps=0.0)
        # 1 calibration pair = $0.04 metered in calls → stop before batch 2
        self.assertIsNotNone(out["stopped"])
        self.assertEqual(len(launch.launches), 2)
        self.assertAlmostEqual(out["spend"], 0.04)

    def test_budget_aborts_when_only_billed_spend_crosses_it(self):
        # rate card says $0.01 per call (under the $0.03 budget after a pair),
        # the provider billed $0.05: the stop must follow the bill
        launch = FakeLaunch(self.store, cost=0.01, api_cost=0.05)
        out = self._run(launch, budget=0.03, daily_cap=0.0, batch_size=5,
                        diff_eps=0.0)
        self.assertIsNotNone(out["stopped"])
        self.assertEqual(len(launch.launches), 2)
        self.assertAlmostEqual(out["spend"], 0.10)

    def test_resume_skips_done_cells(self):
        # priced target must be reached — expensive history keeps the
        # target at REP_FLOOR so 5/5 pairs is genuinely done
        _seed_arm(self.store, "baseline", REP_FLOOR, cost=10.0)
        _seed_arm(self.store, "jev", REP_FLOOR, cost=10.0)
        launch = FakeLaunch(self.store)
        out = self._run(launch, budget=100.0, daily_cap=0.0, diff_eps=0.0)
        self.assertEqual(launch.launches, [])
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "done")

    def test_resume_continues_interrupted_priced_cell(self):
        # cheap history prices the cell far above the floor — an
        # interrupted run at REP_FLOOR pairs is partial, not done
        _seed_arm(self.store, "baseline", REP_FLOOR)
        _seed_arm(self.store, "jev", REP_FLOOR)
        launch = FakeLaunch(self.store)
        out = self._run(launch, budget=100.0, daily_cap=0.0, batch_size=5,
                        diff_eps=0.0)
        self.assertTrue(launch.launches)
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "done")

    def test_partial_cell_fills_gap_before_advancing(self):
        # baseline has reps 1-3, jev only 1-2 → next launch is jev rep 3
        _seed_arm(self.store, "baseline", 3)
        _seed_arm(self.store, "jev", 2)
        launch = FakeLaunch(self.store)
        self._run(launch, budget=100.0, daily_cap=0.0, batch_size=5,
                  diff_eps=0.0)
        self.assertEqual(launch.launches[0][1:3], ("jev", 3))

    def test_code_cell_refused_without_isolated_runtime(self):
        spec = TaskSpec(id="t", type="code", prompt="p")
        env = dict.fromkeys(("ORCHESTRAL_CODE_RUNTIME", "E2B_DOMAIN", "E2B_API_KEY"), "")
        launch = FakeLaunch(self.store)
        with patch.dict(os.environ, env):
            out = self._run(launch, tasks={"t": spec}, budget=100.0,
                            daily_cap=0.0)
        self.assertEqual(launch.launches, [])
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "aborted")
        self.assertIn("runtime", out["cells"]["t:o/m:w/m"]["note"])

    def test_code_cell_runs_with_isolated_runtime(self):
        spec = TaskSpec(id="t", type="code", prompt="p")
        env = {"ORCHESTRAL_CODE_RUNTIME": "isolated", "E2B_DOMAIN": "x",
               "E2B_API_KEY": "y"}
        launch = FakeLaunch(self.store)
        with patch.dict(os.environ, env):
            self._run(launch, tasks={"t": spec}, budget=0.0, daily_cap=0.0,
                      batch_size=1, diff_eps=0.0)
        self.assertEqual(len(launch.launches), 2 * REP_FLOOR)


class TestMatrixLoading(unittest.TestCase):
    def test_load_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            tasks_dir.mkdir()
            (tasks_dir / "t1.yaml").write_text(
                "id: t1\ntype: html\nprompt: hi\n", encoding="utf-8")
            mpath = Path(tmp) / "m.yaml"
            mpath.write_text(
                "name: m\norchestrators: [o/m]\nworkers: [w/m]\ntasks: [t1]\n",
                encoding="utf-8")
            matrix = load_matrix(mpath)
            self.assertEqual(matrix.name, "m")
            self.assertEqual(len(matrix.cells), 1)
            specs = resolve_matrix_tasks(matrix, tasks_dir)
            self.assertEqual(specs["t1"].id, "t1")

    def test_path_shaped_task_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Matrix(name="m", orchestrators=["o"], workers=["w"],
                       tasks=["../secret"])
            with self.assertRaises(ValueError):
                resolve_matrix_tasks(m, tmp)

    def test_bad_matrix_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("orchestrators: []\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_matrix(bad)


class TestDryRun(unittest.TestCase):
    def test_dry_run_prints_plan_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            tasks_dir.mkdir()
            (tasks_dir / "t.yaml").write_text(
                "id: t\ntype: html\nprompt: hi\n", encoding="utf-8")
            mpath = Path(tmp) / "m.yaml"
            mpath.write_text(
                "name: m\norchestrators: [o/m]\nworkers: [w/m]\ntasks: [t]\n",
                encoding="utf-8")
            args = argparse.Namespace(
                matrix=str(mpath), tasks_dir=str(tasks_dir),
                runs_dir=str(Path(tmp) / "runs"), budget=12.0, diff_eps=0.15,
                dry_run=True,
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                harness.cmd_experiment(args)
            self.assertIn("t:o/m:w/m", buf.getvalue())
            # RunStore's ctor creates index.db; "touches nothing" means no rows
            store = RunStore(args.runs_dir)
            self.assertEqual(store.list_runs(), [])
            self.assertEqual(store.annotations(), [])


class TestRunIsStale(unittest.TestCase):
    def test_no_signals_is_stale(self):
        meta = _meta(status="running", run_dir=None, started_at="")
        self.assertTrue(run_is_stale(meta, time.time()))

    def test_fresh_events_mtime_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
            meta = _meta(status="running", run_dir=str(run_dir), started_at="")
            self.assertFalse(run_is_stale(meta, time.time()))

    def test_old_events_mtime_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r"
            run_dir.mkdir()
            ev = run_dir / "events.jsonl"
            ev.write_text("{}\n", encoding="utf-8")
            old = time.time() - STALE_RUNNING_SECONDS - 60
            os.utime(ev, (old, old))
            meta = _meta(status="running", run_dir=str(run_dir), started_at="")
            self.assertTrue(run_is_stale(meta, time.time()))

    def test_started_at_fallback(self):
        old = _meta(status="running", run_dir=None,
                    started_at="2026-01-01T00:00:00+00:00")
        fresh = _meta(status="running", run_dir=None,
                      started_at=datetime.now(UTC).isoformat())
        self.assertTrue(run_is_stale(old, time.time()))
        self.assertFalse(run_is_stale(fresh, time.time()))


class TestOrphanRecovery(unittest.TestCase):
    """A0: 'running' rows that outlive their process reopen their slots."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)
        self.spec = TaskSpec(id="t", type="html", prompt="p")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, launch, **kw):
        kw.setdefault("tasks", {"t": self.spec})
        kw.setdefault("emit", lambda *_: None)
        return run_experiment(self.store, MATRIX, launch=launch, **kw)

    def _orphan(self, arm: str, rep: int, *, stale: bool) -> str:
        """Index a 'running' row with an events.jsonl heartbeat — old
        mtime for corpses, fresh for runs possibly live elsewhere."""
        rid = f"orph-{arm}-{rep}-{'stale' if stale else 'live'}"
        run_dir = Path(self.tmp.name) / rid
        run_dir.mkdir()
        ev = run_dir / "events.jsonl"
        ev.write_text("{}\n", encoding="utf-8")
        started = datetime.now(UTC).isoformat()
        if stale:
            old = time.time() - STALE_RUNNING_SECONDS - 60
            os.utime(ev, (old, old))
            started = "2026-01-01T00:00:00+00:00"
        self.store.index_meta(_meta(
            run_id=rid, status="running", started_at=started,
            run_group=f"m:t:o/m:w/m:{arm}", replicate=rep,
            run_dir=str(run_dir), passes=None, score=None,
            total_cost_usd=None, config={"jev_assist": arm == "jev"},
        ))
        return rid

    def test_stale_orphan_slot_reopens_and_marks_aborted(self):
        rid = self._orphan("baseline", 1, stale=True)
        launch = FakeLaunch(self.store)
        self._run(launch, budget=0.0, daily_cap=0.0, batch_size=1,
                  diff_eps=0.0)
        slots = {(a, r) for _k, a, r, _g, _s in launch.launches}
        self.assertIn(("baseline", 1), slots)
        aborted = [a for a in self.store.annotations()
                   if a["kind"] == "run" and a["target"] == rid
                   and a["flag"] == "aborted"]
        self.assertEqual(len(aborted), 1)

    def test_live_running_row_blocks_slot(self):
        rid = self._orphan("baseline", 1, stale=False)
        launch = FakeLaunch(self.store)
        out = self._run(launch, budget=0.0, daily_cap=0.0, batch_size=1,
                        diff_eps=0.0)
        # the live row holds baseline rep 1 — jev rep 1 launches alone,
        # then every slot is filled and the cell parks at partial
        self.assertEqual(
            launch.launches[0][1:3], ("jev", 1))
        self.assertEqual(len(launch.launches), 1)
        self.assertFalse([a for a in self.store.annotations()
                          if a["target"] == rid])
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "partial")

    def test_terminal_statuses_fill_slots(self):
        for i in range(1, REP_FLOOR + 1):
            self.store.index_meta(_meta(
                run_id=f"cancel-b-{i}", status="cancelled",
                run_group="m:t:o/m:w/m:baseline", replicate=i,
                passes=None, score=None, total_cost_usd=None))
            self.store.index_meta(_meta(
                run_id=f"fail-j-{i}", status="failed",
                run_group="m:t:o/m:w/m:jev", replicate=i,
                passes=None, score=None, total_cost_usd=None,
                failure_reason="exception:boom",
                config={"jev_assist": True}))
        launch = FakeLaunch(self.store)
        out = self._run(launch, budget=100.0, daily_cap=0.0, diff_eps=0.0)
        # terminal rows own their slots — nothing relaunches, no spin
        self.assertEqual(launch.launches, [])
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "pending")

    def test_all_orphan_cell_recovers(self):
        for arm in ("baseline", "jev"):
            for i in range(1, REP_FLOOR + 1):
                self._orphan(arm, i, stale=True)
        launch = FakeLaunch(self.store)
        out = self._run(launch, budget=0.0, daily_cap=0.0, batch_size=5,
                        diff_eps=0.0)
        # every corpse marked once, every slot relaunched, cell completes
        self.assertEqual(len(launch.launches), 2 * REP_FLOOR)
        aborted = [a for a in self.store.annotations()
                   if a["kind"] == "run" and a["flag"] == "aborted"]
        self.assertEqual(len(aborted), 2 * REP_FLOOR)
        self.assertEqual(out["cells"]["t:o/m:w/m"]["state"], "done")


class TestCmdRecover(unittest.TestCase):
    """harness recover: relaunch the slot an orphaned run left behind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runs_dir = root / "runs"
        self.store = RunStore(str(self.runs_dir))
        self.tasks_dir = root / "tasks"
        self.tasks_dir.mkdir()
        (self.tasks_dir / "t.yaml").write_text(
            "id: t\ntype: html\nprompt: hi\n", encoding="utf-8")
        self.models_dir = root / "models"
        self.models_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, run_id: str, **kw) -> argparse.Namespace:
        base = {
            "run_id": run_id, "force": False, "runs_dir": str(self.runs_dir),
            "tasks_dir": str(self.tasks_dir), "models_dir": str(self.models_dir),
            "retry_limit": None, "replicates": 1, "replicate": None,
            "prompt_variant": None, "planner": "raw", "judge": None,
            "no_judge": True, "no_judge_cache": False, "jev_assist": False,
            "dry_run": True, "json": False, "verbose": False, "group": None,
            "seed": None, "daily_cap": 0.0, "max_cost": 0.0,
            "allow_agent_exec": False,
        }
        base.update(kw)
        return argparse.Namespace(**base)

    def _orphan(self, *, stale: bool = True) -> str:
        run_dir = self.runs_dir / "orphee"
        run_dir.mkdir(parents=True)
        ev = run_dir / "events.jsonl"
        ev.write_text("{}\n", encoding="utf-8")
        started = datetime.now(UTC).isoformat()
        if stale:
            old = time.time() - STALE_RUNNING_SECONDS - 60
            os.utime(ev, (old, old))
            started = "2026-01-01T00:00:00+00:00"
        self.store.index_meta(_meta(
            run_id="orphee", status="running", started_at=started,
            run_dir=str(run_dir), passes=None, score=None,
            total_cost_usd=None, run_group="g:t:o/m:w/m:baseline",
            replicate=3,
            config={"seed": 42, "jev_assist": False, "planner": "raw"},
        ))
        return "orphee"

    def test_unknown_run_refused(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            harness.cmd_recover(self._args("nope"))

    def test_terminal_run_refused(self):
        self.store.index_meta(_meta(run_id="done", status="finished"))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            harness.cmd_recover(self._args("done"))

    def test_live_run_refused_without_force(self):
        rid = self._orphan(stale=False)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            harness.cmd_recover(self._args(rid))

    def test_already_aborted_refused(self):
        # a corpse keeps status="running" — the aborted annotation is the
        # record that its slot was handled; a second recover must not
        # launch a second replacement into it
        rid = self._orphan(stale=True)
        self.store.set_annotation(
            "run", rid, "aborted", note="superseded by earlier-recover")
        with (redirect_stdout(io.StringIO()),
              redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit)):
            harness.cmd_recover(self._args(rid))
        self.assertEqual(len(self.store.list_runs(limit=None)), 1)

    def test_stale_run_relaunches(self):
        rid = self._orphan(stale=True)
        buf = io.StringIO()
        fake = _meta(run_id="new1", run_dir="/tmp/new1")
        with (redirect_stdout(buf), redirect_stderr(io.StringIO()),
              patch.dict(os.environ, {"OPENROUTER_API_KEY": "x"}),
              patch("harness.Runner") as mock_runner):
            mock_runner.return_value.run.return_value = fake
            harness.cmd_recover(self._args(rid, dry_run=False))
        self.assertIn("Recovered", buf.getvalue())
        aborted = [a for a in self.store.annotations()
                   if a["kind"] == "run" and a["target"] == rid
                   and a["flag"] == "aborted"]
        self.assertEqual(len(aborted), 1)
        self.assertIn("new1", aborted[0]["note"])
        # the replacement carries the orphan's group, replicate, and seed
        kwargs = mock_runner.call_args.kwargs
        self.assertEqual(kwargs["run_group"], "g:t:o/m:w/m:baseline")
        self.assertEqual(kwargs["replicate"], 3)
        self.assertEqual(kwargs["seed"], 42)

    def test_dry_run_recover_leaves_slot_open(self):
        # a preview must not claim the slot — the aborted flag is the
        # cross-process record, and writing it would brick real recovery
        rid = self._orphan(stale=True)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            harness.cmd_recover(self._args(rid))  # args default dry_run=True
        aborted = [a for a in self.store.annotations()
                   if a["kind"] == "run" and a["target"] == rid
                   and a["flag"] == "aborted"]
        self.assertEqual(aborted, [])
        # a real recover still works afterwards — the preview did not
        # burn the slot
        buf = io.StringIO()
        fake = _meta(run_id="new1", run_dir="/tmp/new1")
        with (redirect_stdout(buf), redirect_stderr(io.StringIO()),
              patch.dict(os.environ, {"OPENROUTER_API_KEY": "x"}),
              patch("harness.Runner") as mock_runner):
            mock_runner.return_value.run.return_value = fake
            harness.cmd_recover(self._args(rid, dry_run=False))
        self.assertIn("Recovered", buf.getvalue())

    def test_failed_relaunch_reopens_slot(self):
        # a replacement that raises mid-run must release the aborted
        # claim — otherwise the orphan is permanently unrecoverable
        rid = self._orphan(stale=True)
        fake_err = RuntimeError("provider blew up")
        with (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
              patch.dict(os.environ, {"OPENROUTER_API_KEY": "x"}),
              patch("harness.Runner") as mock_runner,
              self.assertRaises(RuntimeError)):
            mock_runner.return_value.run.side_effect = fake_err
            harness.cmd_recover(self._args(rid, dry_run=False))
        flags = [a["flag"] for a in self.store.annotations()
                 if a["kind"] == "run" and a["target"] == rid]
        self.assertEqual(flags, [""])


class TestOrphanCostRepair(unittest.TestCase):
    """A3: killed runs meter $0 forever unless the calls ledger
    rehydrates their runs row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _corpse(self, rid: str = "dead") -> Path:
        run_dir = Path(self.tmp.name) / rid
        run_dir.mkdir()
        ev = run_dir / "events.jsonl"
        ev.write_text("{}\n", encoding="utf-8")
        old = time.time() - STALE_RUNNING_SECONDS - 60
        os.utime(ev, (old, old))
        self.store.index_meta(_meta(
            run_id=rid, status="running", started_at="2026-01-01T00:00:00+00:00",
            run_dir=str(run_dir), total_cost_usd=0.0,
            total_input_tokens=0, total_output_tokens=0,
        ))
        self.store.record_call(run_id=rid, phase="plan", step=1,
                               role="orchestrator", model="o/m",
                               input_tokens=100, output_tokens=200,
                               cost_usd=0.01, api_cost_usd=0.01,
                               pricing_source="api_reported")
        return run_dir

    def test_repair_rehydrates_totals(self):
        run_dir = self._corpse()
        self.assertTrue(self.store.repair_orphan_costs("dead"))
        meta = self.store.get_run("dead")
        self.assertEqual(meta.total_cost_usd, 0.01)
        self.assertEqual(meta.total_input_tokens, 100)
        self.assertEqual(meta.total_output_tokens, 200)
        # run.json reflects the repair for snapshot consumers
        on_disk = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(on_disk["total_cost_usd"], 0.01)
        # idempotent — no rewrite when the ledger already agrees
        self.assertFalse(self.store.repair_orphan_costs("dead"))

    def test_driver_repairs_orphan_before_marking(self):
        self._corpse()
        self.store.index_meta(_meta(
            run_id="dead", status="running", replicate=1,
            run_group="m:t:o/m:w/m:baseline",
            started_at="2026-01-01T00:00:00+00:00",
        ))
        spec = TaskSpec(id="t", type="html", prompt="p")
        launch = FakeLaunch(self.store)
        run_experiment(self.store, MATRIX, launch=launch,
                       tasks={"t": spec}, emit=lambda *_: None,
                       budget=0.0, daily_cap=0.0, batch_size=1,
                       diff_eps=0.0)
        meta = self.store.get_run("dead")
        self.assertEqual(meta.total_cost_usd, 0.01)


class TestMissingCostCount(unittest.TestCase):
    """A3: api_cost_usd NULL keyed against the pricing vocabulary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, rid: str, source: str | None, api_cost: float | None,
              *, dry_run: bool = False) -> None:
        self.store.record_call(run_id=rid, phase="plan", step=1,
                               role="orchestrator", model="o/m",
                               cost_usd=0.0 if api_cost is None else api_cost,
                               api_cost_usd=api_cost, pricing_source=source,
                               dry_run=dry_run)

    def test_vocabulary(self):
        self._call("r", "api_reported", 0.01)       # real cost — known
        self._call("r", "configured_estimate", None)  # estimate — missing
        self._call("r", "flat_estimate", None)        # estimate — missing
        self._call("r", "cli_reported", None)         # no usage.usd — missing
        self._call("r", "api", None)                  # no usage.cost — missing
        self._call("r", "api", 0.005)                 # usage.cost — known
        self._call("r", "unmetered", None)            # legitimately $0
        self._call("r", "none", None)                 # legitimately $0
        self._call("r", "configured_estimate", None, dry_run=True)  # excluded
        self.assertEqual(self.store.missing_cost_count(run_id="r"), 4)

    def test_group_prefix_scope(self):
        self.store.index_meta(_meta(run_id="a", run_group="m:t:o/m:w/m:baseline"))
        self.store.index_meta(_meta(run_id="b", run_group="m:t:o/m:w/m:jev"))
        self.store.index_meta(_meta(run_id="c", run_group="other:t:o/m:w/m:baseline"))
        for rid in ("a", "b", "c"):
            self._call(rid, "configured_estimate", None)
        self.assertEqual(
            self.store.missing_cost_count(group_prefix="m:t:o/m:w/m:"), 2)

    def test_grouped_counts_match_scalar_per_cell(self):
        # missing_cost_counts_by_group is the one-pass version the
        # experiment summary uses — it must agree with the scalar
        # predicate on the same pricing vocabulary
        self.store.index_meta(_meta(run_id="a", run_group="m:t:o/m:w/m:baseline"))
        self.store.index_meta(_meta(run_id="b", run_group="m:t:o/m:w/m:jev"))
        self.store.index_meta(_meta(run_id="c", run_group="m:u:o/m:w/m:baseline"))
        self.store.index_meta(_meta(run_id="d", run_group="other:t:o/m:w/m:baseline"))
        self._call("a", "configured_estimate", None)
        self._call("a", "api", 0.01)
        self._call("b", "flat_estimate", None)
        self._call("b", "unmetered", None)
        self._call("c", "cli_reported", None)
        self._call("d", "api", None)
        self._call("d", "none", None)
        counts = self.store.missing_cost_counts_by_group("m:")
        self.assertEqual(counts["m:t:o/m:w/m:baseline"], 1)
        self.assertEqual(counts["m:t:o/m:w/m:jev"], 1)
        self.assertEqual(counts["m:u:o/m:w/m:baseline"], 1)
        self.assertNotIn("other:t:o/m:w/m:baseline", counts)
        # and the experiment's per-cell attribution over it agrees with
        # the scalar count
        per_cell = sum(
            n for g, n in counts.items() if g.startswith("m:t:o/m:w/m:")
        )
        self.assertEqual(
            per_cell,
            self.store.missing_cost_count(group_prefix="m:t:o/m:w/m:"))


class TestCellState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_partial_done(self):
        self.assertEqual(
            cell_state(self.store, "m", CELL, REP_FLOOR, 0.15), "pending")
        _seed_arm(self.store, "baseline", 2)
        _seed_arm(self.store, "jev", 2)
        self.assertEqual(
            cell_state(self.store, "m", CELL, REP_FLOOR, 0.15), "partial")
        _seed_arm(self.store, "baseline", REP_FLOOR - 2, offset=2)
        _seed_arm(self.store, "jev", REP_FLOOR - 2, offset=2)
        self.assertEqual(
            cell_state(self.store, "m", CELL, REP_FLOOR, 0.15), "done")


if __name__ == "__main__":
    unittest.main()
