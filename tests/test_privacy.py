"""Tests for scrub/publish: binary passthrough, widened redaction, allowlist, manifest."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from orchestral.planners import TINY_PNG as PNG_BYTES
from orchestral.privacy import scrub_all, scrub_run, scrub_text


def _make_run(runs_dir: Path, name: str = "o/t/w/abc123", files: dict[str, bytes] | None = None) -> Path:
    run_dir = runs_dir / name
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "abc123",
        "orchestrator": "org/orch",
        "task_id": "t",
        "worker": "org/work",
        "status": "finished",
        "score": 8.5,
        "passes": True,
        "total_cost_usd": 0.01,
    }))
    for fname, data in (files or {}).items():
        (run_dir / fname).write_bytes(data)
    return run_dir


def _fake_key(prefix: str, body: str) -> str:
    """Assemble credential-shaped test input at runtime.

    Key-shaped literals in source trip secret scanners even when fake,
    so tests build them from prefix + body fragments.
    """
    return prefix + body


class TestScrubText(unittest.TestCase):
    def test_redacts_widened_credential_patterns(self):
        text = (
            "keys: "
            + _fake_key("sk-or-", "abcdef1234567890abcdef1234") + " "
            + _fake_key("sk-", "abcdef1234567890abcdef1234") + " "
            + _fake_key("sk-ant-", "api03-abcdef1234567890abcdef") + " "
            + _fake_key("ghp_", "abcdef1234567890abcdef1234567890ab") + " "
            + _fake_key("github_pat_", "11ABCDEFG0abcdef1234567890_abcdef1234567890abcdef1234567890abcdef") + " "
            + _fake_key("AKIA", "IOSFODNN7EXAMPLE") + " "
            + _fake_key("AIza", "SyD4iE2xVSpkLLOXoyq2uexnFX1XvvWWi70") + " "
            + _fake_key("gsk_", "abcdef1234567890abcdef1234567890") + " "
            + _fake_key("xai-", "abcdef1234567890abcdef1234567890")
        )
        out = scrub_text(text)
        for token in ("sk-or-abc", "sk-abcdef", "sk-ant", "ghp_", "github_pat_",
                      "AKIA", "AIza", "gsk_", "xai-"):
            self.assertNotIn(token, out, token)
        self.assertIn("[REDACTED_api_key]", out)

    def test_redacts_pem_and_windows_paths(self):
        pem = "-----BEGIN PRIVATE " + "KEY-----\nMIIabc\n-----END PRIVATE " + "KEY-----"
        out = scrub_text(pem + " at C:\\Users\\alice\\secret.txt")
        self.assertNotIn("MIIabc", out)
        self.assertNotIn("C:\\Users\\alice", out)

    def test_strips_url_userinfo(self):
        out = scrub_text("base_url=https://user:pass@llm.internal.corp/v1")
        self.assertNotIn("user:pass", out)
        self.assertIn("[REDACTED_AUTH]@", out)

    def test_redacts_internal_hostnames(self):
        out = scrub_text("endpoint llm.internal.corp resolved")
        self.assertNotIn("llm.internal.corp", out)


class TestScrubRun(unittest.TestCase):
    def test_binary_png_roundtrips_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={"screenshot.png": PNG_BYTES})
            out = scrub_run(src, Path(td) / "pub")
            self.assertEqual((out / "screenshot.png").read_bytes(), PNG_BYTES)

    def test_png_with_secret_bytes_not_rewritten(self):
        payload = PNG_BYTES + _fake_key("sk-or-", "abcdef1234567890abcdef1234").encode()
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={"artifact.png": payload})
            out = scrub_run(src, Path(td) / "pub")
            self.assertEqual((out / "artifact.png").read_bytes(), payload)

    def test_jsonl_secrets_redacted(self):
        line = json.dumps({"key": _fake_key("sk-or-", "abcdef1234567890abcdef1234"), "p": "/home/u/x"})
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={"events.jsonl": (line + "\n").encode()})
            out = scrub_run(src, Path(td) / "pub")
            text = (out / "events.jsonl").read_text()
            self.assertNotIn("sk-or-abc", text)
            self.assertNotIn("/home/u", text)

    def test_url_userinfo_stripped_in_run_json(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs")
            meta = json.loads((src / "run.json").read_text())
            meta["config"] = {"worker": {"base_url": "https://user:pass@llm.internal.corp/v1"}}
            (src / "run.json").write_text(json.dumps(meta))
            out = scrub_run(src, Path(td) / "pub")
            text = (out / "run.json").read_text()
            self.assertNotIn("user:pass", text)
            self.assertNotIn("internal.corp", text)

    def test_non_allowlisted_files_stay_behind(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={
                "notes.txt": b"do not publish",
                ".env": b"SECRET=1",
                "artifact.html": b"<html></html>",
            })
            out = scrub_run(src, Path(td) / "pub")
            self.assertFalse((out / "notes.txt").exists())
            self.assertFalse((out / ".env").exists())
            self.assertTrue((out / "artifact.html").exists())

    def test_malformed_json_falls_back_to_text_scrub(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs")
            (src / "report.json").write_text('{"partial": true, "key": "' + _fake_key("sk-or-", "abcdef1234567890abcdef1234") + '"')
            out = scrub_run(src, Path(td) / "pub")
            text = (out / "report.json").read_text()
            self.assertNotIn("sk-or-abc", text)
            self.assertIn("partial", text)


class TestScrubAllManifest(unittest.TestCase):
    def test_manifest_indexes_runs(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, "o/t/w/run1")
            _make_run(runs_dir, "o2/t2/w2/run2")
            out_dir = Path(td) / "pub"
            copied = scrub_all(runs_dir, out_dir)
            self.assertEqual(len(copied), 2)
            manifest = json.loads((out_dir / "manifest.json").read_text())
            self.assertEqual(len(manifest), 2)
            self.assertEqual(manifest[0]["run_id"], "abc123")
            self.assertIn("run", manifest[0])

    def test_manifest_tolerates_malformed_run_json(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/bad")
            (run_dir / "run.json").write_text("{not json")
            out_dir = Path(td) / "pub"
            copied = scrub_all(runs_dir, out_dir)
            self.assertEqual(len(copied), 1)
            manifest = json.loads((out_dir / "manifest.json").read_text())
            self.assertEqual(len(manifest), 1)
            self.assertNotIn("run_id", manifest[0])


class TestScrubArchivePolicy(unittest.TestCase):
    def test_archive_omitted_and_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            with zipfile.ZipFile(run_dir / "artifact.zip", "w") as archive:
                archive.writestr("index.html", "SECRET_IN_ARCHIVE")
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "artifact.zip").exists())
            manifest = json.loads((out_dir / "manifest.json").read_text())
            omissions = manifest[0]["scrub_omissions"]
            self.assertEqual([o["file"] for o in omissions], ["artifact.zip"])
            self.assertIn("cannot be redacted", omissions[0]["reason"])
            # the warning names the run; the file name lives in the manifest
            self.assertIn("o/t/w/run1", stderr.getvalue())

    def test_other_binaries_still_copied(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "artifact.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                scrub_all(runs_dir, out_dir)

            self.assertTrue((out_dir / "o/t/w/run1" / "artifact.png").exists())
            manifest = json.loads((out_dir / "manifest.json").read_text())
            self.assertNotIn("scrub_omissions", manifest[0])
            self.assertEqual(stderr.getvalue(), "")

    def test_text_redaction_still_applies_alongside_archive_skip(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            with zipfile.ZipFile(run_dir / "artifact.zip", "w") as archive:
                archive.writestr("index.html", "SECRET_IN_ARCHIVE")
            secret = "sk-or-" + "a" * 30
            (run_dir / "worker-0.json").write_text(json.dumps({"completion": {"content": secret}}))
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "artifact.zip").exists())
            scrubbed = (out_dir / "o/t/w/run1" / "worker-0.json").read_text()
            self.assertNotIn(secret, scrubbed)
            self.assertIn("REDACTED", scrubbed)


if __name__ == "__main__":
    unittest.main()
