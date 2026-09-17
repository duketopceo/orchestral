"""Tests for swe-patch — unified-diff apply + hidden-test validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.patch import PatchError, apply_unified_diff, extract_patch
from orchestral.runner import Runner

FILES = {
    "config.py": 'DEFAULTS = {\n    "timeout_ms": 5000,\n    "retries": 3,\n}\n\ndef load(overrides=None):\n    cfg = dict(DEFAULTS)\n    cfg.update(overrides or {})\n    return cfg\n',
    "app.py": 'from config import load\n\ndef main():\n    cfg = load({"timeout_ms": 9})\n    return cfg["timeout_ms"]\n',
}
DIFF = '''--- a/config.py
+++ b/config.py
@@ -1,4 +1,4 @@
 DEFAULTS = {
-    "timeout_ms": 5000,
+    "request_timeout_ms": 5000,
     "retries": 3,
 }
--- a/app.py
+++ b/app.py
@@ -3,3 +3,3 @@
 def main():
-    cfg = load({"timeout_ms": 9})
-    return cfg["timeout_ms"]
+    cfg = load({"request_timeout_ms": 9})
+    return cfg["request_timeout_ms"]
'''


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1)


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "patch-test",
        "type": "swe-patch",
        "prompt": "Rename the key via a diff.",
        "metadata": {
            "files": FILES,
            "patch": DIFF,
            "module": "config.py",
            "tests": "import unittest\nfrom config import load\n\nclass T(unittest.TestCase):\n    def test_renamed(self):\n        self.assertIn('request_timeout_ms', load())\n",
        },
    }
    base.update(kwargs)
    return TaskSpec(**base)


class TestApply(unittest.TestCase):
    def test_clean_apply(self):
        out = apply_unified_diff(FILES, DIFF)
        self.assertIn('"request_timeout_ms": 5000', out["config.py"])
        self.assertIn('cfg["request_timeout_ms"]', out["app.py"])

    def test_context_mismatch_raises(self):
        bad = DIFF.replace('"timeout_ms": 5000', '"other": 1')
        with self.assertRaises(PatchError):
            apply_unified_diff(FILES, bad)

    def test_bare_paths_no_prefix(self):
        bare = DIFF.replace("a/config.py", "config.py").replace("b/config.py", "config.py") \
                   .replace("a/app.py", "app.py").replace("b/app.py", "app.py")
        out = apply_unified_diff(FILES, bare)
        self.assertIn("request_timeout_ms", out["config.py"])

    def test_malformed_hunk_header(self):
        with self.assertRaises(PatchError):
            apply_unified_diff(FILES, "--- a/x\n+++ b/x\n@@ nope @@\n")

    def test_new_file_created(self):
        diff = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+hello\n"
        out = apply_unified_diff(FILES, diff)
        self.assertEqual(out["new.txt"], "hello\n")


class TestExtractPatch(unittest.TestCase):
    def test_json_wrapper(self):
        self.assertEqual(extract_patch(json.dumps({"patch": DIFF})), DIFF)

    def test_raw_diff(self):
        self.assertEqual(extract_patch(DIFF), DIFF)

    def test_no_diff(self):
        self.assertIsNone(extract_patch("no diff here"))
        self.assertIsNone(extract_patch('{"patch": 42}'))


class _FakeClient:
    def __init__(self, payloads, pick=0):
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
            body = json.dumps({"subtasks": [{"id": 0, "description": "patch it"}]})
        else:
            body = json.dumps({"score": 0.5, "passed": True, "reasoning": "ok"})
        return {"content": body, "usage": {"prompt_tokens": 20, "completion_tokens": 10}, "latency_ms": 1, "id": "fake"}

    def close(self):
        pass


class TestPatchRunner(unittest.TestCase):
    def test_dry_run_applies_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"),
            )
            self.assertTrue(meta.passes)
            self.assertTrue((Path(meta.run_dir) / "artifact.diff").exists())

    def test_live_valid_patch_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(payloads=[json.dumps({"patch": DIFF})])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)

    def test_live_bad_context_fails_at_apply(self):
        bad = DIFF.replace('"timeout_ms": 5000', '"renamed_already": 1')
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(payloads=[json.dumps({"patch": bad})])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertFalse(meta.passes)
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertFalse(report["checks"]["applies"])

    def test_live_no_diff_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(payloads=["I rewrote it in prose"])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertFalse(meta.passes)


if __name__ == "__main__":
    unittest.main()
