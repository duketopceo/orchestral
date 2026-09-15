"""Tests for constraint validation checks and the constraint task type."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner

COMPLIANT = (
    "The UpRight Basic is a sturdy standing desk priced at $249. It adjusts "
    "smoothly, holds a full laptop-and-monitor setup, and assembles in under "
    "an hour. For a home office on a budget, it covers the fundamentals well."
)

METADATA = {
    "min_words": 30,
    "max_words": 60,
    "required": ["UpRight Basic", "$249"],
    "forbidden": ["best", "amazing", "revolutionary"],
    "forbidden_pattern": "!+",
    "reference_text": COMPLIANT,
}
CHECKS = ["non_empty", "within_budget", "has_required", "no_forbidden", "no_pattern"]


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "constraint-test",
        "type": "constraint",
        "prompt": "Write a constrained blurb.",
        "validation": list(CHECKS),
        "metadata": dict(METADATA),
    }
    base.update(kwargs)
    return TaskSpec(**base)


def _validate(artifact: str, validation=None, metadata=None):
    runner = Runner(runs_dir=tempfile.mkdtemp(), dry_run=True)
    task = _task(
        validation=validation if validation is not None else CHECKS,
        metadata=metadata if metadata is not None else METADATA,
    )
    return runner._validate(task, artifact)


class TestWithinBudget(unittest.TestCase):
    def test_passes_inside_bounds(self):
        passes, _ = _validate(COMPLIANT)
        self.assertTrue(passes)

    def test_fails_over_max_words(self):
        passes, report = _validate(COMPLIANT + " " + "more " * 40)
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["within_budget"])
        self.assertTrue(any("max_words" in e for e in report["errors"]))

    def test_fails_under_min_words(self):
        passes, report = _validate("UpRight Basic $249 desk.")
        self.assertFalse(passes)
        self.assertTrue(any("min_words" in e for e in report["errors"]))

    def test_char_bounds(self):
        md = {"min_chars": 10, "max_chars": 20}
        passes, _ = _validate("a" * 25, validation=["within_budget"], metadata=md)
        self.assertFalse(passes)
        passes, _ = _validate("a" * 15, validation=["within_budget"], metadata=md)
        self.assertTrue(passes)

    def test_fails_closed_with_no_bounds(self):
        passes, report = _validate("text", validation=["within_budget"], metadata={})
        self.assertFalse(passes)
        self.assertTrue(any("no min/max" in e for e in report["errors"]))


class TestTokenConstraints(unittest.TestCase):
    def test_has_required_passes(self):
        passes, report = _validate(COMPLIANT, validation=["has_required"])
        self.assertTrue(passes)
        self.assertTrue(report["checks"]["has_required"])

    def test_has_required_case_insensitive(self):
        passes, _ = _validate(
            COMPLIANT.lower() + " filler " * 30,
            validation=["has_required"],
        )
        self.assertTrue(passes)

    def test_has_required_missing(self):
        passes, report = _validate("a desk " * 10, validation=["has_required"])
        self.assertFalse(passes)
        self.assertTrue(any("$249" in e for e in report["errors"]))

    def test_no_forbidden_hits(self):
        passes, report = _validate(COMPLIANT + " it is the best " + "word " * 25)
        self.assertFalse(passes)
        self.assertTrue(any("best" in e for e in report["errors"]))

    def test_no_forbidden_fails_closed_when_empty(self):
        passes, _ = _validate("text", validation=["no_forbidden"], metadata={})
        self.assertFalse(passes)


class TestPatternConstraints(unittest.TestCase):
    def test_no_pattern_blocks_exclamation(self):
        passes, report = _validate(COMPLIANT + " Great desk!")
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["no_pattern"])

    def test_matches_pattern(self):
        passes, _ = _validate(
            COMPLIANT,
            validation=["matches_pattern"],
            metadata={"pattern": r"\$\d+"},
        )
        self.assertTrue(passes)
        passes, _ = _validate(
            "no price here",
            validation=["matches_pattern"],
            metadata={"pattern": r"\$\d+"},
        )
        self.assertFalse(passes)

    def test_invalid_regex_fails_closed(self):
        passes, report = _validate(
            "text",
            validation=["matches_pattern"],
            metadata={"pattern": "[unclosed"},
        )
        self.assertFalse(passes)
        self.assertTrue(any("valid regex" in e for e in report["errors"]))


class _FakeClient:
    """Chat stand-in returning canned text; plan → one subtask, pick → 0."""

    def __init__(self, body: str):
        self.body = body

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = self.body
        elif "candidates" in data:
            body = json.dumps({"subtask_id": 0})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [{"id": 0, "description": "blurb"}]})
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


class TestConstraintRunner(unittest.TestCase):
    def test_dry_run_passes_via_reference_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/txt", "worker"),
            )
            self.assertTrue(meta.passes)

    def test_live_compliant_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(COMPLIANT)
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/txt", "worker"))
            self.assertTrue(meta.passes)

    def test_live_violation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = "The UpRight Basic at $249 is the best amazing revolutionary desk!!! " + "wow " * 60
            client = _FakeClient(bad)
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/txt", "worker"))
            self.assertFalse(meta.passes)
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertFalse(report["checks"]["no_forbidden"])
            self.assertFalse(report["checks"]["no_pattern"])
            self.assertFalse(report["checks"]["within_budget"])


if __name__ == "__main__":
    unittest.main()
