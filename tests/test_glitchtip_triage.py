"""Tests for the GlitchTip-to-GitHub issue bridge (scripts/glitchtip_to_issues.py).

The repository is public and the observatory is private, so the pins
here are the scrub rules: nothing carrying URLs, run ids, or host
identifiers may reach a filed issue title or body.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "glitchtip_to_issues", ROOT / "scripts" / "glitchtip_to_issues.py"
)
MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MOD)


class ScrubTest(unittest.TestCase):
    def test_urls_redacted(self):
        self.assertEqual(
            MOD.scrub("fetch failed https://obs.shippedit.dev/api/runs/abc?x=1"),
            "fetch failed [url]",
        )

    def test_uuid_and_hex_ids_redacted(self):
        self.assertEqual(
            MOD.scrub("run 95e8b323-5013-4fb3-95bd-203a80bcfb0f failed"),
            "run [id] failed",
        )
        self.assertEqual(
            MOD.scrub("token 95e8b32350134fb395bd203a80bcfb0f leaked"),
            "token [id] leaked",
        )

    def test_run_dir_names_redacted(self):
        self.assertEqual(MOD.scrub("missing run-sb-alpha-probe-42"), "missing [id]")
        self.assertEqual(MOD.scrub("cli-live0001 timed out"), "[id] timed out")

    def test_short_identifiers_survive(self):
        self.assertEqual(MOD.scrub("HTTP 500 on /api/overview"), "HTTP 500 on /api/overview")

    def test_empty_and_none(self):
        self.assertEqual(MOD.scrub(""), "")
        self.assertEqual(MOD.scrub(None), "")


class IssueShapeTest(unittest.TestCase):
    ISSUE: ClassVar[dict] = {
        "id": "42",
        "count": 7,
        "userCount": 2,
        "level": "error",
        "firstSeen": "2026-10-05T00:00:00Z",
        "lastSeen": "2026-10-05T01:00:00Z",
        "title": "TypeError: cannot read https://obs.shippedit.dev/x?run=abc123def456",
        "metadata": {
            "type": "TypeError",
            "value": "cannot read run-sb-alpha-probe-9 at https://obs.shippedit.dev",
        },
    }
    EVENT: ClassVar[dict] = {
        "entries": [
            {
                "type": "exception",
                "data": {
                    "values": [
                        {
                            "stacktrace": {
                                "frames": [
                                    {"filename": "main.js", "lineNo": 3},
                                    {"filename": "run.js", "lineNo": 87},
                                ]
                            }
                        }
                    ]
                },
            }
        ]
    }

    def test_title_scrubs_and_prefixes(self):
        title = MOD.issue_title(self.ISSUE)
        self.assertTrue(title.startswith("[obs] TypeError: "))
        self.assertNotIn("shippedit", title)
        self.assertNotIn("sb-alpha", title)

    def test_title_truncates(self):
        long = dict(self.ISSUE, metadata={"type": "E", "value": "v" * 500})
        self.assertLessEqual(len(MOD.issue_title(long)), 120)

    def test_body_scrubs_message_but_keeps_signal(self):
        body = MOD.issue_body(self.ISSUE, self.EVENT)
        self.assertIn("| events | 7 |", body)
        self.assertIn("`TypeError`", body)
        self.assertIn("run.js:87", body)
        self.assertIn(MOD.marker("42"), body)
        self.assertNotIn("shippedit", body)
        self.assertNotIn("sb-alpha-probe", body)

    def test_marker_is_stable(self):
        self.assertEqual(MOD.marker("42"), "<!-- glitchtip:42 -->")


class DedupTest(unittest.TestCase):
    def test_already_filed_true_when_search_hits(self):
        with patch.object(MOD, "_get", return_value={"total_count": 1}):
            self.assertTrue(MOD.already_filed("42", "tok"))

    def test_already_filed_false_on_clean_search(self):
        with patch.object(MOD, "_get", return_value={"total_count": 0}):
            self.assertFalse(MOD.already_filed("42", "tok"))

    def test_search_failure_fails_closed(self):
        with patch.object(MOD, "_get", side_effect=OSError("down")):
            self.assertTrue(MOD.already_filed("42", "tok"))


class MainFlowTest(unittest.TestCase):
    def test_missing_env_fails(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(MOD.main(), 2)

    def test_files_up_to_cap(self):
        issues = [{"id": str(i), "metadata": {"type": "E", "value": "x"}} for i in range(15)]
        env = {"GLITCHTIP_TOKEN": "gt", "GH_TOKEN": "gh", "GH_REPO": "o/r"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(MOD, "MAX_ISSUES", 3),
            patch.object(MOD, "REPO", "o/r"),
            patch.object(MOD, "glitchtip_issues", return_value=issues),
            patch.object(MOD, "already_filed", return_value=False),
            patch.object(MOD, "latest_event", return_value={}),
            patch.object(MOD, "file_issue", return_value="http://x") as filed,
        ):
            self.assertEqual(MOD.main(), 0)
            self.assertEqual(filed.call_count, 3)


if __name__ == "__main__":
    unittest.main()
