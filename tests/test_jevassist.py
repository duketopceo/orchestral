"""Jev-in-the-loop: decisions-engine assists inside the run's decision loop.

Gates are advisory — a broken or silent advisor degrades to inconclusive and
the run proceeds unmodified; verdicts can only improve inputs (one replan /
one rework max), never fail a run.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.jevassist import output_gate, plan_gate
from orchestral.logger import EventLogger
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role, retry_limit=0,
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
    )


JEV = _model("~typesafe/jev-latest", "judge")
TASK = TaskSpec(id="t1", type="html", prompt="make a page", validation=["non_empty"])
GOOD_HTML = "<html><title>t</title><body><h1>x</h1></body></html>"


def _decide(answers: dict) -> dict:
    return {"answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0001},
            "latency_ms": 3, "id": "d1"}


class _Client:
    """Provider-shaped fake with a canned decide queue."""

    def __init__(self, decides: list[dict] | None = None) -> None:
        self.decides = list(decides or [])
        self.calls = 0

    def decide(self, *, model: str, state: object, questions: dict) -> dict:
        self.calls += 1
        self.last_state = state
        self.last_questions = questions
        if self.decides:
            return self.decides.pop(0)
        return {"answers": {}, "usage": {}}

    def chat(self, **kw) -> dict:
        return {}

    def close(self) -> None:
        pass


class TestPlanGate(unittest.TestCase):
    def _gate(self, decides):
        with tempfile.TemporaryDirectory() as tmp, EventLogger(Path(tmp)) as logger:
            client = _Client(decides)
            out = plan_gate(
                logger=logger, step=2, task=TASK,
                plan={"subtasks": [{"id": 0, "description": "s"}]},
                judge=JEV, client=client)
            return out, client

    def test_sound_plan(self):
        out, _ = self._gate([_decide({"sound": {"noul": 0.9},
                                    "coverage": {"score": 4}})])
        self.assertTrue(out["sound"])
        self.assertIsNone(out["replan_note"])
        self.assertEqual(len(out["costs"]), 1)

    def test_unsound_plan_requests_replan(self):
        out, _ = self._gate([_decide({"sound": {"noul": 0.2},
                                    "coverage": {"score": 1}})])
        self.assertFalse(out["sound"])
        self.assertIn("plan critic", out["replan_note"])

    def test_null_noul_is_inconclusive(self):
        out, _ = self._gate([_decide({"sound": {"noul": None},
                                    "coverage": {"score": 2}})])
        self.assertIsNone(out["sound"])
        self.assertIsNone(out["replan_note"])

    def test_broken_advisor_degrades_not_raises(self):
        client = _Client()
        client.decide = MagicMock(side_effect=RuntimeError("decisions down"))
        with tempfile.TemporaryDirectory() as tmp, EventLogger(Path(tmp)) as logger:
            out = plan_gate(
                logger=logger, step=2, task=TASK, plan={"subtasks": []},
                judge=JEV, client=client)
        self.assertTrue(out["record"]["inconclusive"])
        self.assertIn("decisions down", out["record"]["error"])
        self.assertIsNone(out["sound"])


class TestOutputGate(unittest.TestCase):
    SUBTASKS: ClassVar = [{"id": 0, "description": "part a"},
                          {"id": 1, "description": "part b"}]
    RESULTS: ClassVar = [{"content": "a"}, {"content": "b"}]

    def _gate(self, decides):
        with tempfile.TemporaryDirectory() as tmp, EventLogger(Path(tmp)) as logger:
            client = _Client(decides)
            return output_gate(
                logger=logger, step=3, task=TASK,
                subtasks=self.SUBTASKS, results=self.RESULTS,
                judge=JEV, client=client), client

    def test_adequate_outputs(self):
        out, _ = self._gate([_decide({"adequate": {"noul": 0.8},
                                     "weakest": {"answer": "1"}})])
        self.assertTrue(out["adequate"])
        self.assertEqual(out["weakest"], 1)

    def test_inadequate_names_weakest(self):
        out, _ = self._gate([_decide({"adequate": {"noul": 0.1},
                                     "weakest": {"answer": "0"}})])
        self.assertFalse(out["adequate"])
        self.assertEqual(out["weakest"], 0)

    def test_out_of_range_weakest_rejected(self):
        out, _ = self._gate([_decide({"adequate": {"noul": 0.1},
                                     "weakest": {"answer": "9"}})])
        self.assertFalse(out["adequate"])
        self.assertIsNone(out["weakest"])


class TestRunnerAssist(unittest.TestCase):
    """The gates fire inside run() only under --jev-assist and a decisions judge."""

    def _run(self, decides, jev_assist=True):
        tmp = tempfile.mkdtemp()
        orch = MagicMock()
        orch.chat.side_effect = [
            {"content": json.dumps({"subtasks": [{"id": 0, "description": "s"}]}),
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "latency_ms": 1},
            {"content": json.dumps({"subtasks": [{"id": 0, "description": "s2"}]}),
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "latency_ms": 1},
            {"content": GOOD_HTML,
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "latency_ms": 1},
        ]
        worker = MagicMock()
        worker.chat.side_effect = [
            {"content": "section-one",
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "latency_ms": 1},
            {"content": "section-reworked",
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "latency_ms": 1},
        ]
        judge = _Client(decides)
        store = RunStore(tmp)
        runner = Runner(runs_dir=tmp, store=store, jev_assist=jev_assist,
                        clients={"orchestrator": orch, "worker": worker,
                                 "judge": judge})
        meta = runner.run(TASK, _model("o/m", "orchestrator"),
                          _model("w/m", "worker"), judge=JEV)
        events = [
            json.loads(line) for line in
            (Path(meta.run_dir) / "events.jsonl").read_text().splitlines() if line.strip()
        ]
        return meta, events, orch, worker, judge

    def test_unsound_plan_triggers_one_replan(self):
        meta, events, orch, _, _ = self._run([
            _decide({"sound": {"noul": 0.1}, "coverage": {"score": 1}}),   # plan gate
            _decide({"adequate": {"noul": 0.9}, "weakest": {"answer": "0"}}),  # output gate
            _decide({"verdict": {"noul": 0.9}, "quality": {"score": 4}}),  # judge
        ])
        plan = json.loads((Path(meta.run_dir) / "plan.json").read_text())
        self.assertTrue(plan["jev_replan"])
        self.assertEqual(plan["subtasks"][0]["description"], "s2")
        # plan call + replan call + assemble call
        self.assertEqual(orch.chat.call_count, 3)
        self.assertIn("jev_replan", {e.get("type") for e in events})

    def test_sound_plan_no_replan(self):
        meta, _, orch, _, _ = self._run([
            _decide({"sound": {"noul": 0.95}, "coverage": {"score": 4}}),
            _decide({"adequate": {"noul": 0.9}, "weakest": {"answer": "0"}}),
            _decide({"verdict": {"noul": 0.9}, "quality": {"score": 4}}),
        ])
        plan = json.loads((Path(meta.run_dir) / "plan.json").read_text())
        self.assertNotIn("jev_replan", plan)
        self.assertEqual(orch.chat.call_count, 2)

    def test_inadequate_outputs_rework_weakest(self):
        _, events, _, worker, _ = self._run([
            _decide({"sound": {"noul": 0.9}, "coverage": {"score": 4}}),
            _decide({"adequate": {"noul": 0.2}, "weakest": {"answer": "0"}}),
            _decide({"verdict": {"noul": 0.9}, "quality": {"score": 4}}),
        ])
        self.assertEqual(worker.chat.call_count, 2)
        self.assertIn("jev_rework", {e.get("type") for e in events})

    def test_assist_off_no_gate_calls(self):
        _, _, _, _, judge = self._run(
            [_decide({"verdict": {"noul": 0.9}, "quality": {"score": 4}})],
            jev_assist=False)
        # only the judge's own decide call ran — no gates
        self.assertEqual(judge.calls, 1)


if __name__ == "__main__":
    unittest.main()
