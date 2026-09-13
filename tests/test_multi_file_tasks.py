"""Tests for the multi-file task type end to end (dry-run + mocked live path)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.fileset import FilesetError
from orchestral.runner import Runner


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "multi-test",
        "type": "multi-file",
        "prompt": "Build a two-file static site.",
        "validation": ["non_empty", "zip_signature", "has_paths"],
        "metadata": {"expected_paths": ["index.html", "style.css"]},
    }
    base.update(kwargs)
    return TaskSpec(**base)


class _FakeClient:
    """Chat stand-in returning a JSON file set per subtask."""

    def __init__(self, file_sets: list[dict] | None = None, content: str | None = None):
        self.file_sets = file_sets or []
        self.content = content
        self.calls = 0
        self.worker_calls = 0

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        self.calls += 1
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}  # the judge sends prose, not a JSON envelope
        if self.content is not None and "subtask" in data:
            body = self.content
        elif "subtask" in data:
            index = min(self.worker_calls, len(self.file_sets) - 1)
            self.worker_calls += 1
            body = json.dumps(self.file_sets[index])
        elif "candidates" in data:
            body = json.dumps({"subtask_id": 0})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [
                {"id": 0, "description": "markup"},
                {"id": 1, "description": "styles"},
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


class TestMultiFileDryRun(unittest.TestCase):
    def test_dry_run_writes_valid_zip_with_expected_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"),
            )
            run_dir = Path(meta.run_dir)
            artifact = run_dir / "artifact.zip"

            self.assertTrue(artifact.exists())
            self.assertTrue(zipfile.is_zipfile(artifact))
            with zipfile.ZipFile(artifact) as archive:
                names = archive.namelist()
            self.assertIn("index.html", names)
            self.assertIn("style.css", names)
            self.assertTrue(meta.passes)

            report = json.loads((run_dir / "report.json").read_text())
            self.assertTrue(report["checks"]["zip_signature"])
            self.assertTrue(report["checks"]["has_paths"])
            # the fake plan repeats the same two paths per subtask, so every
            # overlap is recorded — and no conflict record carries content
            self.assertTrue(all("path" in c for c in report["merge_conflicts"]))
            self.assertNotIn("<!doctype", json.dumps(report["merge_conflicts"]))

            cost = json.loads((run_dir / "cost.json").read_text())
            self.assertTrue(any(c["cost_usd"] > 0 for c in cost))

    def test_dry_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(runs_dir=tmp, planner="raw", dry_run=True)
            first = runner.run(_task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"))
            second = runner.run(_task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"))
            one = (Path(first.run_dir) / "artifact.zip").read_bytes()
            two = (Path(second.run_dir) / "artifact.zip").read_bytes()
            self.assertEqual(one, two)

    def test_worker_records_carry_paths_not_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"),
            )
            run_dir = Path(meta.run_dir)
            record = json.loads((run_dir / "worker-0.json").read_text())
            self.assertIn("index.html", record["paths"])
            self.assertNotIn("<!doctype", json.dumps(record))
            self.assertNotIn("<!doctype", (run_dir / "events.jsonl").read_text())


class TestMultiFileLivePath(unittest.TestCase):
    def test_merge_unions_sets_and_records_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(file_sets=[
                {"files": [{"path": "index.html", "content": "<h1>one</h1>"}]},
                {"files": [
                    {"path": "index.html", "content": "<h1>two</h1>"},
                    {"path": "style.css", "content": "body{}"},
                ]},
            ])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"))

            run_dir = Path(meta.run_dir)
            with zipfile.ZipFile(run_dir / "artifact.zip") as archive:
                self.assertEqual(archive.read("index.html"), b"<h1>two</h1>")
                self.assertEqual(archive.read("style.css"), b"body{}")
            report = json.loads((run_dir / "report.json").read_text())
            self.assertEqual(len(report["merge_conflicts"]), 1)
            self.assertEqual(report["merge_conflicts"][0]["path"], "index.html")
            self.assertTrue(meta.passes)

    def test_contents_never_reach_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(file_sets=[
                {"files": [{"path": "index.html", "content": "UNIQUE_BODY_MARKER"}]},
            ])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(validation=["non_empty", "zip_signature"]),
                  _model("org/x", "orchestrator"), _model("wrk/text", "worker"))

            run_dir = Path(meta.run_dir)
            with zipfile.ZipFile(run_dir / "artifact.zip") as archive:
                self.assertIn(b"UNIQUE_BODY_MARKER", archive.read("index.html"))
            for name in ("worker-0.json", "events.jsonl", "report.json"):
                self.assertNotIn("UNIQUE_BODY_MARKER", (run_dir / name).read_text(),
                                 f"{name} leaked file contents")

    def test_prose_response_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(content="I could not produce files.")
            with self.assertRaises(FilesetError):
                Runner(
                    runs_dir=tmp, planner="raw",
                    clients={"orchestrator": client, "worker": client},
                ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"))

    def test_unsafe_path_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(file_sets=[
                {"files": [{"path": "../escape.txt", "content": "x"}]},
            ])
            with self.assertRaises(FilesetError) as ctx:
                Runner(
                    runs_dir=tmp, planner="raw",
                    clients={"orchestrator": client, "worker": client},
                ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"))
            self.assertIn("escape.txt", str(ctx.exception))

    def test_judge_sees_listing_not_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(file_sets=[
                {"files": [{"path": "index.html", "content": "UNIQUE_BODY_MARKER"}]},
            ])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client, "judge": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/text", "worker"),
                  judge=_model("j/judge", "judge"))

            run_dir = Path(meta.run_dir)
            events = (run_dir / "events.jsonl").read_text()
            self.assertNotIn("UNIQUE_BODY_MARKER", events)
            report = json.loads((run_dir / "report.json").read_text())
            self.assertNotIn("UNIQUE_BODY_MARKER", json.dumps(report))


class TestMultiFileValidation(unittest.TestCase):
    def _check(self, validation, artifact, metadata=None):
        runner = Runner(runs_dir=tempfile.mkdtemp(), dry_run=True)
        task = _task(validation=validation, metadata=metadata or {"expected_paths": ["index.html"]})
        return runner._validate_multi(task, artifact)

    @staticmethod
    def _zip(files: dict[str, str]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, body in files.items():
                archive.writestr(path, body)
        return buffer.getvalue()

    def test_passes_when_expected_paths_present(self):
        passes, report = self._check(
            ["non_empty", "zip_signature", "has_paths"], self._zip({"index.html": "<h1>x</h1>"})
        )
        self.assertTrue(passes)
        self.assertTrue(report["checks"]["has_paths"])

    def test_fails_on_missing_path(self):
        passes, report = self._check(
            ["non_empty", "zip_signature", "has_paths"], self._zip({"style.css": "x"})
        )
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["has_paths"])

    def test_fails_on_empty_expected_file(self):
        passes, _ = self._check(
            ["non_empty", "zip_signature", "has_paths"], self._zip({"index.html": ""})
        )
        self.assertFalse(passes)

    def test_fails_on_directory_entry(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("index.html/", "")
        passes, _ = self._check(["non_empty", "zip_signature", "has_paths"], buffer.getvalue())
        self.assertFalse(passes)

    def test_fails_on_non_zip_and_empty(self):
        self.assertFalse(self._check(["non_empty", "zip_signature"], b"not-a-zip")[0])
        self.assertFalse(self._check(["non_empty", "zip_signature"], b"")[0])

    def test_unknown_check_fails_closed_even_beside_known_ones(self):
        passes, report = self._check(
            ["non_empty", "zip_signature", "bogus_check"], self._zip({"index.html": "x"})
        )
        self.assertFalse(passes)
        self.assertTrue(report["checks"]["zip_signature"])


if __name__ == "__main__":
    unittest.main()
