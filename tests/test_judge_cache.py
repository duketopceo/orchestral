"""Tests for the judge result cache and image judging path."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from orchestral.config import ModelConfig, TaskSpec
from orchestral.judge import judge_artifact
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str = "worker") -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role, input_price_per_mtok=0.1, output_price_per_mtok=0.4)


class TestJudgeCacheTable(unittest.TestCase):
    def test_put_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            result = {"score": 0.9, "passed": True, "reasoning": "ok"}
            store.put_judge_result("t1", "j/model", "abc123", result)
            self.assertEqual(store.get_judge_result("t1", "j/model", "abc123"), result)

    def test_miss_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            self.assertIsNone(store.get_judge_result("t1", "j/model", "nope"))


class TestJudgeCacheInRun(unittest.TestCase):
    def _runner(self, store: RunStore, use_cache: bool, reply: dict) -> Runner:
        runner = Runner(dry_run=False, runs_dir=store.root, use_judge_cache=use_cache, store=store)
        runner.client = MagicMock()
        runner.client.chat.return_value = {
            "content": json.dumps(reply),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1,
            "id": "x",
        }
        return runner

    def _judge(self, runner: Runner, task: TaskSpec, artifact_text: str, judge_slug="j/model"):
        return runner._judge_with_cache(
            logger=MagicMock(),
            step=1,
            task=task,
            artifact_bytes=None,
            artifact_text=artifact_text,
            judge=_model(judge_slug, "judge"),
        )

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"})
    def test_cache_hit_serves_same_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            task = TaskSpec(id="t", type="html", prompt="p")
            artifact = "<html>whatever</html>"
            sha = hashlib.sha256(artifact.encode()).hexdigest()
            seeded = {"score": 0.77, "passed": True, "reasoning": "cached"}
            store.put_judge_result(task.id, "j/model", sha, seeded)

            runner = self._runner(store, use_cache=True, reply={"score": 0.99, "passed": True, "reasoning": "api"})
            result, costs = self._judge(runner, task, artifact)
            self.assertEqual(result, seeded)
            self.assertEqual(costs, [])
            runner.client.chat.assert_not_called()

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"})
    def test_no_judge_cache_bypasses_read_but_still_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            task = TaskSpec(id="t", type="html", prompt="p")
            artifact = "<html>x</html>"
            sha = hashlib.sha256(artifact.encode()).hexdigest()
            store.put_judge_result(task.id, "j/model", sha, {"score": 0.1, "passed": False, "reasoning": "stale"})

            fresh = {"score": 0.9, "passed": True, "reasoning": "new"}
            runner = self._runner(store, use_cache=False, reply=fresh)
            result, _ = self._judge(runner, task, artifact)
            self.assertEqual(result["score"], 0.9)
            runner.client.chat.assert_called_once()
            self.assertEqual(store.get_judge_result(task.id, "j/model", sha)["reasoning"], "new")

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"})
    def test_cache_key_changes_with_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            task = TaskSpec(id="t", type="html", prompt="p")
            runner = self._runner(store, use_cache=True, reply={"score": 0.5, "passed": True, "reasoning": "r"})
            self._judge(runner, task, "<html>a</html>")
            self._judge(runner, task, "<html>b</html>")
            self.assertEqual(runner.client.chat.call_count, 2)

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"})
    def test_cache_key_changes_with_judge_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            task = TaskSpec(id="t", type="html", prompt="p")
            runner = self._runner(store, use_cache=True, reply={"score": 0.5, "passed": True, "reasoning": "r"})
            self._judge(runner, task, "<html>a</html>", judge_slug="j/one")
            self._judge(runner, task, "<html>a</html>", judge_slug="j/two")
            self.assertEqual(runner.client.chat.call_count, 2)

    def test_dry_run_does_not_populate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(id="t", type="html", prompt="p")
            judge = _model("j/model", "judge")
            Runner(dry_run=True, runs_dir=tmp).run(task, _model("o/m", "orchestrator"), _model("w/m"), judge)
            store = RunStore(tmp)
            with store._connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM judge_cache").fetchone()[0]
            self.assertEqual(count, 0)


class TestImageJudgeMessages(unittest.TestCase):
    def test_image_bytes_build_multimodal_message(self):
        judge = _model("j/vision", "judge")
        client = MagicMock()
        client.chat.return_value = {
            "content": '{"score": 0.8, "passed": true, "reasoning": "nice"}',
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 100,
            "id": "x",
        }
        logger = MagicMock()
        result, costs = judge_artifact(
            logger=logger,
            step=1,
            task=TaskSpec(id="img", type="image", prompt="a logo"),
            artifact="",
            judge=judge,
            client=client,
            dry_run=False,
            image_bytes=b"\x89PNG fake",
        )
        self.assertEqual(result["score"], 0.8)
        user_msg = client.chat.call_args.kwargs["messages"][1]["content"]
        self.assertIsInstance(user_msg, list)
        types = {p["type"] for p in user_msg}
        self.assertEqual(types, {"text", "image_url"})
        self.assertTrue(user_msg[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_oversized_image_skips_api_call(self):
        from orchestral.judge import MAX_JUDGE_IMAGE_BYTES

        client = MagicMock()
        result, costs = judge_artifact(
            logger=MagicMock(),
            step=1,
            task=TaskSpec(id="img", type="image", prompt="a logo"),
            artifact="",
            judge=_model("j/vision", "judge"),
            client=client,
            dry_run=False,
            image_bytes=b"x" * (MAX_JUDGE_IMAGE_BYTES + 1),
        )
        self.assertIsNone(result["score"])
        self.assertEqual(costs, [])
        client.chat.assert_not_called()


class TestGridJudgePlumbing(unittest.TestCase):
    def test_grid_records_judge_in_run_config(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        import harness

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                task="landing-page-coffee",
                orchestrators="deepseek/deepseek-v4-flash-0731",
                workers="z-ai/glm-5.3-flash",
                planner="raw",
                judge="anthropic/claude-haiku-4.5",
                no_judge_cache=False,
                retry_limit=None,
                prompt_variant=None,
                jobs=1,
                dry_run=True,
                json=False,
                runs_dir=tmp,
                tasks_dir="tasks",
                models_dir="models",
            )
            with redirect_stdout(io.StringIO()):
                harness.cmd_grid(args)
            runs = RunStore(tmp).list_runs(limit=None)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].config["judge"], "anthropic/claude-haiku-4.5")


if __name__ == "__main__":
    unittest.main()
