"""Frontier-model review: digest, schema validation, batch, corpus report."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.review import _validate_review, build_run_digest, run_review_batch
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


def _seed_run(runs_dir: str) -> str:
    meta = Runner(dry_run=True, runs_dir=runs_dir, store=RunStore(runs_dir)).run(
        TaskSpec(id="t-task", type="html", prompt="p"),
        _model("o/model", "orchestrator"), _model("w/model", "worker"),
    )
    return meta.run_id


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def chat(self, **kw):
        self.calls += 1
        return {"content": self.content, "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "latency_ms": 5.0}


class TestReview(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "tasks").mkdir()
        (self.root / "tasks" / "t-task.yaml").write_text("id: t-task\ntype: html\nprompt: p\nvalidation:\n  - non_empty\n")
        self.runs = self.root / "runs"
        self.reports = self.root / "reports"
        self.reviewer = _model("x-ai/grok-4.3", "reference")

    def test_digest_has_run_task_plan_report(self):
        run_id = _seed_run(str(self.runs))
        meta = RunStore(self.runs).list_runs()[0]
        digest = build_run_digest(Path(meta.run_dir), meta, self.root / "tasks")
        self.assertEqual(digest["run"]["run_id"], run_id)
        self.assertEqual(digest["task"]["type"], "html")
        self.assertIn("plan", digest)
        self.assertIn("report", digest)
        self.assertIn("events", digest)
        self.assertIn("run.completed", digest["events"]["counts"])

    def test_validate_review_enforces_schema(self):
        good = _validate_review({
            "run_quality": "suspect",
            "verdict": "v",
            "findings": [
                {"kind": "contract", "severity": "high", "claim": "c", "evidence": "e"},
                {"kind": "bogus", "severity": "extreme", "claim": "c2", "evidence": "e2"},
                {"claim": "no evidence — dropped"},
            ],
            "suggested_check": "check",
        })
        self.assertEqual(good["run_quality"], "suspect")
        self.assertEqual(len(good["findings"]), 2)
        self.assertEqual(good["findings"][1]["kind"], "model")  # bad kind falls back
        self.assertEqual(good["findings"][1]["severity"], "low")

    def test_batch_dry_run_writes_review_json(self):
        _seed_run(str(self.runs))
        _seed_run(str(self.runs))
        result = run_review_batch(
            RunStore(self.runs), self.reviewer, None,
            dry_run=True, reports_dir=self.reports, tasks_dir=self.root / "tasks",
        )
        self.assertEqual(result["reviewed"], 2)
        self.assertEqual(result["cost_usd"], 0.0)
        for meta in RunStore(self.runs).list_runs():
            rec = json.loads((Path(meta.run_dir) / "review.json").read_text())
            self.assertEqual(rec["reviewer"], "x-ai/grok-4.3")
            self.assertEqual(rec["run_quality"], "clean")
        self.assertTrue(list(self.reports.glob("review-*.md")))

    def test_batch_skips_existing_unless_force(self):
        _seed_run(str(self.runs))
        store = RunStore(self.runs)
        run_review_batch(store, self.reviewer, None, dry_run=True, reports_dir=self.reports, tasks_dir=self.root / "tasks")
        result = run_review_batch(store, self.reviewer, None, dry_run=True, reports_dir=self.reports, tasks_dir=self.root / "tasks")
        self.assertEqual(result["reviewed"], 0)
        self.assertEqual(result["skipped"], 1)
        result = run_review_batch(store, self.reviewer, None, dry_run=True, force=True, reports_dir=self.reports, tasks_dir=self.root / "tasks")
        self.assertEqual(result["reviewed"], 1)

    def test_batch_with_fake_client_records_cost(self):
        _seed_run(str(self.runs))
        client = FakeClient(json.dumps({
            "run_quality": "suspect", "verdict": "contract gap",
            "findings": [{"kind": "prompt", "severity": "high", "claim": "schema unnamed", "evidence": "plan"}],
            "suggested_check": "grep prompt for 'subtasks'",
        }))
        result = run_review_batch(
            RunStore(self.runs), self.reviewer, client,
            reports_dir=self.reports, tasks_dir=self.root / "tasks",
        )
        self.assertEqual(client.calls, 2)  # per-run + synthesis
        self.assertGreater(result["cost_usd"], 0)
        rec = json.loads(next(Path(meta.run_dir) for meta in RunStore(self.runs).list_runs()).joinpath("review.json").read_text())
        self.assertEqual(rec["run_quality"], "suspect")
        self.assertEqual(rec["findings"][0]["kind"], "prompt")

    def test_reviewer_api_error_records_instead_of_crashing(self):
        _seed_run(str(self.runs))

        class DeadClient:
            def chat(self, **kw):
                raise RuntimeError("provider 404")

        result = run_review_batch(
            RunStore(self.runs), self.reviewer, DeadClient(),
            reports_dir=self.reports, tasks_dir=self.root / "tasks",
        )
        rec = result["reviews"][0]
        self.assertEqual(rec["run_quality"], "suspect")
        self.assertIn("provider 404", rec["review_error"])
        self.assertEqual(result["cost_usd"], 0.0)

    def test_unparseable_reviewer_output_marks_suspect(self):
        _seed_run(str(self.runs))
        client = FakeClient("not json at all")
        result = run_review_batch(
            RunStore(self.runs), self.reviewer, client,
            reports_dir=self.reports, tasks_dir=self.root / "tasks",
        )
        self.assertEqual(result["reviews"][0]["run_quality"], "suspect")
        self.assertTrue(result["reviews"][0].get("parse_failed"))


if __name__ == "__main__":
    unittest.main()
