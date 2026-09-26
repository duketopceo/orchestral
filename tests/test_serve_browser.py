"""Browser smoke for the web observatory — skipped without playwright.

Mirrors test_shots' optional-extra pattern: the test only runs when the
`[shots]`-grade playwright install is present; otherwise it skips clean.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.web.server import Observatory, make_handler

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestBrowserSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        Runner(dry_run=True, runs_dir=cls.tmp, store=RunStore(cls.tmp), run_group="browser-group").run(
            TaskSpec(id="t-task", type="html", prompt="p"),
            _model("o/model", "orchestrator"), _model("w/model", "worker"),
        )
        obs = Observatory(Path(cls.tmp), Path(cls.tmp) / "tasks", Path(cls.tmp) / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_overview_renders_in_browser(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page()
                pg.goto(f"http://127.0.0.1:{self.port}/")
                pg.wait_for_selector("h1")
                self.assertIn("Overview", pg.inner_text("h1"))
                self.assertIn("orchestral", pg.inner_text("body"))
                self.assertIn("t-task", pg.inner_text("body"))
                pg.goto(f"http://127.0.0.1:{self.port}/#/new")
                pg.wait_for_selector("button.primary")
                self.assertIn("launch", pg.inner_text("body"))
            finally:
                browser.close()

    def test_cards_studio_filters_and_opens_universal_card(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page(viewport={"width": 390, "height": 844})
                pg.goto(f"http://127.0.0.1:{self.port}/#/cards")
                pg.wait_for_selector("#view[data-ready='1']")
                self.assertIn("Cards", pg.inner_text("h1"))
                self.assertIn("shareable card", pg.inner_text("body").lower())
                self.assertGreaterEqual(pg.locator(".gallery-card").count(), 1)
                self.assertEqual(pg.locator("body").evaluate("e => e.scrollWidth"), 390)

                pg.select_option("#cards-lens", "divergence")
                pg.wait_for_selector("#view[data-ready='1']")
                self.assertIn("lens=divergence", pg.url)
                pg.locator(".gallery-card-title").first.click()
                pg.wait_for_selector(".xcard")
                self.assertIn("run group", pg.inner_text("body").lower())
                self.assertGreaterEqual(pg.locator(".xc-proof-panel").count(), 2)
            finally:
                browser.close()

    def test_card_is_usable_at_mobile_width(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page(viewport={"width": 390, "height": 844})
                pg.goto(
                    f"http://127.0.0.1:{self.port}/#/card?kind=group&target=browser-group",
                )
                pg.wait_for_selector(".xcard")
                pg.wait_for_selector("#view[data-ready='1']")
                self.assertEqual(pg.locator("body").evaluate("e => e.scrollWidth"), 390)
                card = pg.locator(".xcard")
                self.assertLessEqual(card.evaluate("e => e.scrollWidth"), 390)
                self.assertGreater(card.evaluate("e => e.getBoundingClientRect().height"), 675)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
