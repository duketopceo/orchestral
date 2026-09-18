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


if __name__ == "__main__":
    unittest.main()
