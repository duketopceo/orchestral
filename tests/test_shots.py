"""Tests for screenshot capture behavior (degradation and staleness logic)."""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestral.shots import ScreenshotUnavailable, capture_page, capture_run


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


def _fake_browser() -> tuple[MagicMock, MagicMock]:
    page = MagicMock()
    page.locator.return_value.first.screenshot.return_value = b"png-bytes"
    browser = MagicMock()
    browser.new_page.return_value = page
    return browser, page


class TestCapturePage(unittest.TestCase):
    """SPA page capture: injected browser exercises the path without
    playwright, matching capture_html's injection contract."""

    def test_waits_for_readiness_and_returns_bytes(self):
        browser, page = _fake_browser()
        png = capture_page("http://x/#/leaderboard", browser=browser)
        self.assertEqual(png, b"png-bytes")
        page.goto.assert_called_once_with("http://x/#/leaderboard")
        page.wait_for_selector.assert_called_once_with(
            "#view[data-ready]", state="visible", timeout=20000)
        page.locator.assert_called_once_with("#view")  # captures settled view
        page.close.assert_called_once()

    def test_element_narrows_to_node(self):
        browser, page = _fake_browser()
        capture_page("http://x/#/card?kind=pairing", element=".xcard",
                     browser=browser)
        page.locator.assert_called_with(".xcard")

    def test_capture_failure_wraps_and_closes_page(self):
        browser, page = _fake_browser()
        page.goto.side_effect = RuntimeError("boom")
        with self.assertRaises(ScreenshotUnavailable):
            capture_page("http://x/", browser=browser)
        page.close.assert_called_once()


class TestCardsCLI(unittest.TestCase):
    """harness cards: batch export drives the SPA through a shared fake
    browser — no playwright needed, individual failures don't abort."""

    def _run_cards(self, tmp: str, capture=b"png") -> None:
        import harness
        from orchestral.config import ModelConfig, TaskSpec
        from orchestral.runner import Runner
        from orchestral.storage import RunStore

        def _m(slug, role):
            return ModelConfig(slug=slug, name=slug, role=role,
                               input_price_per_mtok=0.03,
                               output_price_per_mtok=0.10)

        Runner(dry_run=True, runs_dir=tmp, store=RunStore(tmp)).run(
            TaskSpec(id="t1", type="html", prompt="p"),
            _m("o/m", "orchestrator"), _m("w/m", "worker"))
        for d in ("tasks", "models"):
            (Path(tmp) / d).mkdir(exist_ok=True)
        args = harness.argparse.Namespace(
            runs_dir=tmp, tasks_dir=str(Path(tmp) / "tasks"),
            models_dir=str(Path(tmp) / "models"),
            reports_dir=str(Path(tmp) / "reports"), group=None)

        @contextmanager
        def _session():
            yield MagicMock()

        kw = ({"return_value": capture} if isinstance(capture, bytes)
              else {"side_effect": capture})
        with patch("orchestral.shots.browser_session", _session), \
             patch("orchestral.shots.capture_page", **kw):
            harness.cmd_cards(args)

    def test_writes_overview_leaderboard_and_pairing_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_cards(tmp)
            names = {p.name for p in (Path(tmp) / "reports" / "cards").glob("*.png")}
            self.assertIn("overview.png", names)
            self.assertIn("leaderboard.png", names)
            self.assertTrue(any(n.startswith("pairing-") for n in names))

    def test_individual_failure_continues_batch(self):
        calls = []

        def _flaky(url, **kw):
            calls.append(url)
            if "#/leaderboard" in url:
                raise ScreenshotUnavailable("timeout")
            return b"png"

        with tempfile.TemporaryDirectory() as tmp:
            self._run_cards(tmp, capture=_flaky)
            names = {p.name for p in
                     (Path(tmp) / "reports" / "cards").glob("*.png")}
            self.assertIn("overview.png", names)
            self.assertNotIn("leaderboard.png", names)  # failed, not fatal
            self.assertTrue(any(n.startswith("pairing-") for n in names))
            self.assertGreater(len(calls), 2)  # kept going past the failure


if __name__ == "__main__":
    unittest.main()
