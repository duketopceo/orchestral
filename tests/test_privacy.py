"""Tests for scrub/publish: binary passthrough, widened redaction, allowlist, manifest.

The publication policy is a fail-closed allow, so it is tested in both
directions: content that cannot be redacted is withheld even when its name says
otherwise, and approved media is never withheld because a signature check
reached too far.
"""

from __future__ import annotations

import bz2
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


def manifest_blocked(out_dir: Path) -> dict[str, dict[str, str]]:
    """Blocked-file records from the first run's manifest entry, keyed by filename."""
    manifest = json.loads((out_dir / "manifest.json").read_text())
    return {item["file"]: item for item in manifest[0]["scrub_blocked"]}


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
            self.assertIn("o/t/w/run1", stderr.getvalue())

    def test_unapproved_archive_extensions_are_omitted(self):
        for suffix in (".7z", ".bz2", ".xz", ".rar"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as td:
                runs_dir = Path(td) / "runs"
                run_dir = _make_run(runs_dir, "o/t/w/run1")
                (run_dir / f"artifact{suffix}").write_bytes(b"harmless archive fixture")
                out_dir = Path(td) / "pub"

                with contextlib.redirect_stderr(io.StringIO()):
                    scrub_all(runs_dir, out_dir)

                self.assertFalse((out_dir / "o/t/w/run1" / f"artifact{suffix}").exists())
                manifest = json.loads((out_dir / "manifest.json").read_text())
                self.assertEqual(
                    [item["file"] for item in manifest[0]["scrub_omissions"]],
                    [f"artifact{suffix}"],
                )
                self.assertEqual(manifest[0]["scrub_blocked"][0]["status"], "blocked")

    def test_database_extensions_are_omitted(self):
        for suffix in (".db", ".sqlite", ".sqlite3"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as td:
                runs_dir = Path(td) / "runs"
                run_dir = _make_run(runs_dir, "o/t/w/run1")
                (run_dir / f"artifact{suffix}").write_bytes(b"harmless database fixture")
                out_dir = Path(td) / "pub"

                with contextlib.redirect_stderr(io.StringIO()):
                    scrub_all(runs_dir, out_dir)

                self.assertFalse((out_dir / "o/t/w/run1" / f"artifact{suffix}").exists())
                manifest = json.loads((out_dir / "manifest.json").read_text())
                self.assertEqual(
                    [item["file"] for item in manifest[0]["scrub_omissions"]],
                    [f"artifact{suffix}"],
                )

    def test_unknown_binary_is_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "artifact.bin").write_bytes(b"harmless\x00binary fixture")
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "artifact.bin").exists())
            manifest = json.loads((out_dir / "manifest.json").read_text())
            self.assertEqual(manifest[0]["scrub_omissions"][0]["file"], "artifact.bin")
            self.assertIn("binary", manifest[0]["scrub_omissions"][0]["reason"])

    def test_archive_signature_blocks_approved_binary_name(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "artifact.png").write_bytes(b"PK\x03\x04harmless archive fixture")
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "artifact.png").exists())
            manifest = json.loads((out_dir / "manifest.json").read_text())
            self.assertIn("archive", manifest[0]["scrub_omissions"][0]["reason"])

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

    def test_manifest_requires_manual_inspection_and_second_scanner(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, "o/t/w/run1")
            out_dir = Path(td) / "pub"

            scrub_all(runs_dir, out_dir)

            manifest = json.loads((out_dir / "manifest.json").read_text())
            self.assertEqual(manifest[0]["scrub_policy"], "fail_closed")
            self.assertTrue(manifest[0]["publication_review"]["manual_inspection_required"])
            self.assertTrue(manifest[0]["publication_review"]["second_scanner_required"])

    def test_scrub_all_removes_stale_blocked_output(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, "o/t/w/run1")
            out_dir = Path(td) / "pub"
            stale = out_dir / "o/t/w/run1/artifact.db"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale database fixture")
            scrub_all(runs_dir, out_dir)
            self.assertFalse(stale.exists())

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


