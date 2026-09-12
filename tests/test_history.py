"""Tests for per-model history aggregation and the dashboard scatter."""

from __future__ import annotations

import unittest

from orchestral.reporter import _dashboard_html, model_history
from orchestral.storage import RunMeta


def _meta(rid: str, orch: str, worker: str, *, cost: float, score=None, passes=True, judge=None, status="finished"):
    return RunMeta(
        run_id=rid, orchestrator=orch, task_id="t", worker=worker, status=status,
        started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:01:00Z",
        total_cost_usd=cost, total_input_tokens=10, total_output_tokens=20,
        score=score, passes=passes, run_dir="/tmp/x",
        config={"judge": judge, "planner": "raw"},
    )


class TestModelHistory(unittest.TestCase):
    def test_aggregates_by_role(self):
        runs = [
            _meta("a", "o1", "w1", cost=0.01, score=0.8, judge="j1"),
            _meta("b", "o1", "w1", cost=0.03, score=None, passes=False, judge="j1"),
            _meta("c", "o2", "w2", cost=0.10, score=0.9, status="failed"),
        ]
        h = model_history(runs)
        self.assertEqual(set(h), {"orchestrator", "worker", "judge"})
        o1 = h["orchestrator"]["o1"]
        self.assertEqual(o1["runs"], 2)
        self.assertAlmostEqual(o1["pass_rate"], 0.5)
        self.assertAlmostEqual(o1["avg_score"], 0.8)  # only scored run counts
        self.assertAlmostEqual(o1["avg_cost"], 0.02)
        self.assertAlmostEqual(o1["total_cost"], 0.04)
        # failed run excluded everywhere
        self.assertNotIn("o2", h["orchestrator"])
        # judge attribution from config
        self.assertEqual(h["judge"]["j1"]["runs"], 2)

    def test_empty(self):
        h = model_history([])
        self.assertEqual(h, {"orchestrator": {}, "worker": {}, "judge": {}})


class TestDashboardScatter(unittest.TestCase):
    def test_dashboard_contains_scatter_and_history(self):
        runs = [
            _meta("a", "o1", "w1", cost=0.01, score=0.8),
            _meta("b", "o2", "w2", cost=0.02, score=None, passes=False),
        ]
        summary = {"runs": 2, "total_cost_usd": 0.03, "total_tokens": 60}
        page = _dashboard_html(runs, summary)
        self.assertIn("Cost vs quality", page)
        self.assertIn("<svg", page)
        self.assertIn("<circle", page)
        self.assertIn("Orchestrator history", page)
        self.assertIn("Worker history", page)
        self.assertIn("o1", page)

    def test_dashboard_empty_index(self):
        page = _dashboard_html([], {"runs": 0, "total_cost_usd": 0.0, "total_tokens": 0})
        self.assertIn("No finished runs yet.", page)


class TestHistoryCommand(unittest.TestCase):
    def _seed(self, tmp):
        from orchestral.storage import RunStore
        store = RunStore(tmp)
        base = {            "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:01:00Z",
            "total_input_tokens": 10, "total_output_tokens": 20, "run_dir": "/tmp/x",}
        store.index_meta(RunMeta(run_id="a", orchestrator="o1", task_id="t", worker="w1",
                                 status="finished", total_cost_usd=0.01, score=0.8, passes=True,
                                 config={"judge": "j1"}, **base))
        store.index_meta(RunMeta(run_id="b", orchestrator="o1", task_id="t", worker="w1",
                                 status="finished", total_cost_usd=0.03, score=None, passes=False,
                                 config={"judge": "j1"}, **base))
        store.index_meta(RunMeta(run_id="c", orchestrator="o2", task_id="t", worker="w2",
                                 status="finished", total_cost_usd=0.10, score=0.9, passes=True,
                                 config={}, **base))

    def test_history_cli_prints_and_filters(self):
        import argparse
        import io
        import tempfile
        from contextlib import redirect_stdout

        import harness

        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            args = argparse.Namespace(orchestrator=None, worker=None, runs_dir=tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                harness.cmd_history(args)
            text = out.getvalue()
            self.assertIn("Orchestrator history", text)
            self.assertIn("o1", text)
            self.assertIn("o2", text)
            self.assertIn("Judge history", text)
            self.assertIn("j1", text)

            args.orchestrator = "o1"
            out = io.StringIO()
            with redirect_stdout(out):
                harness.cmd_history(args)
            text = out.getvalue()
            self.assertIn("o1", text)
            self.assertNotIn("o2", text)


if __name__ == "__main__":
    unittest.main()
