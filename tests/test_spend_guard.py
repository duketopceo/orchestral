"""Tests for the harness spend guard: --daily-cap and --max-cost."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from datetime import UTC, datetime

import harness
from orchestral.storage import RunMeta, RunStore


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t", "worker": "w/m",
        "status": "finished", "started_at": datetime.now(UTC).isoformat(),
        "total_cost_usd": 0.01, "total_input_tokens": 10, "total_output_tokens": 20,
        "latency_ms": 100.0, "passes": True, "score": 0.8, "run_group": "g",
    }
    base.update(kw)
    return RunMeta(**base)


def _args(**kw) -> argparse.Namespace:
    base = {"dry_run": False, "daily_cap": 0.0, "max_cost": 0.0}
    base.update(kw)
    return argparse.Namespace(**base)


class TestSpendMeters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_spend_today_counts_todays_runs(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=0.30))
        self.store.index_meta(_meta(run_id="b", total_cost_usd=0.20))
        self.store.index_meta(_meta(run_id="old", total_cost_usd=99.0,
                                    started_at="2020-01-01T00:00:00"))
        self.assertAlmostEqual(self.store.spend_today(), 0.50)

    def test_mean_run_cost_scopes_to_pairing(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=0.02,
                                    orchestrator="o/x", worker="w/y"))
        self.store.index_meta(_meta(run_id="b", total_cost_usd=0.04,
                                    orchestrator="o/x", worker="w/y"))
        self.store.index_meta(_meta(run_id="c", total_cost_usd=0.60,
                                    orchestrator="o/z", worker="w/z"))
        self.assertAlmostEqual(
            self.store.mean_run_cost(orchestrator="o/x", worker="w/y"), 0.03)
        self.assertAlmostEqual(self.store.mean_run_cost(), 0.22)

    def test_mean_run_cost_ignores_unfinished(self):
        self.store.index_meta(_meta(run_id="a", status="failed", total_cost_usd=99.0))
        self.assertIsNone(self.store.mean_run_cost())

    def test_meters_ignore_dry_run_rows(self):
        """Dry runs are free exercises — they must not inflate the daily
        spend meter or the per-run cost estimate used for grid pricing."""
        self.store.index_meta(_meta(run_id="dry", total_cost_usd=99.0, dry_run=True))
        self.store.index_meta(_meta(run_id="real", total_cost_usd=0.05))
        self.assertAlmostEqual(self.store.spend_today(), 0.05)
        self.assertAlmostEqual(self.store.mean_run_cost(), 0.05)


class TestBudgetCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_caps_passes(self):
        harness._budget_check(_args(), self.store, 100)  # no raise

    def test_dry_run_never_blocks(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=50.0))
        harness._budget_check(_args(dry_run=True, daily_cap=0.01), self.store, 1)

    def test_daily_cap_blocks_when_spent(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=6.0))
        with self.assertRaises(SystemExit):
            harness._budget_check(_args(daily_cap=5.0), self.store, 1)

    def test_daily_cap_blocks_projected_estimate(self):
        # $4.50 spent + 200-run grid at $0.01 mean → $6.50 projected > $5 cap
        self.store.index_meta(_meta(run_id="a", total_cost_usd=4.50))
        with self.assertRaises(SystemExit):
            harness._budget_check(_args(daily_cap=5.0), self.store, 200)

    def test_max_cost_blocks_big_estimate(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=0.05))
        with self.assertRaises(SystemExit):
            harness._budget_check(_args(max_cost=0.10), self.store, 10)  # est $0.50

    def test_small_estimate_passes(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=0.005))
        harness._budget_check(_args(daily_cap=5.0, max_cost=1.0), self.store, 12)

    def test_pairing_scoped_mean_wins_over_global(self):
        # the pairing's own history is cheap even though the global mean
        # is dragged up by an expensive pairing
        self.store.index_meta(_meta(run_id="big", total_cost_usd=10.0,
                                    orchestrator="o/z", worker="w/z"))
        self.store.index_meta(_meta(run_id="cheap", total_cost_usd=0.01,
                                    orchestrator="o/x", worker="w/y"))
        # 10 launches × scoped mean $0.01 = $0.10 < $0.50 → passes
        harness._budget_check(_args(max_cost=0.50), self.store, 10,
                              orchestrator="o/x", worker="w/y")
        # the same launch count at the ~$5 global mean → blocked
        with self.assertRaises(SystemExit):
            harness._budget_check(_args(max_cost=0.50), self.store, 10)

    def test_scoped_mean_falls_back_to_global(self):
        # no history for the pairing → global mean, then the $0.01 default
        self.store.index_meta(_meta(run_id="a", total_cost_usd=10.0,
                                    orchestrator="o/z", worker="w/z"))
        with self.assertRaises(SystemExit):
            harness._budget_check(_args(max_cost=0.50), self.store, 10,
                                  orchestrator="o/new", worker="w/new")


class TestPerCellRecheck(unittest.TestCase):
    """Grids/batches re-check spend_today between sequential cell launches —
    the aggregate pre-check can't see spend that lands mid-run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_spend_recheck_blocks_at_cap(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=6.0))
        with self.assertRaises(SystemExit):
            harness._spend_recheck(_args(daily_cap=5.0), self.store)

    def test_spend_recheck_off_without_cap_or_on_dry_run(self):
        self.store.index_meta(_meta(run_id="a", total_cost_usd=6.0))
        harness._spend_recheck(_args(daily_cap=0.0), self.store)
        harness._spend_recheck(_args(daily_cap=5.0, dry_run=True), self.store)

    def test_grid_aborts_remaining_cells_when_cap_crossed(self):
        """The first cell's cost indexes before the second launches — the
        next cell's recheck must stop the grid instead of overshooting."""
        import os
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        root = Path(self.tmp.name)
        (root / "tasks").mkdir()
        (root / "models").mkdir()
        (root / "tasks" / "t.yaml").write_text("id: t\ntype: html\nprompt: p\n")
        (root / "models" / "m.yaml").write_text(
            "models:\n"
            "  - slug: o/m\n    name: o\n    role: orchestrator\n"
            "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
            "  - slug: w/m\n    name: w\n    role: worker\n"
            "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
        )
        calls: list[int] = []

        class SpendyRunner:
            def __init__(self, **kw):
                pass

            def run(self, task, orch, worker, judge):
                calls.append(1)
                # the cell's spend lands in the index before the next launch
                self_store = RunStore(root / "runs")
                self_store.index_meta(_meta(
                    run_id=f"r{len(calls)}", total_cost_usd=6.0))
                return MagicMock(
                    run_id=f"r{len(calls)}", replicate=None, passes=True,
                    score=None, total_cost_usd=6.0,
                    total_input_tokens=1, total_output_tokens=1)

        args = _args(
            daily_cap=5.0, task="t",
            runs_dir=str(root / "runs"), tasks_dir=str(root / "tasks"),
            models_dir=str(root / "models"),
            orchestrators="o/m", workers="w/m", jobs=1, replicates=3,
            planner="raw", judge=None, no_judge_cache=False,
            retry_limit=None, prompt_variant=None, json=False,
            verbose=False, group=None, replicate=None, seed=None,
            allow_agent_exec=False,
        )
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "x"}):
            with patch.object(harness, "Runner", SpendyRunner):
                with self.assertRaises(SystemExit):
                    harness.cmd_grid(args)
        self.assertEqual(calls, [1])  # cell 2 never launched


if __name__ == "__main__":
    unittest.main()
