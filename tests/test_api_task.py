"""Tests for the api task type — local stub replay validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.apistub import check_api, parse_plan
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner

STUB = [
    {"method": "GET", "path": "/health", "status": 200, "json": {"ok": True}},
    {"method": "GET", "path": "/users/42", "status": 200, "json": {"id": 42}},
    {"method": "POST", "path": "/orders", "status": 201, "json": {"order_id": 1}},
    {"method": "GET", "path": "/orders", "status": 200, "json": []},
]
CALLS = [
    {"method": "GET", "path": "/health"},
    {"method": "GET", "path": "/users/42"},
    {"method": "POST", "path": "/orders", "json": {"user_id": 42, "sku": "MUG-11", "qty": 2}},
    {"method": "GET", "path": "/orders", "params": {"user_id": 42}},
]


def _metadata(**overrides):
    md = {"stub": STUB, "calls": CALLS, "strict": True}
    md.update(overrides)
    return md


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "api-test",
        "type": "api",
        "prompt": "Plan the API calls.",
        "metadata": _metadata(),
    }
    base.update(kwargs)
    return TaskSpec(**base)


class TestParsePlan(unittest.TestCase):
    def test_raw_list(self):
        plan = parse_plan('[{"method": "GET", "path": "/x"}]')
        self.assertEqual(plan[0]["path"], "/x")

    def test_fenced(self):
        plan = parse_plan('```json\n[{"method": "GET", "path": "/y"}]\n```')
        self.assertEqual(plan[0]["path"], "/y")

    def test_calls_wrapper(self):
        plan = parse_plan('{"calls": [{"method": "GET", "path": "/z"}]}')
        self.assertEqual(plan[0]["path"], "/z")

    def test_unparseable(self):
        self.assertIsNone(parse_plan("no plan"))
        self.assertIsNone(parse_plan('{"a": 1}'))


class TestCheckApi(unittest.TestCase):
    def test_correct_plan_passes(self):
        report = check_api(_metadata(), json.dumps(CALLS))
        self.assertTrue(report["parsed"])
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(report["calls_made"], 4)

    def test_inline_query_params_match(self):
        calls = [dict(c) for c in CALLS]
        calls[3] = {"method": "GET", "path": "/orders?user_id=42"}
        report = check_api(_metadata(), json.dumps(calls))
        self.assertTrue(report["passes"])

    def test_missing_call_partial_credit(self):
        report = check_api(_metadata(), json.dumps(CALLS[:3]))
        self.assertFalse(report["passes"])
        self.assertEqual(report["score"], 0.75)
        self.assertIn("GET /orders", report["missing"])

    def test_wrong_body_misses(self):
        calls = [dict(c) for c in CALLS]
        calls[2] = {"method": "POST", "path": "/orders", "json": {"user_id": 42, "sku": "X"}}
        report = check_api(_metadata(), json.dumps(calls))
        self.assertFalse(report["passes"])
        self.assertIn("POST /orders", report["missing"])

    def test_wrong_params_misses(self):
        calls = [dict(c) for c in CALLS]
        calls[3] = {"method": "GET", "path": "/orders", "params": {"user_id": 99}}
        report = check_api(_metadata(), json.dumps(calls))
        self.assertFalse(report["passes"])

    def test_unexpected_call_fails_strict(self):
        extra = [*CALLS, {"method": "DELETE", "path": "/orders"}]
        report = check_api(_metadata(), json.dumps(extra))
        self.assertEqual(report["matched"], 4)
        self.assertFalse(report["passes"])
        self.assertIn("DELETE /orders", report["unexpected"])

    def test_unexpected_call_ok_when_not_strict(self):
        extra = [*CALLS, {"method": "GET", "path": "/health"}]
        report = check_api(_metadata(strict=False), json.dumps(extra))
        self.assertTrue(report["passes"])

    def test_unparseable_plan(self):
        report = check_api(_metadata(), "I can't do that")
        self.assertFalse(report["parsed"])
        self.assertIsNone(report["score"])

    def test_missing_metadata_calls(self):
        report = check_api({"stub": STUB}, "[]")
        self.assertFalse(report["passes"])
        self.assertTrue(any("metadata.calls" in e for e in report["errors"]))


class _FakeClient:
    """Chat stand-in: plan → two subtasks, workers → canned plan, pick → 0."""

    def __init__(self, plans: list[str], pick: int = 0):
        self.plans = plans
        self.pick = pick
        self.worker_calls = 0

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = self.plans[min(self.worker_calls, len(self.plans) - 1)]
            self.worker_calls += 1
        elif "candidates" in data:
            body = json.dumps({"subtask_id": self.pick})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [
                {"id": 0, "description": "plan one"},
                {"id": 1, "description": "plan two"},
            ]})
        else:
            body = json.dumps({"score": 0.5, "passed": True, "reasoning": "ok"})
        return {
            "content": body,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "latency_ms": 1,
            "id": "fake",
        }

    def close(self):
        pass


class TestApiRunner(unittest.TestCase):
    def test_dry_run_replays_expected_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/api", "worker"),
            )
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)
            self.assertTrue((Path(meta.run_dir) / "artifact.json").exists())

    def test_live_correct_plan_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(plans=[json.dumps(CALLS)])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/api", "worker"))
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)

    def test_live_incomplete_plan_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(plans=[json.dumps(CALLS[:2])])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/api", "worker"))
            self.assertFalse(meta.passes)
            self.assertEqual(meta.score, 0.5)


if __name__ == "__main__":
    unittest.main()
