"""A top-level JSON list from the orchestrator is normalised, not rejected.

Measured failure: the cheapest pairing failed 4 of 16 runs on `api-order-lookup`
with "orchestrator plan must be a JSON object, got list" because the task asks
for "ONLY the JSON list" and the orchestrator obeyed it literally. A list of
step objects is a plan; a list of API calls is the orchestrator answering the
task itself. Scalars and empty lists stay malformed output.
"""

from __future__ import annotations

import json
import tempfile
import unittest

from orchestral.config import ModelConfig, TaskSpec
from orchestral.logger import EventLogger
from orchestral.planners import PlanError, normalize_plan, plan_raw


class _Client:
    def __init__(self, content: str) -> None:
        self._content = content

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        return {
            "content": self._content,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1,
            "id": "fake",
        }

    def close(self):
        pass


def _model() -> ModelConfig:
    return ModelConfig(
        slug="o/x", name="o/x", role="orchestrator",
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
    )


def _plan(content: str):
    task = TaskSpec(id="t", type="api", prompt="p")
    with tempfile.TemporaryDirectory() as tmp:
        logger = EventLogger(tmp, "run")
        return plan_raw(
            logger=logger, task=task, orchestrator=_model(), step=0,
            client=_Client(content), dry_run=False,
        )[0]


class TestNormalizePlan(unittest.TestCase):
    def test_dict_passes_through(self):
        plan = {"subtasks": [{"id": "a"}]}
        self.assertIs(normalize_plan(plan), plan)

    def test_list_of_steps_becomes_subtasks(self):
        steps = [{"id": "s1", "description": "do a"}, {"id": "s2", "description": "do b"}]
        self.assertEqual(normalize_plan(steps), {"subtasks": steps})

    def test_list_of_strings_becomes_described_subtasks(self):
        self.assertEqual(
            normalize_plan(["do a", "do b"]),
            {"subtasks": [{"id": "s0", "description": "do a"}, {"id": "s1", "description": "do b"}]},
        )

    def test_list_of_api_calls_is_a_self_executed_answer(self):
        calls = [{"method": "GET", "path": "/health"}, {"method": "GET", "path": "/users/42"}]
        out = normalize_plan(calls)
        self.assertNotIn("subtasks", out)
        self.assertEqual(json.loads(out["content"]), calls)

    def test_empty_list_is_malformed(self):
        with self.assertRaises(PlanError):
            normalize_plan([])

    def test_scalar_is_malformed(self):
        with self.assertRaises(PlanError):
            normalize_plan(42)

    def test_mixed_list_is_malformed(self):
        with self.assertRaises(PlanError):
            normalize_plan([{"id": "a"}, 3])


class TestPlanRawAcceptsList(unittest.TestCase):
    def test_step_list(self):
        plan = _plan('[{"id": "s1", "description": "x"}]')
        self.assertEqual(plan["subtasks"], [{"id": "s1", "description": "x"}])
        self.assertEqual(plan["planner"], "raw")
        self.assertEqual(plan["task_id"], "t")

    def test_call_list(self):
        plan = _plan('[{"method": "GET", "path": "/health"}]')
        self.assertEqual(json.loads(plan["content"]), [{"method": "GET", "path": "/health"}])

    def test_scalar_still_fails(self):
        with self.assertRaises(PlanError):
            _plan("[1, 2]")


if __name__ == "__main__":
    unittest.main()
