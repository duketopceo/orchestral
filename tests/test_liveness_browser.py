"""U9: the rail Activity list shows stalled and unowned rows, from the corpus.

Skipped without playwright. Zero network: the server binds loopback and the
corpus is key-free."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestral.storage import RunMeta, RunStore
from orchestral.web.server import Observatory, make_handler

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus)

SHOT_DIR = os.environ.get("ORCH_U9_SHOTS")


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed")
class TestRailActivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.root, "full")
        cls.orphan = cls.manifest["orphan_run_id"]
        # one CLI-launched run that is alive right now (event 20 seconds ago)
        store = RunStore(cls.root)
        now = datetime.now(UTC)
        run_dir = cls.root / "cli-live0001"
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text(json.dumps({
            "type": "run.started", "timestamp": (now - timedelta(seconds=20)).isoformat()}) + "\n")
        store.index_meta(RunMeta(
            run_id="cli-live0001", orchestrator="corpus/orch-a", task_id="corpus-landing-page",
            worker="corpus/worker-cheap", status="running",
            started_at=(now - timedelta(minutes=5)).isoformat(), run_dir=str(run_dir)))
        obs = Observatory(cls.root, FIXTURES / "tasks", FIXTURES / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _page(self, browser, theme: str = "paper"):
        pg = browser.new_page(viewport={"width": 1280, "height": 800})
        pg.add_init_script(f"try{{localStorage.setItem('orchestral.theme','{theme}')}}catch(e){{}}")
        pg.goto(f"http://127.0.0.1:{self.port}/")
        pg.wait_for_selector("#view[data-ready='ok']")
        pg.wait_for_selector("#rail-jobs .rail-job")
        return pg

    def test_rail_lists_stalled_and_unowned_rows(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = self._page(browser)
                # the orphan's heartbeat is days old: past LOST_AFTER_S it is
                # a lost ghost, still abandonable, wearing the stalled glyph
                lost = pg.locator(f"#rail-jobs .rail-job[data-run='{self.orphan}']")
                live = pg.locator("#rail-jobs .rail-job[data-run='cli-live0001']")
                self.assertEqual(lost.count(), 1)
                self.assertEqual(lost.get_attribute("data-state"), "lost")
                self.assertEqual(live.get_attribute("data-state"), "live")
                # unowned is said in words; the glyph is the sprite's, and differs by shape
                self.assertIn("lost", lost.inner_text().lower())
                self.assertIn("Started from the CLI. Stop it there.", lost.inner_text())
                self.assertIn("Started from the CLI. Stop it there.", live.inner_text())
                self.assertEqual(lost.locator("svg use").get_attribute("href"), "#i-stalled")
                self.assertEqual(live.locator("svg use").get_attribute("href"), "#i-live")
                # abandon only on stalled/lost unowned; cancel never on unowned
                self.assertEqual(lost.locator("button.abandon").count(), 1)
                self.assertEqual(live.locator("button.abandon").count(), 0)
                self.assertEqual(pg.locator("#rail-jobs button.cancel").count(), 0)
            finally:
                browser.close()

    def _clear_annotations(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.root / "index.db")) as conn, conn:
            conn.execute("DELETE FROM annotations")

    def test_mark_abandoned_retires_the_row(self):
        self.addCleanup(self._clear_annotations)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = self._page(browser)
                row = pg.locator(f"#rail-jobs .rail-job[data-run='{self.orphan}']")
                row.locator("button.abandon").click()
                pg.wait_for_selector(f"#rail-jobs .rail-job[data-run='{self.orphan}']", state="detached")
                self.assertEqual(pg.locator("#rail-jobs .rail-job[data-run='cli-live0001']").count(), 1)
            finally:
                browser.close()
            # the index status is untouched
            self.assertEqual(RunStore(self.root).get_run(self.orphan).status, "running")

    def test_read_only_capabilities_hide_abandon_and_cancel(self):
        meta = {"mode": "local", "synced_at": None, "source_commit": None,
                "capabilities": {"launch": False, "cancel": False, "flag_write": False,
                                 "thread": False, "png_capture": False, "live_stream": True}}
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page(viewport={"width": 1280, "height": 800})
                pg.route("**/api/meta", lambda r: r.fulfill(
                    status=200, content_type="application/json", body=json.dumps(meta)))
                pg.goto(f"http://127.0.0.1:{self.port}/")
                pg.wait_for_selector("#rail-jobs .rail-job")
                self.assertEqual(pg.locator("#rail-jobs button").count(), 0)
                self.assertGreaterEqual(pg.locator("#rail-jobs .rail-job").count(), 2)
            finally:
                browser.close()

    @unittest.skipUnless(SHOT_DIR, "set ORCH_U9_SHOTS to capture screenshots")
    def test_capture_screenshots(self):
        assert SHOT_DIR
        out = Path(SHOT_DIR)
        out.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in ("paper", "stage"):
                    pg = self._page(browser, theme)
                    pg.evaluate(
                        "t => { document.documentElement.setAttribute('data-theme', t); }", theme)
                    pg.locator("#rail").screenshot(path=str(out / f"rail-{theme}.png"))
                    pg.close()
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
