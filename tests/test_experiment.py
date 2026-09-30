"""Tests for the batched A/B experiment driver (orchestral/experiment.py)."""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import harness
from orchestral.config import TaskSpec
from orchestral.experiment import (
    REP_CAP,
    REP_FLOOR,
    Cell,
    Matrix,
    cell_runs,
    cell_state,
    estimate_pair_cost,
    load_matrix,
    rep_target,
    resolve_matrix_tasks,
    run_experiment,
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
                 fail: set[tuple[str, int]] | None = None):
        self.store = store
        self.passes = passes
        self.cost = cost
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
                               role="orchestrator", model="o/m", cost_usd=self.cost)
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

    def test_resume_skips_done_cells(self):
        _seed_arm(self.store, "baseline", REP_FLOOR)
        _seed_arm(self.store, "jev", REP_FLOOR)
        launch = FakeLaunch(self.store)
        out = self._run(launch, budget=100.0, daily_cap=0.0, diff_eps=0.0)
        self.assertEqual(launch.launches, [])
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
