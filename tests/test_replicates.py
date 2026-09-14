"""Tests for replicate runs and variance aggregation (piece 2)."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import harness
from orchestral.stats import aggregate, mean, percentile, stdev
from orchestral.storage import RunMeta, RunStore


class TestStatsHelpers(unittest.TestCase):
    def test_mean_stdev_edges(self):
        self.assertEqual(mean([]), 0.0)
        self.assertEqual(mean([2.0, 4.0]), 3.0)
        self.assertEqual(stdev([]), 0.0)
        self.assertEqual(stdev([5.0]), 0.0)  # n<2 -> 0, not an exception
        self.assertAlmostEqual(stdev([1.0, 2.0, 3.0]), 1.0)

    def test_percentile(self):
        self.assertEqual(percentile([], 50), 0.0)
        self.assertEqual(percentile([7.0], 95), 7.0)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(percentile(list(range(1, 101)), 95), 95.05)


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t", "worker": "w/m",
        "status": "finished", "started_at": "2026-01-01T00:00:00",
        "total_cost_usd": 0.01, "total_input_tokens": 10, "total_output_tokens": 20,
        "latency_ms": 100.0, "passes": True, "score": 0.8, "run_group": "g",
    }
    base.update(kw)
    return RunMeta(**base)


class TestAggregate(unittest.TestCase):
    def test_cell_stats(self):
        runs = [
            _meta(run_id="a", passes=True, score=0.8, total_cost_usd=0.01, latency_ms=100),
            _meta(run_id="b", passes=False, score=0.4, total_cost_usd=0.03, latency_ms=300),
            _meta(run_id="c", status="failed", passes=None, score=None,
                  failure_reason="exception:timeout", total_cost_usd=0.005, latency_ms=50),
        ]
        cells = aggregate(runs)
        self.assertEqual(len(cells), 1)
        c = cells[0]
        self.assertEqual(c.runs, 3)
        self.assertEqual(c.finished, 2)
        self.assertEqual(c.passed, 1)
        self.assertAlmostEqual(c.pass_rate, 1 / 3)  # failures count against
        self.assertAlmostEqual(c.score_mean, 0.6)   # over scored runs only
        self.assertAlmostEqual(c.cost_total, 0.045)
        self.assertAlmostEqual(c.cost_mean, 0.015)
        self.assertAlmostEqual(c.latency_p50, 100.0)
        self.assertEqual(c.failures, {"exception:timeout": 1})
        self.assertAlmostEqual(c.successes_per_dollar, 1 / 0.045)

    def test_cells_keyed_by_group(self):
        runs = [
            _meta(run_id="a", run_group="g1"),
            _meta(run_id="b", run_group="g2"),
            _meta(run_id="c", run_group=None),
        ]
        self.assertEqual(len(aggregate(runs)), 3)
        self.assertEqual(len(aggregate(runs, by_group=False)), 1)

    def test_no_cost_no_spd(self):
        c = aggregate([_meta(total_cost_usd=0.0)])[0]
        self.assertIsNone(c.successes_per_dollar)


def _args(**over):
    base = {
        "task": "landing-page-coffee",
        "orchestrator": "deepseek/deepseek-v4-flash-0731",
        "worker": "z-ai/glm-5.3-flash",
        "planner": "raw",
        "judge": None,
        "no_judge_cache": False,
        "retry_limit": None,
        "prompt_variant": None,
        "jobs": 1,
        "dry_run": True,
        "json": False,
        "verbose": False,
        "group": None,
        "replicate": None,
        "replicates": 1,
        "seed": None,
        "runs_dir": None,
        "tasks_dir": "tasks",
        "models_dir": "models",
    }
    base.update(over)
    return argparse.Namespace(**base)


class TestReplicateRuns(unittest.TestCase):
    def test_replicates_share_group_index_and_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(replicates=3, seed=42, runs_dir=tmp)
            with redirect_stdout(io.StringIO()):
                harness.cmd_run(args)
            runs = RunStore(tmp).list_runs(limit=None)
            self.assertEqual(len(runs), 3)
            groups = {r.run_group for r in runs}
            self.assertEqual(len(groups), 1)
            self.assertTrue(next(iter(groups)).startswith("rep-"))
            self.assertEqual(sorted(r.replicate for r in runs), [1, 2, 3])
            # --seed 42 -> replicates record 42, 43, 44
            self.assertEqual(sorted(r.config["seed"] for r in runs), [42, 43, 44])

    def test_explicit_group_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(replicates=2, group="exp1", runs_dir=tmp)
            with redirect_stdout(io.StringIO()):
                harness.cmd_run(args)
            runs = RunStore(tmp).list_runs(limit=None)
            self.assertEqual({r.run_group for r in runs}, {"exp1"})

    def test_replicate_and_replicates_conflict(self):
        args = _args(replicates=2, replicate=1, runs_dir=tempfile.mkdtemp())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            harness.cmd_run(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_zero_replicates_rejected(self):
        args = _args(replicates=0, runs_dir=tempfile.mkdtemp())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            harness.cmd_run(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_grid_replicates_expand_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(replicates=2, runs_dir=tmp)
            args.orchestrators = "deepseek/deepseek-v4-flash-0731"
            args.workers = "z-ai/glm-5.3-flash"
            with redirect_stdout(io.StringIO()):
                harness.cmd_grid(args)
            runs = RunStore(tmp).list_runs(limit=None)
            self.assertEqual(len(runs), 2)
            self.assertEqual(sorted(r.replicate for r in runs), [1, 2])

    def test_report_groups_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()):
                harness.cmd_run(_args(replicates=3, group="exp9", runs_dir=tmp))
            args = _args(runs_dir=tmp)
            args.html = False
            args.pairings = False
            args.groups = True
            args.group = None
            args.sort = "started_at"
            args.desc = True
            args.task = None
            args.limit = None
            out = io.StringIO()
            with redirect_stdout(out):
                harness.cmd_report(args)
            text = out.getvalue()
            self.assertIn("exp9", text)
            self.assertIn("landing-page-coffee", text)
            self.assertIn("±", text.replace("&plusmn;", "±"))

    def test_failed_replicate_does_not_stop_rest(self):
        """Runner.run re-raises after recording the failure; a mid-loop
        flake must not lose the remaining replicates."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            real_runner = harness.Runner

            class FlakyRunner(real_runner):
                def run(self, *a, **kw):
                    if self.replicate == 2:
                        raise RuntimeError("boom")
                    return super().run(*a, **kw)

            args = _args(replicates=3, group="flaky", runs_dir=tmp)
            with patch.object(harness, "Runner", FlakyRunner), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as ctx:
                harness.cmd_run(args)
            self.assertEqual(ctx.exception.code, 1)
            runs = RunStore(tmp).list_runs(run_group="flaky")
            self.assertEqual(sorted(r.replicate for r in runs), [1, 3])

    def test_report_group_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()):
                harness.cmd_run(_args(replicates=2, group="keep", runs_dir=tmp))
            runs = RunStore(tmp).list_runs(run_group="keep")
            self.assertEqual(len(runs), 2)
            self.assertEqual(RunStore(tmp).list_runs(run_group="nope"), [])


if __name__ == "__main__":
    unittest.main()
