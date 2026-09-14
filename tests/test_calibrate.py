"""Tests for judge calibration — agreement metrics and label joining."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.calibrate import (
    agreement_metrics,
    cohens_kappa,
    collect_pairs,
    load_labels,
    pearson,
    spearman,
)
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task() -> TaskSpec:
    return TaskSpec(
        id="cal-test", type="html", prompt="Make a page.",
        validation=["non_empty"],
    )


class _JudgeClient:
    """Chat stand-in: plan → one subtask, worker → html, judge → 0.8/pass."""

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = "<html><body>page</body></html>"
        elif "candidates" in data or "prompt" in data:
            body = json.dumps({"subtasks": [{"id": 0, "description": "page"}]})
        else:
            body = json.dumps({"score": 0.8, "passed": True, "reasoning": "decent"})
        return {
            "content": body,
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            "latency_ms": 1, "id": "fake",
        }

    def close(self):
        pass


class TestMetrics(unittest.TestCase):
    def test_pearson_perfect(self):
        self.assertAlmostEqual(pearson([1, 2, 3], [2, 4, 6]), 1.0)

    def test_pearson_constant_input_none(self):
        self.assertIsNone(pearson([1, 1, 1], [1, 2, 3]))

    def test_spearman_handles_ties(self):
        rho = spearman([1, 1, 2, 3], [10, 10, 20, 30])
        self.assertAlmostEqual(rho, 1.0)

    def test_kappa_perfect_and_chance(self):
        self.assertAlmostEqual(cohens_kappa([True, False], [True, False]), 1.0)
        # judge always predicts True against a 50/50 truth → agreement at chance
        self.assertAlmostEqual(
            cohens_kappa([True, False, True, False], [True] * 4), 0.0
        )

    def test_score_metrics(self):
        pairs = [
            {"human_score": 1.0, "judge_score": 0.9, "human_passed": True, "judge_passed": True},
            {"human_score": 0.0, "judge_score": 0.2, "human_passed": False, "judge_passed": True},
        ]
        m = agreement_metrics(pairs)
        self.assertAlmostEqual(m["score"]["mae"], 0.15)
        self.assertEqual(m["verdict"]["n"], 2)
        self.assertEqual(m["verdict"]["fp"], 1)
        self.assertAlmostEqual(m["verdict"]["accuracy"], 0.5)

    def test_partial_coverage_only_counts_both_sides(self):
        pairs = [
            {"human_score": 1.0, "judge_score": None, "human_passed": True, "judge_passed": None},
        ]
        m = agreement_metrics(pairs)
        self.assertIsNone(m["score"])
        self.assertIsNone(m["verdict"])


class TestCollectPairs(unittest.TestCase):
    def _judged_run(self, tmp: str):
        client = _JudgeClient()
        meta = Runner(
            runs_dir=tmp, planner="raw",
            clients={"orchestrator": client, "worker": client, "judge": client},
        ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/y", "worker"),
              judge=_model("j/judge", "judge"))
        return meta

    def test_joins_labels_to_judge_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._judged_run(tmp)
            labels = [{"run_id": meta.run_id, "score": 0.9, "passed": True}]
            result = collect_pairs(RunStore(tmp), labels)
            self.assertEqual(result["coverage"]["matched"], 1)
            pair = result["pairs"][0]
            self.assertEqual(pair["judge_score"], 0.8)
            self.assertTrue(pair["judge_passed"])
            m = agreement_metrics(result["pairs"])
            self.assertAlmostEqual(m["score"]["mae"], 0.1)
            self.assertEqual(m["verdict"]["accuracy"], 1.0)

    def test_unmatched_labels_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._judged_run(tmp)
            result = collect_pairs(RunStore(tmp), [{"run_id": "doesnotexist"}])
            self.assertEqual(result["coverage"]["matched"], 0)
            self.assertEqual(result["coverage"]["unmatched"], ["doesnotexist"])

    def test_load_labels_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.yaml"
            path.write_text("labels:\n  - run_id: abc\n    score: 0.5\n    passed: true\n")
            labels = load_labels(path)
            self.assertEqual(labels[0]["run_id"], "abc")
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("labels:\n  - score: 0.5\n")
            with self.assertRaises(ValueError):
                load_labels(bad)


if __name__ == "__main__":
    unittest.main()
