"""Tests for screenshot capture behavior (degradation and staleness logic)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestral.shots import ScreenshotUnavailable, capture_run


class TestCaptureRun(unittest.TestCase):
    def test_no_artifact_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, status = capture_run(tmp)
            self.assertIsNone(path)
            self.assertEqual(status, "no_artifact")

    def test_current_screenshot_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            artifact = run_dir / "artifact.html"
            artifact.write_text("<html><body>hi</body></html>")
            shot = run_dir / "screenshot.png"
            shot.write_bytes(b"png")
            # make the shot newer than the artifact
            now = time.time()
            import os
            os.utime(artifact, (now - 10, now - 10))
            os.utime(shot, (now, now))
            path, status = capture_run(run_dir)
            self.assertEqual(path, shot)
            self.assertEqual(status, "current")

    def test_capture_or_degrade(self):
        """With playwright+browsers installed this captures; otherwise it raises
        ScreenshotUnavailable — either outcome must be clean, never a crash."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "artifact.html").write_text("<html><body><h1>hi</h1></body></html>")
            try:
                path, status = capture_run(run_dir)
            except ScreenshotUnavailable:
                return  # degraded path is a pass
            self.assertEqual(status, "captured")
            self.assertTrue(path and path.exists() and path.stat().st_size > 0)

    def test_capture_with_fake_browser(self):
        """Injected browser objects exercise the capture path without playwright."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "artifact.html").write_text("<html><body><h1>hi</h1></body></html>")

            page = MagicMock()
            def _shot(*, path, full_page):
                Path(path).write_bytes(b"png")
            page.screenshot.side_effect = _shot
            browser = MagicMock()
            browser.new_page.return_value = page

            path, status = capture_run(run_dir, browser=browser)
            self.assertEqual(status, "captured")
            self.assertTrue(path.exists())
            page.route.assert_called_once()  # network isolation applied
            page.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
