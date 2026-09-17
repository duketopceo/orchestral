"""Pipeline tasks — subtasks run in order, each sees prior outputs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "pipeline-test",
        "type": "pipeline",
        "prompt": "Chain the subtasks.",
        "validation": ["has_required"],
        "metadata": {
            "reference_text": "final artifact",
            "required": ["final"],
        },
    }
    base.update(kwargs)
    return TaskSpec(**base)


class _FakeClient:
    """Captures worker subtask payloads; returns canned content."""

    def __init__(self):
        self.worker_subtasks: list[dict] = []

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            self.worker_subtasks.append(data["subtask"])
            body = json.dumps({"content": f"output-{data['subtask'].get('id')}"})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [
                {"id": 0, "description": "step one"},
                {"id": 1, "description": "step two"},
            ]})
        else:
            body = json.dumps({"score": 0.5, "passed": True, "reasoning": "ok"})
        return {"content": body, "usage": {"prompt_tokens": 20, "completion_tokens": 10}, "latency_ms": 1, "id": "fake"}

    def close(self):
        pass


class TestPipeline(unittest.TestCase):
    def test_prior_outputs_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient()
            meta = Runner(
                runs_dir=Path(tmp), planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertEqual(len(client.worker_subtasks), 2)
            self.assertNotIn("prior_outputs", client.worker_subtasks[0])
            self.assertEqual(
                client.worker_subtasks[1]["prior_outputs"],
                [{"subtask_id": 0, "content": "output-0"}],
            )
            artifact = Path(meta.run_dir) / "artifact.txt"
            self.assertEqual(artifact.read_text(), "output-1")
            self.assertFalse(meta.passes)  # "final" not in "output-1"

    def test_last_subtask_satisfying_validation_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient()
            # last worker returns content containing the required token
            orig_chat = client.chat

            def chat(model, messages, max_tokens=4096, temperature=0.4):
                data = json.loads(messages[-1]["content"])
                if "subtask" in data and data["subtask"].get("id") == 1:
                    client.worker_subtasks.append(data["subtask"])
                    return {"content": json.dumps({"content": "the final answer"}),
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                            "latency_ms": 1, "id": "fake"}
                return orig_chat(model, messages, max_tokens, temperature)

            client.chat = chat
            meta = Runner(
                runs_dir=Path(tmp), planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertTrue(meta.passes)

    def test_dry_run_uses_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=Path(tmp), planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"),
            )
            self.assertTrue(meta.passes)
            artifact = Path(meta.run_dir) / "artifact.txt"
            self.assertIn("final artifact", artifact.read_text())


if __name__ == "__main__":
    unittest.main()
