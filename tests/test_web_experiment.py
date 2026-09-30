"""Tests for the observatory experiment payload (web/state.py::experiment_payload)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.storage import RunMeta, RunStore
from orchestral.web.state import experiment_payload


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t", "worker": "w/m",
        "status": "finished", "started_at": "2026-01-01T00:00:00",
        "total_cost_usd": 0.01, "latency_ms": 100.0, "passes": True,
        "run_group": "g", "config": {}, "run_dir": "",
    }
    base.update(kw)
    return RunMeta(**base)


class TestExperimentPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RunStore(self.root / "runs")
        self.matrix_path = self.root / "experiments" / "m.yaml"
        self.matrix_path.parent.mkdir()
        self.matrix_path.write_text(
            "name: m\norchestrators: [o/m]\nworkers: [w/m]\ntasks: [t]\n",
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_dir_with_events(self, run_id: str, events: list[dict]) -> str:
        d = self.root / "runs" / "o/m" / "t" / "w/m" / run_id
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return str(d)

    def _seed(self, arm: str, n: int, *, passes: bool = True) -> None:
        jev = arm == "jev"
        for i in range(1, n + 1):
            rid = f"m-{arm}-{i}"
            run_dir = ""
            if jev and i == 1:
                run_dir = self._run_dir_with_events(rid, [
                    {"type": "run.started"},
                    {"type": "jev_replan"},
                    {"type": "jev_rework"},
                    {"type": "run.finished"},
                ])
            self.store.index_meta(_meta(
                run_id=rid, passes=passes, run_dir=run_dir,
                run_group=f"m:t:o/m:w/m:{arm}", replicate=i,
                config={"jev_assist": jev},
            ))

    def test_missing_matrix_returns_none(self):
        self.assertIsNone(
            experiment_payload(self.store, self.root / "experiments" / "nope.yaml"))

    def test_payload_shape_and_self_judge_label(self):
        self._seed("baseline", 3, passes=False)
        self._seed("jev", 3, passes=True)
        p = experiment_payload(self.store, self.matrix_path)
        assert p is not None
        self.assertEqual(p["matrix"], "m")
        self.assertEqual(p["primary_axis"], "mechanical pass")
        self.assertTrue(any("self-referential" in c for c in p["caveats"]))
        self.assertEqual(len(p["cells"]), 1)

        cell = p["cells"][0]
        self.assertEqual(cell["cell"], "t:o/m:w/m")
        self.assertEqual(cell["baseline"]["passes"], 0)
        self.assertEqual(cell["baseline"]["n"], 3)
        self.assertEqual(cell["jev"]["passes"], 3)
        self.assertIsNotNone(cell["baseline"]["ci"])
        # difference CI present and pointing at lift
        self.assertIsNotNone(cell["diff_ci"])
        self.assertGreater(cell["diff_ci"][0], 0)
        self.assertEqual(cell["verdict"], "lift")

    def test_jev_intervention_counts(self):
        self._seed("baseline", 1)
        self._seed("jev", 1)
        p = experiment_payload(self.store, self.matrix_path)
        assert p is not None
        iv = p["cells"][0]["jev"]["interventions"]
        self.assertEqual(iv, {"replan": 1, "rework": 1})
        # the baseline arm never carries intervention fields
        self.assertNotIn("interventions", p["cells"][0]["baseline"])

    def test_cost_per_pass_per_arm(self):
        self._seed("baseline", 2, passes=True)
        self._seed("jev", 2, passes=True)
        p = experiment_payload(self.store, self.matrix_path)
        assert p is not None
        self.assertAlmostEqual(p["cells"][0]["jev"]["cost_per_pass"], 0.01)
        self.assertAlmostEqual(p["cells"][0]["jev"]["cost"], 0.02)


if __name__ == "__main__":
    unittest.main()