class TestScrubRunPolicy(unittest.TestCase):
    def test_direct_scrub_run_blocks_all_unapproved_file_types(self):
        files = {
            "artifact.7z": b"harmless archive fixture",
            "artifact.bz2": b"harmless archive fixture",
            "artifact.xz": b"harmless archive fixture",
            "artifact.rar": b"harmless archive fixture",
            "artifact.db": b"harmless database fixture",
            "artifact.bin": b"harmless\x00binary fixture",
        }
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files=files)
            out = scrub_run(src, Path(td) / "pub")

            for name in files:
                self.assertFalse((out / name).exists(), name)
            self.assertTrue((out / "run.json").exists())

    def test_direct_scrub_run_removes_stale_blocked_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs")
            out_dir = Path(td) / "pub"
            stale = out_dir / src.name / "artifact.db"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale database fixture")

            out = scrub_run(src, out_dir)

            self.assertFalse(stale.exists())
            self.assertTrue((out / "run.json").exists())

    def test_direct_scrub_run_blocks_allowlisted_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs")
            target = src / "outside.txt"
            target.write_text("harmless fixture", encoding="utf-8")
            (src / "artifact.link").symlink_to(target)
            out = scrub_run(src, Path(td) / "pub")

            self.assertFalse((out / "artifact.link").exists())


