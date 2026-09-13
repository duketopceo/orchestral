"""Tests for video task config, cost accounting, and worker eligibility."""

from __future__ import annotations

import unittest

from harness import _eligible_workers
from orchestral.config import ModelConfig, load_models
from orchestral.costs import compute_video_cost


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


class TestVideoModelConfig(unittest.TestCase):
    def test_price_per_video_second_loads_and_serializes(self):
        cfg = _cfg(price_per_video_second=0.25, metadata={"modalities": ["video"]})
        self.assertEqual(cfg.price_per_video_second, 0.25)
        self.assertEqual(cfg.to_dict()["price_per_video_second"], 0.25)
        self.assertTrue(cfg.supports("video"))
        self.assertFalse(cfg.supports("text"))

    def test_default_yaml_video_entries_are_disabled(self):
        models = load_models("models")
        self.assertFalse(any(m.supports("video") for m in models))


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


if __name__ == "__main__":
    unittest.main()
