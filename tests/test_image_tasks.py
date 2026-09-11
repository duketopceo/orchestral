"""End-to-end dry-run tests for the image task type."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import PNG_MAGIC, Runner


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0,
        retry_limit=1, price_per_image=0.03,
    )


class TestModalityFilter(unittest.TestCase):
    def test_supports_helper(self):
        img = ModelConfig(slug="a/b", name="x", role="worker", input_price_per_mtok=0, output_price_per_mtok=0, metadata={"modalities": ["image"]})
        txt = ModelConfig(slug="c/d", name="y", role="worker", input_price_per_mtok=0, output_price_per_mtok=0)
        self.assertTrue(img.supports("image"))
        self.assertFalse(txt.supports("image"))

    def test_tilde_slug_disabled(self):
        from orchestral.config import load_models
        models = load_models("models")
        self.assertTrue(models)
        self.assertFalse(any(m.slug.startswith("~") for m in models))


class TestImageTaskDryRun(unittest.TestCase):
    def test_dry_run_writes_png_artifact_and_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(
                id="img-test", type="image",
                prompt="Generate a hero image for a coffee site.",
                validation=["non_empty", "png_signature"],
            )
            runner = Runner(runs_dir=tmp, planner="raw", dry_run=True)
            meta = runner.run(task, _model("org/x", "orchestrator"), _model("wrk/img", "worker"))

            run_dir = Path(meta.run_dir)
            artifact = run_dir / "artifact.png"
            self.assertTrue(artifact.exists(), "artifact.png missing")
            self.assertTrue(artifact.read_bytes().startswith(PNG_MAGIC))
            self.assertTrue(meta.passes)

            cost = json.loads((run_dir / "cost.json").read_text())
            phases = [c["phase"] for c in cost]
            self.assertIn("delegate", phases)
            self.assertIn("assemble", phases)

            report = json.loads((run_dir / "report.json").read_text())
            self.assertTrue(report["checks"].get("png_signature"))

    def test_bad_png_fails_validation(self):
        task = TaskSpec(id="img-bad", type="image", prompt="x", validation=["png_signature"])
        runner = Runner(runs_dir=tempfile.mkdtemp(), planner="raw", dry_run=True)
        passes, report = runner._validate_image(task, b"not-a-png")
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["png_signature"])

    def test_empty_bytes_fail_non_empty(self):
        task = TaskSpec(id="img-empty", type="image", prompt="x", validation=["non_empty"])
        runner = Runner(runs_dir=tempfile.mkdtemp(), planner="raw", dry_run=True)
        passes, _ = runner._validate_image(task, b"")
        self.assertFalse(passes)


if __name__ == "__main__":
    unittest.main()