class TestPublicationPolicyBothDirections(unittest.TestCase):
    """A block that is never lifted is a regression; so is a block that never fires.

    Every case here pairs a file the policy must withhold with a file it must
    let through. The let-through cases are the ones that catch an over-broad
    signature check, which is the failure mode that silently drops a user's
    artifacts.
    """

    # -- must block: content that cannot be redacted, whatever it is called --

    def test_renamed_archive_is_blocked_by_content_not_extension(self):
        renamed = {
            "artifact.png": b"PK\x03\x04" + b"\x00" * 64,
            "worker-0.mp4": b"\x1f\x8b\x08\x00" + b"\x00" * 64,
            "worker-0.webm": b"Rar!\x1a\x07\x00" + b"\x00" * 64,
            "screenshot.png": b"7z\xbc\xaf\x27\x1c" + b"\x00" * 64,
            "artifact.png.gz": b"harmless bytes, archive extension",
        }
        for name, data in renamed.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                runs_dir = Path(td) / "runs"
                run_dir = _make_run(runs_dir, "o/t/w/run1")
                (run_dir / name).write_bytes(data)
                out_dir = Path(td) / "pub"

                with contextlib.redirect_stderr(io.StringIO()):
                    scrub_all(runs_dir, out_dir)

                self.assertFalse((out_dir / "o/t/w/run1" / name).exists(), name)
                self.assertEqual(manifest_blocked(out_dir)[name]["status"], "blocked")

    def test_real_bzip2_stream_is_blocked_under_a_text_name(self):
        payload = bz2.compress(b"SECRET_IN_ARCHIVE" * 64)
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "worker-0.log").write_bytes(payload)
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "worker-0.log").exists())
            self.assertEqual(
                manifest_blocked(out_dir)["worker-0.log"]["status"], "blocked"
            )

    def test_renamed_sqlite_database_is_blocked(self):
        payload = b"SQLite format 3\x00" + b"\x00" * 64
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "artifact.png").write_bytes(payload)
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "artifact.png").exists())
            self.assertIn("database", manifest_blocked(out_dir)["artifact.png"]["reason"])

    def test_non_utf8_text_is_blocked_rather_than_published_mangled(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "worker-0.log").write_bytes(b"caf\xe9 notes\n")
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "worker-0.log").exists())
            self.assertIn("non-text", manifest_blocked(out_dir)["worker-0.log"]["reason"])

    def test_blocked_symlink_is_recorded_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            outside = run_dir / "outside.txt"
            outside.write_text("harmless fixture", encoding="utf-8")
            (run_dir / "artifact.link").symlink_to(outside)
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertFalse((out_dir / "o/t/w/run1" / "artifact.link").exists())
            self.assertIn("symbolic link", manifest_blocked(out_dir)["artifact.link"]["reason"])

    # -- must publish: approved media is a narrow allow, not a general one --

    def test_approved_png_with_archive_bytes_at_offset_publishes_byte_identical(self):
        """A real PNG that happens to embed a ZIP local header deeper in the file.

        The signature check is anchored at offset 0. If it ever becomes a
        substring scan, this stops publishing and a real screenshot is dropped.
        """
        self.assertGreater(len(PNG_BYTES), 32, "fixture too small to splice into")
        payload = PNG_BYTES[:20] + b"PK\x03\x04" + PNG_BYTES[20:]
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "screenshot.png").write_bytes(payload)
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                scrub_all(runs_dir, out_dir)

            self.assertEqual(
                (out_dir / "o/t/w/run1" / "screenshot.png").read_bytes(), payload
            )
            self.assertEqual(stderr.getvalue(), "")

    def test_approved_media_headers_all_publish(self):
        approved = {
            "screenshot.png": PNG_BYTES,
            "screenshot.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 32,
            "screenshot.gif": b"GIF89a" + b"\x00" * 32,
            "screenshot.webp": b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 32,
            "screenshot.bmp": b"BM" + b"\x00" * 32,
            "screenshot.ico": b"\x00\x00\x01\x00" + b"\x00" * 32,
            "worker-0.mp4": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32,
            "worker-0.mov": b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 32,
            "artifact.woff": b"wOFF" + b"\x00" * 32,
            "artifact.woff2": b"wOF2" + b"\x00" * 32,
            "artifact.ttf": b"\x00\x01\x00\x00" + b"\x00" * 32,
            "artifact.otf": b"OTTO" + b"\x00" * 32,
        }
        for name, data in approved.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                runs_dir = Path(td) / "runs"
                run_dir = _make_run(runs_dir, "o/t/w/run1")
                (run_dir / name).write_bytes(data)
                out_dir = Path(td) / "pub"

                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    scrub_all(runs_dir, out_dir)

                self.assertEqual(
                    (out_dir / "o/t/w/run1" / name).read_bytes(), data, name
                )
                self.assertEqual(stderr.getvalue(), "", name)

    def test_text_file_starting_with_bzip2_magic_bytes_still_publishes(self):
        """`BZh` is three ASCII characters, and a text log may open with them.

        The bzip2 signature has to match the real stream header, not just its
        first three bytes, or a prose log gets withheld as an archive.
        """
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            payload = b"BZh was the first word of this line, not a stream header\n"
            (run_dir / "worker-0.log").write_bytes(payload)
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                scrub_all(runs_dir, out_dir)

            published = out_dir / "o/t/w/run1" / "worker-0.log"
            self.assertTrue(published.exists())
            self.assertEqual(published.read_bytes(), payload)
            self.assertEqual(stderr.getvalue(), "")

    def test_text_file_mentioning_archive_magic_publishes(self):
        payload = b"we saw PK\\x03\\x04 and 1f8b and BZh in the log stream\n"
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            run_dir = _make_run(runs_dir, "o/t/w/run1")
            (run_dir / "worker-0.log").write_bytes(payload)
            out_dir = Path(td) / "pub"

            with contextlib.redirect_stderr(io.StringIO()):
                scrub_all(runs_dir, out_dir)

            self.assertEqual(
                (out_dir / "o/t/w/run1" / "worker-0.log").read_bytes(), payload
            )


class TestScrubRunBlockedEvidence(unittest.TestCase):
    """`scrub_run` has no manifest, so a silent withhold is an invisible hole."""

    def test_scrub_run_names_every_blocked_file_on_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={
                "artifact.zip": b"harmless archive fixture",
                "artifact.db": b"harmless database fixture",
                "screenshot.png": PNG_BYTES,
            })

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                out = scrub_run(src, Path(td) / "pub")

            self.assertFalse((out / "artifact.zip").exists())
            self.assertFalse((out / "artifact.db").exists())
            self.assertTrue((out / "screenshot.png").exists())
            warning = stderr.getvalue()
            self.assertIn("artifact.zip", warning)
            self.assertIn("artifact.db", warning)
            self.assertIn("scrub", warning)

    def test_scrub_run_stays_quiet_when_nothing_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={"screenshot.png": PNG_BYTES})

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                scrub_run(src, Path(td) / "pub")

            self.assertEqual(stderr.getvalue(), "")


