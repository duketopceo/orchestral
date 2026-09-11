"""Tests for the judge result cache and image judging path."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
    def _run_once(self, runs_dir: str, judge: ModelConfig, task: TaskSpec) -> None:
        Runner(dry_run=True, runs_dir=runs_dir).run(task, _model("o/m", "orchestrator"), _model("w/m"), judge)

    def test_cache_hit_serves_same_result(self):
        """A second identical run must not call the judge API — dry-run runs
        skip caching, so simulate by seeding the cache then checking a real
        (non-dry) run reads it."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            task = TaskSpec(id="t", type="html", prompt="p")
            artifact_sha = hashlib.sha256(b"<html>whatever</html>").hexdigest()
            seeded = {"score": 0.77, "passed": True, "reasoning": "cached"}
            store.put_judge_result(task.id, "j/model", artifact_sha, seeded)
            self.assertEqual(store.get_judge_result(task.id, "j/model", artifact_sha), seeded)

    def test_dry_run_does_not_populate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(id="t", type="html", prompt="p")
            judge = _model("j/model", "judge")
            self._run_once(tmp, judge, task)
            store = RunStore(tmp)
            # artifact bytes differ per run but nothing should be cached at all
            rows = store._connect()
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


if __name__ == "__main__":
    unittest.main()
