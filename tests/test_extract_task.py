"""Tests for the extract task type — deterministic JSON field grading."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.extract import check_extraction, extract_json
from orchestral.runner import Runner

METADATA = {
    "fields": {
        "name": {"type": "str", "required": True},
        "age": {"type": "int", "required": True},
        "vip": {"type": "bool"},
        "tier": {"type": "str", "enum": ["gold", "silver", "bronze"]},
    },
    "expected": {"name": "Ada", "age": 36, "vip": True, "tier": "gold"},
}


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "extract-test",
        "type": "extract",
        "prompt": "Extract name/age/vip/tier from the record.",
        "metadata": dict(METADATA),
    }
    base.update(kwargs)
    return TaskSpec(**base)


class _FakeClient:
    """Chat stand-in: plan → two subtasks, workers → canned JSON, pick → 0."""

    def __init__(self, payloads: list[str], pick: int = 0):
        self.payloads = payloads
        self.pick = pick
        self.worker_calls = 0

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = self.payloads[min(self.worker_calls, len(self.payloads) - 1)]
            self.worker_calls += 1
        elif "candidates" in data:
            body = json.dumps({"subtask_id": self.pick})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [
                {"id": 0, "description": "first extraction"},
                {"id": 1, "description": "second extraction"},
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


class TestExtractJson(unittest.TestCase):
    def test_raw_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('sure:\n```json\n{"a": 2}\n```'), {"a": 2})

    def test_embedded_in_prose(self):
        self.assertEqual(extract_json('The answer is {"a": 3} — done.'), {"a": 3})

    def test_array(self):
        self.assertEqual(extract_json("[1, 2]"), [1, 2])

    def test_unparseable(self):
        self.assertIsNone(extract_json("no json here"))


class TestCheckExtraction(unittest.TestCase):
    def test_perfect_scores_one(self):
        report = check_extraction(METADATA, json.dumps(METADATA["expected"]))
        self.assertTrue(report["parsed"])
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 1.0)

    def test_partial_credit(self):
        obj = dict(METADATA["expected"], tier="silver")
        report = check_extraction(METADATA, json.dumps(obj))
        self.assertAlmostEqual(report["score"], 0.75)
        self.assertFalse(report["field_results"]["tier"])
        self.assertFalse(report["passes"])  # default threshold is 1.0

    def test_pass_threshold_allows_partial(self):
        md = dict(METADATA, pass_threshold=0.5)
        obj = dict(METADATA["expected"], tier="silver", vip=False)
        report = check_extraction(md, json.dumps(obj))
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 0.5)

    def test_missing_required_fails_despite_threshold(self):
        md = dict(METADATA, pass_threshold=0.0)
        report = check_extraction(md, json.dumps({"name": "Ada"}))
        self.assertFalse(report["checks"]["required_present"])
        self.assertIn("age", report["missing_required"])
        self.assertFalse(report["passes"])

    def test_bool_is_not_int(self):
        obj = dict(METADATA["expected"], age=True)
        report = check_extraction(METADATA, json.dumps(obj))
        self.assertFalse(report["checks"]["types_ok"])
        self.assertFalse(report["passes"])

    def test_enum_violation_fails(self):
        obj = dict(METADATA["expected"], tier="platinum")
        report = check_extraction(METADATA, json.dumps(obj))
        self.assertFalse(report["checks"]["types_ok"])

    def test_non_object_json_scores_zero(self):
        report = check_extraction(METADATA, "[1, 2, 3]")
        self.assertTrue(report["parsed"])
        self.assertEqual(report["score"], 0.0)
        self.assertFalse(report["passes"])

    def test_unparseable_scores_none(self):
        report = check_extraction(METADATA, "I cannot help with that.")
        self.assertFalse(report["parsed"])
        self.assertIsNone(report["score"])
        self.assertFalse(report["passes"])

    def test_no_expected_means_schema_only(self):
        md = {"fields": {"name": {"type": "str", "required": True}}}
        self.assertEqual(check_extraction(md, '{"name": "x"}')["score"], 1.0)
        self.assertEqual(check_extraction(md, '{"name": 1}')["score"], 0.0)

    def test_list_fields_compare_elementwise(self):
        # The list branch of _strict_eq pairs elements with zip(); a length
        # mismatch must score 0 rather than pair the shorter prefix and pass.
        md = {"expected": {"tags": ["a", "b"]}}
        self.assertEqual(check_extraction(md, json.dumps({"tags": ["a", "b"]}))["score"], 1.0)
        self.assertEqual(check_extraction(md, json.dumps({"tags": ["a"]}))["score"], 0.0)
        self.assertEqual(check_extraction(md, json.dumps({"tags": ["a", "c"]}))["score"], 0.0)

    def test_bool_nested_in_a_list_is_not_an_int(self):
        # [True] == [1] in Python, so the elementwise walk must re-check the
        # bool/int distinction instead of deferring to list __eq__.
        md = {"expected": {"counts": [1, 2]}}
        self.assertEqual(check_extraction(md, json.dumps({"counts": [1, 2]}))["score"], 1.0)
        report = check_extraction(md, json.dumps({"counts": [True, 2]}))
        self.assertEqual(report["score"], 0.0)
        self.assertFalse(report["field_results"]["counts"])


class TestExtractRunner(unittest.TestCase):
    def test_dry_run_passes_and_writes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"),
            )
            run_dir = Path(meta.run_dir)
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)
            artifact = run_dir / "artifact.json"
            self.assertTrue(artifact.exists())
            self.assertEqual(json.loads(artifact.read_text())["name"], "Ada")

    def test_live_correct_extraction_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = json.dumps(METADATA["expected"])
            client = _FakeClient(payloads=[good])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)

    def test_live_partial_extraction_fails_with_partial_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            partial = json.dumps(dict(METADATA["expected"], tier="silver"))
            client = _FakeClient(payloads=[partial])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertFalse(meta.passes)
            self.assertAlmostEqual(meta.score, 0.75)

    def test_orchestrator_pick_decides_score(self):
        """Orchestrator picks candidate 1 — its worse extraction scores."""
        with tempfile.TemporaryDirectory() as tmp:
            good = json.dumps(METADATA["expected"])
            bad = json.dumps({"name": "Ada", "age": 36, "vip": True, "tier": "bronze"})
            client = _FakeClient(payloads=[good, bad], pick=1)
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertFalse(meta.passes)
            self.assertAlmostEqual(meta.score, 0.75)
            self.assertEqual(
                json.loads((Path(meta.run_dir) / "artifact.json").read_text())["tier"], "bronze"
            )

    def test_unparseable_candidate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(payloads=["sorry, I can't extract that"])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertFalse(meta.passes)
            self.assertIsNone(meta.score)


if __name__ == "__main__":
    unittest.main()