class TestScrubOutputContainment(unittest.TestCase):
    """A scrub destination that overlaps the source would delete the source.

    `_scrub_dir` clears its destination before writing, so a destination equal
    to the source run directory is data loss, not a cosmetic bug.
    """

    def test_scrub_all_refuses_to_write_over_its_own_source(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, "o/t/w/run1")

            with self.assertRaises(ValueError):
                scrub_all(runs_dir, runs_dir)

    def test_scrub_all_refuses_a_destination_containing_the_source(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, "o/t/w/run1")

            with self.assertRaises(ValueError):
                scrub_all(runs_dir, Path(td) / "pub" / "..")

    def test_scrub_run_refuses_to_write_over_its_own_source(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={"screenshot.png": PNG_BYTES})

            with self.assertRaises(ValueError):
                scrub_run(src, src.parent)

    def test_scrub_run_leaves_the_source_intact_after_a_blocked_file(self):
        with tempfile.TemporaryDirectory() as td:
            src = _make_run(Path(td) / "runs", files={
                "screenshot.png": PNG_BYTES,
                "artifact.db": b"harmless database fixture",
            })

            with contextlib.redirect_stderr(io.StringIO()):
                out = scrub_run(src, Path(td) / "pub")

            self.assertFalse((out / "artifact.db").exists())
            self.assertTrue((src / "artifact.db").exists())
            self.assertEqual((src / "screenshot.png").read_bytes(), PNG_BYTES)


class TestScrubCommandExitStatus(unittest.TestCase):
    """`harness.py scrub` must fail when the published record has a hole.

    The manifest records every withheld file, but a CI publish step reads the
    exit status, not the manifest. If a withheld file still exits 0, the only
    way to notice is a human reading stderr, and that is the invisible hole
    this publication policy exists to avoid.
    """

    def _run_cmd(self, runs_dir: Path, out_dir: Path) -> tuple[int, str]:
        import argparse
        from contextlib import redirect_stderr, redirect_stdout

        from harness import cmd_scrub

        args = argparse.Namespace(runs_dir=str(runs_dir), scrub_dir=str(out_dir))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = cmd_scrub(args)
        return int(rc or 0), stderr.getvalue()

    def test_scrub_command_exits_nonzero_when_a_file_is_withheld(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, files={
                "artifact.screenshot.png": b"PK\x03\x04" + b"not really a png",
            })
            out_dir = Path(td) / "pub"

            rc, stderr = self._run_cmd(runs_dir, out_dir)

            self.assertNotEqual(rc, 0, "a withheld file must not report success")
            self.assertIn("artifact.screenshot.png", stderr)

    def test_scrub_command_names_the_withheld_run_and_file(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, "o/t/w/run1", files={"artifact.db": b"SQLite format 3\x00"})
            out_dir = Path(td) / "pub"

            _, stderr = self._run_cmd(runs_dir, out_dir)

            self.assertIn("o/t/w/run1", stderr)
            self.assertIn("artifact.db", stderr)

    def test_scrub_command_exits_zero_when_nothing_is_withheld(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, files={"screenshot.png": PNG_BYTES})
            out_dir = Path(td) / "pub"

            rc, _ = self._run_cmd(runs_dir, out_dir)

            self.assertEqual(rc, 0, "the clean path must stay green")

    def test_scrub_command_exits_zero_for_an_omitted_but_not_blocked_run(self):
        """`debug.jsonl` is omitted by policy and is not a withheld artifact."""
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            _make_run(runs_dir, files={"screenshot.png": PNG_BYTES, "debug.jsonl": b"{}"})
            out_dir = Path(td) / "pub"

            rc, _ = self._run_cmd(runs_dir, out_dir)

            self.assertEqual(rc, 0)

    def test_scrub_command_exits_zero_when_there_are_no_runs(self):
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td) / "runs"
            runs_dir.mkdir()
            out_dir = Path(td) / "pub"

            rc, _ = self._run_cmd(runs_dir, out_dir)

            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
