"""End-to-end Runner test with mocked provider clients — no network, no keys."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
    )


def _chat_client(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "latency_ms": 1,
        "id": "mock",
    }
    return client


class TestEndToEndMockedProviders(unittest.TestCase):
    def test_full_run_with_judge(self):
        plan = {"subtasks": [{"id": 0, "description": "hero section"}]}
        orch = _chat_client("")  # return value assigned per call below
        orch.chat.side_effect = [
            {"content": json.dumps(plan), "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "latency_ms": 1, "id": "p"},
            {"content": "<html><head><title>T</title></head><body>ok</body></html>",
             "usage": {"prompt_tokens": 200, "completion_tokens": 100}, "latency_ms": 1, "id": "a"},
        ]
        worker = _chat_client("<section>hero</section>")
        judge = _chat_client(json.dumps({"score": 9, "passed": True, "reasoning": "solid"}))

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store,
                clients={"orchestrator": orch, "worker": worker, "judge": judge},
            )
            task = TaskSpec(id="t1", type="html", prompt="build a page")
            meta = runner.run(
                task,
                _model("o/m", "orchestrator"),
                _model("w/m", "worker"),
                _model("j/m", "judge"),
            )

            self.assertEqual(meta.status, "finished")
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 9)
            self.assertGreater(meta.total_input_tokens + meta.total_output_tokens, 0)
            self.assertGreater(meta.total_cost_usd, 0)

            run_dir = Path(meta.run_dir)
            self.assertTrue((run_dir / "artifact.html").exists())
            self.assertTrue((run_dir / "run.json").exists())
            report = json.loads((run_dir / "report.json").read_text())
            self.assertEqual(report["judge"]["score"], 9)

            # per-role routing: each mock got calls; judge saw the artifact
            self.assertEqual(orch.chat.call_count, 2)   # plan + assemble
            self.assertEqual(worker.chat.call_count, 1)  # delegation
            self.assertEqual(judge.chat.call_count, 1)
            judge_msgs = judge.chat.call_args.kwargs["messages"]
            self.assertIn("expert judge", judge_msgs[0]["content"])

    def test_injected_clients_not_closed_by_runner(self):
        """Caller-owned injected clients outlive the run (a grid reuses them)."""
        client = _chat_client("<html><title>t</title><body>x</body></html>")
        # one client serves all orchestrator calls: plan then assemble
        orch = MagicMock()
        orch.chat.side_effect = [
            {"content": json.dumps({"subtasks": [{"id": 0, "description": "s"}]}),
             "usage": {}, "latency_ms": 1, "id": "p"},
            {"content": "<html><title>t</title><body>x</body></html>",
             "usage": {}, "latency_ms": 1, "id": "a"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(
                runs_dir=tmp, store=RunStore(tmp),
                clients={"orchestrator": orch, "worker": client},
            )
            runner.run(TaskSpec(id="t2", type="html", prompt="p"),
                       _model("o/m", "orchestrator"), _model("w/m", "worker"))
            orch.close.assert_not_called()
            client.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
