"""Tests for per-model history aggregation and the dashboard scatter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestral.reporter import _dashboard_html, model_history
from orchestral.storage import RunMeta, RunStore


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


if __name__ == "__main__":
    unittest.main()
