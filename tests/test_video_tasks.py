"""Tests for video task config, cost accounting, worker eligibility, and run paths."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import _eligible_workers
from orchestral.config import ModelConfig, TaskSpec, load_models
from orchestral.costs import compute_video_cost
from orchestral.openrouter import OpenRouterVideoJobError, OpenRouterVideoSubmittedError
from orchestral.planners import TINY_MP4
from orchestral.runner import Runner


def _cfg(**kwargs) -> ModelConfig:
    base = {
        "slug": "v/model",
        "name": "V",
        "role": "worker",
        "input_price_per_mtok": 0.0,
        "output_price_per_mtok": 0.0,
    }
    base.update(kwargs)
    return ModelConfig(**base)


class TestVideoCost(unittest.TestCase):
    def test_api_cost_wins(self):
        cfg = _cfg(price_per_video_second=0.5)
        self.assertEqual(compute_video_cost(cfg, api_cost=0.42, duration_s=4), 0.42)

    def test_per_second_fallback(self):
        cfg = _cfg(price_per_video_second=0.5)
        self.assertEqual(compute_video_cost(cfg, duration_s=4), 2.0)

    def test_no_price_no_cost(self):
        cfg = _cfg()
        self.assertEqual(compute_video_cost(cfg, duration_s=4), 0.0)
        self.assertEqual(compute_video_cost(cfg), 0.0)

    def test_option_aware_rate(self):
        cfg = _cfg(
            price_per_video_second=0.20,
            metadata={"video_pricing": {"*:silent": 0.20, "*:audio": 0.40, "4K:silent": 0.40}},
        )
        # exact match beats wildcard; 4s duration
        self.assertEqual(
            compute_video_cost(cfg, duration_s=4, resolution="4K", generate_audio=False), 1.60
        )
        self.assertEqual(
            compute_video_cost(cfg, duration_s=4, resolution="720p", generate_audio=True), 1.60
        )
        self.assertEqual(
            compute_video_cost(cfg, duration_s=4, resolution="720p", generate_audio=False), 0.80
        )

    def test_unmatched_option_falls_back_to_base_rate(self):
        cfg = _cfg(price_per_video_second=0.10, metadata={"video_pricing": {"4K:silent": 0.40}})
        self.assertEqual(
            compute_video_cost(cfg, duration_s=4, resolution="720p", generate_audio=False), 0.40
        )

    def test_api_cost_beats_option_aware_rate(self):
        cfg = _cfg(price_per_video_second=0.10, metadata={"video_pricing": {"*:audio": 0.40}})
        self.assertEqual(
            compute_video_cost(cfg, api_cost=0.07, duration_s=4, resolution="720p", generate_audio=True),
            0.07,
        )

    def test_verified_model_pricing_is_option_aware(self):
        models = {m.slug: m for m in load_models("models")}
        veo = models["google/veo-3.1"]
        self.assertEqual(
            compute_video_cost(veo, duration_s=4, resolution="4K", generate_audio=True), 2.40
        )
        self.assertEqual(
            compute_video_cost(veo, duration_s=4, resolution="720p", generate_audio=False), 0.80
        )


class TestVideoModelConfig(unittest.TestCase):
    def test_price_per_video_second_loads_and_serializes(self):
        cfg = _cfg(price_per_video_second=0.25, metadata={"modalities": ["video"]})
        self.assertEqual(cfg.price_per_video_second, 0.25)
        self.assertEqual(cfg.to_dict()["price_per_video_second"], 0.25)
        self.assertTrue(cfg.supports("video"))
        self.assertFalse(cfg.supports("text"))

    def test_default_yaml_video_entries(self):
        models = load_models("models")
        video = {m.slug: m for m in models if m.supports("video")}
        # slugs verified against GET /api/v1/videos/models
        self.assertEqual(
            set(video),
            {"google/veo-3.1", "google/veo-3.1-lite", "minimax/hailuo-3", "alibaba/wan-2.7"},
        )
        for m in video.values():
            self.assertGreater(m.price_per_video_second, 0)


class TestVideoWorkerEligibility(unittest.TestCase):
    def test_video_task_selects_video_workers_only(self):
        pool = [
            _cfg(slug="v/vid", metadata={"modalities": ["video"]}),
            _cfg(slug="v/text"),
            _cfg(slug="v/img", metadata={"modalities": ["image"]}),
        ]
        eligible = _eligible_workers(pool, "video")
        self.assertEqual([m.slug for m in eligible], ["v/vid"])

    def test_text_task_excludes_video_only_workers(self):
        pool = [
            _cfg(slug="v/vid", metadata={"modalities": ["video"]}),
            _cfg(slug="v/text"),
            _cfg(slug="v/multi", metadata={"modalities": ["text", "video"]}),
        ]
        eligible = _eligible_workers(pool, "html")
        self.assertEqual([m.slug for m in eligible], ["v/text", "v/multi"])


class _FakeClient:
    """Minimal chat+videos stand-in for live-path runner tests."""

    def __init__(self, video_error: Exception | None = None):
        self.video_calls = 0
        self._video_error = video_error

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        data = json.loads(messages[-1]["content"])
        out = {"subtask_id": 0} if "candidates" in data else {
            "subtasks": [{"id": 0, "description": "one clip"}],
        }
        return {
            "content": json.dumps(out),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1,
            "id": "fake",
        }

    def videos(self, **kwargs):
        self.video_calls += 1
        if self._video_error is not None:
            raise self._video_error
        return {
            "id": "job-x",
            "model": kwargs["model"],
            "video_bytes": TINY_MP4,
            "usage": {"cost": 0.12},
            "latency_ms": 1,
            "raw_response": {},
        }

    def close(self):
        pass


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "vid-test",
        "type": "video",
        "prompt": "A short clip.",
        "validation": ["non_empty", "mp4_signature"],
        "metadata": {"duration": 4, "resolution": "720p", "aspect_ratio": "16:9"},
    }
    base.update(kwargs)
    return TaskSpec(**base)


class TestVideoTaskRun(unittest.TestCase):
    def test_dry_run_writes_mp4_artifacts_and_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _cfg(slug="v/vid", price_per_video_second=0.5)
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _cfg(slug="org/x", role="orchestrator"), worker,
            )
            run_dir = Path(meta.run_dir)

            artifact = run_dir / "artifact.mp4"
            self.assertTrue(artifact.exists())
            self.assertEqual(artifact.read_bytes(), TINY_MP4)
            self.assertTrue((run_dir / "worker-0.mp4").exists())
            self.assertTrue(meta.passes)

            cost = json.loads((run_dir / "cost.json").read_text())
            delegate = next(c for c in cost if c["phase"] == "delegate")
            self.assertEqual(delegate["cost_usd"], 2.0)  # 4s * $0.5/s fallback

            report = json.loads((run_dir / "report.json").read_text())
            self.assertTrue(report["checks"]["mp4_signature"])

    def test_live_run_uses_api_cost_and_writes_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient()
            worker = _cfg(slug="v/vid", price_per_video_second=0.5)
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _cfg(slug="org/x", role="orchestrator"), worker)

            run_dir = Path(meta.run_dir)
            self.assertEqual(client.video_calls, 1)
            self.assertTrue((run_dir / "artifact.mp4").exists())
            self.assertTrue(meta.passes)
            cost = json.loads((run_dir / "cost.json").read_text())
            delegate = next(c for c in cost if c["phase"] == "delegate")
            self.assertEqual(delegate["cost_usd"], 0.12)  # usage.cost wins

    def test_terminal_video_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = OpenRouterVideoJobError("content policy", status="failed")
            client = _FakeClient(video_error=err)
            worker = _cfg(slug="v/vid", retry_limit=3)
            with self.assertRaises(OpenRouterVideoJobError):
                Runner(
                    runs_dir=tmp, planner="raw",
                    clients={"orchestrator": client, "worker": client},
                ).run(_task(), _cfg(slug="org/x", role="orchestrator"), worker)
            self.assertEqual(client.video_calls, 1)

    def test_post_submission_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = OpenRouterVideoSubmittedError("polling failed after 4 errors")
            client = _FakeClient(video_error=err)
            worker = _cfg(slug="v/vid", retry_limit=3)
            with self.assertRaises(OpenRouterVideoSubmittedError):
                Runner(
                    runs_dir=tmp, planner="raw",
                    clients={"orchestrator": client, "worker": client},
                ).run(_task(), _cfg(slug="org/x", role="orchestrator"), worker)
            # one paid submission, never a resubmit
            self.assertEqual(client.video_calls, 1)

    def test_judge_skipped_for_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = _cfg(slug="j/judge", role="judge")
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _cfg(slug="org/x", role="orchestrator"),
                _cfg(slug="v/vid"), judge=judge,
            )
            run_dir = Path(meta.run_dir)
            events = (run_dir / "events.jsonl").read_text()
            self.assertIn("judge_skipped", events)
            report = json.loads((run_dir / "report.json").read_text())
            self.assertNotIn("judge", report)
            self.assertIsNone(report["score"])


class TestVideoValidation(unittest.TestCase):
    def _check(self, validation, artifact):
        runner = Runner(runs_dir=tempfile.mkdtemp(), dry_run=True)
        return runner._validate_video(_task(validation=validation), artifact)

    def test_mp4_signature_pass(self):
        passes, report = self._check(["non_empty", "mp4_signature"], TINY_MP4)
        self.assertTrue(passes)
        self.assertTrue(report["checks"]["mp4_signature"])

    def test_mp4_signature_fail(self):
        passes, report = self._check(["mp4_signature"], b"not-a-video")
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["mp4_signature"])

    def test_non_empty_fail(self):
        passes, _ = self._check(["non_empty"], b"")
        self.assertFalse(passes)

    def test_unknown_check_fails_closed(self):
        passes, report = self._check(["bogus_check"], TINY_MP4)
        self.assertFalse(passes)
        self.assertEqual(report["checks"], {})


if __name__ == "__main__":
    unittest.main()
