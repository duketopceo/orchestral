"""Tests for the needle task type — long-context needle-in-haystack."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner

METADATA = {
    "document": "line 1\n" * 100 + "release_token=FALCON-4417\n" + "line 2\n" * 100,
    "required": ["FALCON-4417"],
    "forbidden": ["ALPHA-7734", "DELTA-2210"],
    "expected_answer": "FALCON-4417",
}
CHECKS = ["non_empty", "has_required", "no_forbidden"]


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "needle-test",
        "type": "needle",
        "prompt": "Find the release_token.",
        "validation": list(CHECKS),
        "metadata": dict(METADATA),
    }
    base.update(kwargs)
    return TaskSpec(**base)


class _FakeClient:
    """Chat stand-in: plan → one subtask, workers → canned answer, pick → 0."""

    def __init__(self, answer: str):
        self.answer = answer
        self.worker_messages: list[str] = []

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            self.worker_messages.append(messages[-1]["content"])
            body = self.answer
        elif "candidates" in data:
            body = json.dumps({"subtask_id": 0})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [{"id": 0, "description": "find it"}]})
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


class TestNeedleRunner(unittest.TestCase):
    def test_dry_run_passes_via_expected_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/nd", "worker"),
            )
            self.assertTrue(meta.passes)
            self.assertTrue((Path(meta.run_dir) / "artifact.txt").exists())

    def test_document_reaches_the_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient("FALCON-4417")
            Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/nd", "worker"))
            self.assertTrue(client.worker_messages)
            payload = json.loads(client.worker_messages[0])
            self.assertEqual(payload["subtask"]["document"], METADATA["document"])

    def test_correct_token_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient("FALCON-4417")
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/nd", "worker"))
            self.assertTrue(meta.passes)

    def test_decoy_token_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient("ALPHA-7734")
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/nd", "worker"))
            self.assertFalse(meta.passes)
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertFalse(report["checks"]["has_required"])
            self.assertFalse(report["checks"]["no_forbidden"])

    def test_verbose_answer_with_decoy_fails(self):
        """A right answer padded with a decoy mention still fails."""
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient("The token is FALCON-4417, not DELTA-2210")
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/nd", "worker"))
            self.assertFalse(meta.passes)


if __name__ == "__main__":
    unittest.main()
