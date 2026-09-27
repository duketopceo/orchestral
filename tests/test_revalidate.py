"""Mechanical re-validation: replay validators on stored artifacts, repair the score axis."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from orchestral.config import ModelConfig, TaskSpec
from orchestral.fileset import read_zip
from orchestral.revalidate import revalidate_runs
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


class TestRevalidate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "tasks").mkdir()
        (self.root / "tasks" / "t-task.yaml").write_text(
            "id: t-task\ntype: html\nprompt: make a page\nvalidation:\n  - non_empty\n")
        self.runs = self.root / "runs"

    def _seed(self, spec: TaskSpec | None = None) -> str:
        meta = Runner(dry_run=True, runs_dir=str(self.runs),
                      store=RunStore(self.runs)).run(
            spec or TaskSpec(id="t-task", type="html", prompt="make a page"),
            _model("o/model", "orchestrator"), _model("w/model", "worker"))
        return meta.run_id

    def _pollute(self, run_id: str, *, judge_score: float = 0.9) -> Path:
        """Simulate the pre-fix corruption: judge score in score, no passes field."""
        store = RunStore(self.runs)
        run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
        report_path = run_dir / "report.json"
        report = json.loads(report_path.read_text())
        report["score"] = judge_score
        report.pop("passes", None)
        report["judge"] = {"score": judge_score, "passed": True, "model": "j/model"}
        report_path.write_text(json.dumps(report))
        meta = store.get_run(run_id)
        meta.score = judge_score  # type: ignore[union-attr]
        store.update_meta(meta)  # type: ignore[union-attr]
        return run_dir

    def test_restores_mechanical_axis(self):
        run_id = self._seed()
        run_dir = self._pollute(run_id, judge_score=0.9)
        res = revalidate_runs(RunStore(self.runs), tasks_dir=self.root / "tasks")
        self.assertEqual(res["repaired"], 1)
        report = json.loads((run_dir / "report.json").read_text())
        self.assertIsNone(report["score"])          # html has no mechanical score
        self.assertTrue(report["passes"])           # mechanical verdict restored
        stamp = report["revalidated"]
        self.assertEqual(stamp["score_was"], 0.9)   # old value preserved for audit
        meta = RunStore(self.runs).get_run(run_id)
        self.assertIsNone(meta.score)               # type: ignore[union-attr]
        self.assertTrue(meta.passes)                # type: ignore[union-attr]
        # judge axis untouched
        self.assertEqual(report["judge"]["score"], 0.9)

    def test_idempotent_second_pass(self):
        """First pass adds the missing `passes` field; second is a no-op."""
        self._seed()
        first = revalidate_runs(RunStore(self.runs), tasks_dir=self.root / "tasks")
        self.assertEqual(first["repaired"], 1)  # report.json gains `passes`
        res = revalidate_runs(RunStore(self.runs), tasks_dir=self.root / "tasks")
        self.assertEqual(res["unchanged"], 1)
        self.assertEqual(res["repaired"], 0)

    def test_dry_run_writes_nothing(self):
        run_id = self._seed()
        run_dir = self._pollute(run_id)
        res = revalidate_runs(RunStore(self.runs), dry_run=True,
                              tasks_dir=self.root / "tasks")
        self.assertEqual(res["repaired"], 1)
        report = json.loads((run_dir / "report.json").read_text())
        self.assertEqual(report["score"], 0.9)      # still polluted
        self.assertNotIn("revalidated", report)
        meta = RunStore(self.runs).get_run(run_id)
        self.assertEqual(meta.score, 0.9)           # type: ignore[union-attr]

    def test_missing_artifact_skipped(self):
        run_id = self._seed()
        run_dir = Path(RunStore(self.runs).get_run(run_id).run_dir)  # type: ignore[union-attr]
        for a in run_dir.glob("artifact.*"):
            a.unlink()
        res = revalidate_runs(RunStore(self.runs), tasks_dir=self.root / "tasks")
        self.assertEqual(res["skipped"], 1)
        self.assertIn("artifact", res["results"][0]["skipped"])

    def test_missing_spec_skipped(self):
        self._seed(TaskSpec(id="ghost-task", type="html", prompt="p"))
        res = revalidate_runs(RunStore(self.runs), tasks_dir=self.root / "tasks")
        self.assertEqual(res["skipped"], 1)
        self.assertIn("spec", res["results"][0]["skipped"])

    def test_failed_validation_sets_failure_reason(self):
        run_id = self._seed()
        store = RunStore(self.runs)
        run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
        (run_dir / "artifact.html").write_text("")   # fails non_empty
        res = revalidate_runs(store, tasks_dir=self.root / "tasks")
        self.assertEqual(res["repaired"], 1)
        meta = store.get_run(run_id)
        self.assertFalse(meta.passes)               # type: ignore[union-attr]
        self.assertEqual(meta.failure_reason, "validation")  # type: ignore[union-attr]

    def test_code_task_replays_suite(self):
        """A zip artifact is unzipped and _validate_code re-executes the suite."""
        (self.root / "tasks" / "t-code.yaml").write_text(
            "id: t-code\ntype: code\nprompt: write fizzbuzz\n"
            "metadata:\n  module: solution.py\n  tests: |\n    import unittest\n")
        run_id = self._seed(TaskSpec(id="t-code", type="code", prompt="write fizzbuzz",
                                     metadata={"module": "solution.py", "tests": "import unittest\n"}))
        store = RunStore(self.runs)
        run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
        zpath = run_dir / "artifact.zip"
        if not zpath.exists():
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("solution.py", "def fizzbuzz(n):\n    return n\n")
        run_dir = self._pollute(run_id, judge_score=0.2)
        suite = {"executed": True, "tests_run": 4, "ok": True, "output_tail": "OK"}
        with patch("orchestral.runner.run_unittest_suite", return_value=suite):
            res = revalidate_runs(store, tasks_dir=self.root / "tasks")
        self.assertEqual(res["repaired"], 1)
        report = json.loads((run_dir / "report.json").read_text())
        self.assertEqual(report["score"], 1.0)      # suite fraction, not the 0.2 judge score
        meta = store.get_run(run_id)
        self.assertEqual(meta.score, 1.0)           # type: ignore[union-attr]


class TestReadZip(unittest.TestCase):
    def test_round_trip(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.py", "x = 1")
            zf.writestr("dir/", "")
            zf.writestr("bin.dat", b"\xff\xfe")
        out = read_zip(buf.getvalue())
        self.assertEqual(out, {"a.py": "x = 1"})  # dir + binary dropped

    def test_empty_zip(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        self.assertEqual(read_zip(buf.getvalue()), {})


if __name__ == "__main__":
    unittest.main()
