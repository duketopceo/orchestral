"""The Score, U15: the Publish views and the card capture in a real browser.

Skipped without playwright (BROWSER=1 scripts/bootstrap-venv.sh). Zero paid
calls: the corpus is built locally and nothing reaches a provider."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import struct
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from orchestral.storage import RunStore
from orchestral.web import snapshot
from orchestral.web.server import Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


corpus = _load("build_fixture_corpus", "scripts/build-fixture-corpus.py")
sb = _load("serve_browser_helpers", "tests/test_serve_browser.py")
HAS_PLAYWRIGHT = sb.HAS_PLAYWRIGHT
# Regression ceiling for a 2400x1350 optimized-or-not capture, recorded from the
# first fixture capture (KTD9). A card that grows past it needs a reason.
CARD_PNG_CEILING = 100 * 1024  # first fixture captures: 56-74KB


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


GROUP = "corpus-main:r0"
THIN = "corpus-thin"


def card_hash(kind: str, target: str, extra: str = "") -> str:
    return f"#/card?kind={kind}&target={urllib.parse.quote(target, safe='')}{extra}"


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestPublishLocal(sb._Browser):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        cls.store = RunStore(cls.tmp / "runs")
        runs = cls.store.list_runs(limit=None)
        cls.held = [r.run_id for r in runs if (r.config or {}).get("holdout")]
        obs = Observatory(cls.tmp / "runs", FIXTURES / "tasks", FIXTURES / "models")
        cls.httpd, port = sb._serve(make_handler(obs))
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def _open(self, route: str, width: int = 1440, height: int = 900):
        pg = self.page(viewport={"width": width, "height": height})
        pg.goto(f"{self.base}/{route}")
        pg.wait_for_selector("#view[data-ready='ok']", timeout=20000)
        return pg

    # --- gallery -----------------------------------------------------------

    def test_gallery_is_a_list_with_one_large_preview(self):
        pg = self._open("#/cards?scope=all")
        self.assertIn("Publish", pg.inner_text("h1"))
        self.assertEqual(pg.locator(".gallery-card, .gallery-grid").count(), 0)
        rows = pg.locator("#pub-list .pub-row")
        self.assertGreaterEqual(rows.count(), 2)
        self.assertEqual(pg.locator("#pub-list .pub-row[aria-current='true']").count(), 1)
        self.assertEqual(pg.locator("#pub-preview .xcard").count(), 1)
        first = pg.locator("#pub-preview .xcard").get_attribute("data-card-scope")
        rows.nth(1).click()
        pg.wait_for_selector("#view[data-ready='ok']")
        self.assertEqual(pg.locator("#pub-list .pub-row[aria-current='true']").count(), 1)
        self.assertEqual(pg.locator("#pub-preview .xcard").count(), 1)
        self.assertTrue(first)

    def test_gallery_never_lists_the_holdout_group(self):
        pg = self._open("#/cards")
        self.assertNotIn("corpus-holdout", pg.inner_text("#view"))
        self.assertNotIn("holdout", pg.locator("#pub-list").inner_text().lower())

    def test_holdout_card_url_shows_the_withheld_state_not_a_card(self):
        pg = self.page()
        pg.goto(f"{self.base}/{card_hash('group', 'corpus-holdout')}")
        pg.wait_for_selector("#view[data-ready]", timeout=20000)
        self.assertEqual(pg.locator(".xcard").count(), 0)
        for run_id in self.held:
            pg.goto(f"{self.base}/{card_hash('run', run_id)}")
            pg.wait_for_selector("#view[data-ready]", timeout=20000)
            self.assertEqual(pg.locator(".xcard").count(), 0, run_id)

    # --- card layout -------------------------------------------------------

    def _assert_card_fits(self, pg, width: int):
        pg.wait_for_selector(".xcard")
        self.assertEqual(pg.evaluate("document.documentElement.scrollWidth"), width)
        box = pg.locator(".xcard").bounding_box()
        assert box
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], width + 0.5)
        # nothing inside the card spills past the card's own edges
        spill = pg.evaluate("""() => {
          const card = document.querySelector('.xcard').getBoundingClientRect();
          const bad = [];
          for (const el of document.querySelectorAll('.xcard *')) {
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            if (r.right > card.right + 1 || r.bottom > card.bottom + 1 || r.left < card.left - 1)
              bad.push(el.className + ':' + Math.round(r.right - card.right));
          }
          return bad;
        }""")
        self.assertEqual(spill, [])

    def test_card_never_clips_at_1440_1360_and_390(self):
        for width in (1440, 1360, 390):
            for target in (GROUP, THIN):
                pg = self._open(card_hash("group", target), width=width)
                self._assert_card_fits(pg, width)

    def test_pairing_and_run_cards_fit_at_390(self):
        run_id = next(r.run_id for r in self.store.list_runs(limit=None)
                      if r.status == "finished" and r.run_id not in self.held)
        for route in (card_hash("pairing", "corpus/orch-a|corpus/worker-cheap"),
                      card_hash("run", run_id)):
            pg = self._open(route, width=390, height=844)
            self._assert_card_fits(pg, 390)

    def test_editor_preview_is_feed_sized(self):
        pg = self._open(card_hash("group", GROUP), width=1440)
        feed = pg.locator("#feed-preview").bounding_box()
        assert feed
        self.assertEqual(round(feed["width"]), 600)
        self.assertEqual(pg.locator("#feed-preview .xcard").count(), 1)

    def test_card_reads_as_a_program_note(self):
        pg = self._open(card_hash("pairing", "corpus/orch-a|corpus/worker-cheap"))
        for sel in (".pc-top .pc-mark", ".pc-top .pc-word", ".pc-scope", ".pc-title", ".pc-claim",
                    ".pc-measures .pc-measure", ".pc-foot .pc-caveat", ".pc-foot .pc-prov"):
            self.assertGreaterEqual(pg.locator(f".xcard {sel}").count(), 1, sel)
        self.assertEqual(pg.locator(".xcard .pc-measure").count(), 3)
        self.assertEqual(pg.locator(".xcard .pc-measure.mech .pc-interval").count(), 1)
        self.assertEqual(pg.locator(".xcard .pc-baton").count(), 1)  # the glyph between the pair
        self.assertEqual(pg.locator(".xcard .pc-mark").get_attribute("viewBox"), "0 0 24 24")

    def test_card_with_no_proof_has_no_proof_row_and_no_unavailable_text(self):
        pg = self._open(card_hash("group", "corpus-orphan"))
        self.assertEqual(pg.locator(".xcard .pc-proof").count(), 0)
        self.assertNotIn("unavailable", pg.locator(".xcard").inner_text().lower())
        self.assertNotIn("no stored proof", pg.locator(".xcard").inner_text().lower())

    def test_card_with_proof_shows_the_proof_row(self):
        pg = self._open(card_hash("group", GROUP))
        self.assertEqual(pg.locator(".xcard .pc-proof").count(), 1)

    # --- alt text and caption ---------------------------------------------

    def test_alt_text_is_offered_beside_download_and_is_editable_with_a_counter(self):
        pg = self._open(card_hash("group", GROUP))
        api = json.loads(urllib.request.urlopen(
            f"{self.base}/api/card?kind=group&target={urllib.parse.quote(GROUP)}").read())
        alt = pg.locator("#alt-text")
        self.assertEqual(alt.input_value(), api["story"]["alt"])
        self.assertEqual(pg.locator("#alt-count").inner_text(), f"{len(api['story']['alt'])} / 1000")
        alt.fill("x" * 40)
        self.assertEqual(pg.locator("#alt-count").inner_text(), "40 / 1000")
        self.assertEqual(alt.get_attribute("maxlength"), "1000")
        self.assertEqual(pg.locator("#caption-text").get_attribute("maxlength"), "1000")
        self.assertTrue(pg.locator("#caption-count").inner_text().endswith("/ 1000"))

    def test_empty_alt_text_blocks_download_and_says_why(self):
        pg = self._open(card_hash("group", GROUP))
        pg.locator("#alt-text").fill("")
        self.assertTrue(pg.locator("#download-png").is_disabled())
        self.assertIn("Alt text is required", pg.locator("#alt-error").inner_text())
        pg.locator("#alt-text").fill("A card.")
        self.assertFalse(pg.locator("#download-png").is_disabled())
        self.assertEqual(pg.locator("#alt-error").inner_text().strip(), "")

    # --- download ----------------------------------------------------------

    def _download_with(self, status: int, body: dict | bytes, ctype: str = "application/json"):
        pg = self._open(card_hash("group", GROUP))
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        pg.route("**/api/shot.png*", lambda route: route.fulfill(
            status=status, body=payload, content_type=ctype))
        return pg

    def test_download_fetches_a_blob_and_saves_it(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        pg = self._download_with(200, png, "image/png")
        with pg.expect_download() as dl:
            pg.locator("#download-png").click()
        self.assertTrue(dl.value.suggested_filename.endswith(".png"))
        self.assertIn("Saved", pg.locator("#dl-status").inner_text())

    def test_playwright_missing_shows_the_install_command_inline(self):
        pg = self._download_with(503, {
            "error": "playwright is not installed", "code": "playwright_missing",
            "install": "pip install 'orchestral[shots]' && playwright install chromium"})
        pg.locator("#download-png").click()
        pg.wait_for_function("document.getElementById('dl-status').textContent.includes('pip install')")
        text = pg.locator("#dl-status").inner_text()
        self.assertIn("Playwright is not installed", text)
        self.assertIn("pip install 'orchestral[shots]' && playwright install chromium", text)

    def test_chromium_missing_shows_the_browser_install_inline(self):
        pg = self._download_with(503, {
            "error": "no chromium", "code": "chromium_missing",
            "install": "playwright install chromium"})
        pg.locator("#download-png").click()
        pg.wait_for_function("document.getElementById('dl-status').textContent.includes('playwright install')")
        self.assertIn("Chromium is not installed", pg.locator("#dl-status").inner_text())

    def test_timeout_is_stated_and_retryable(self):
        pg = self._download_with(504, {"error": "slow", "code": "timeout"})
        pg.locator("#download-png").click()
        pg.wait_for_function("document.getElementById('dl-status').textContent.includes('too long')")
        self.assertFalse(pg.locator("#download-png").is_disabled())

    # --- capture mode and the real PNG ---------------------------------------

    def test_capture_mode_is_a_bare_1200_by_675_card(self):
        pg = self._open(card_hash("group", GROUP, "&capture=1"), width=1200, height=675)
        self.assertEqual(pg.evaluate("document.documentElement.dataset.capture"), "1")
        box = pg.locator(".xcard").bounding_box()
        assert box
        self.assertEqual((round(box["width"]), round(box["height"])), (1200, 675))
        self.assertEqual((box["x"], box["y"]), (0, 0))
        self.assertEqual(pg.evaluate("document.documentElement.dataset.theme"), "paper")
        self._assert_card_fits(pg, 1200)

    def test_real_capture_is_2400_by_1350_deterministic_and_under_the_ceiling(self):
        route = "/card?kind=group&target=" + urllib.parse.quote(GROUP, safe="")
        url = f"{self.base}/api/shot.png?route={urllib.parse.quote(route, safe='')}"
        first = urllib.request.urlopen(url, timeout=120).read()
        second = urllib.request.urlopen(url, timeout=120).read()
        self.assertEqual(png_size(first), (2400, 1350))
        self.assertLessEqual(len(first), CARD_PNG_CEILING)
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())

    def test_real_capture_of_a_holdout_card_is_a_404_never_an_image(self):
        for route in ("/card?kind=group&target=corpus-holdout",
                      *(f"/card?kind=run&target={r}" for r in self.held)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(
                    f"{self.base}/api/shot.png?route={urllib.parse.quote(route, safe='')}")
            self.assertEqual(cm.exception.code, 404, route)


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestPublishHosted(sb._Browser):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        cls.store = RunStore(cls.tmp / "runs")
        cls.snap = snapshot.build_snapshot(
            cls.store, FIXTURES / "tasks", FIXTURES / "models", run_ids=[],
            synced_at="2026-10-02T11:00:00+00:00", source_commit="abc1234")
        sim = type("Sim", (sb._WorkerSim,), {"snap": cls.snap})
        cls.hosted, port = sb._serve(sim)
        cls.hbase = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.hosted.shutdown()
        cls.hosted.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def test_hosted_card_has_alt_text_and_no_png_download(self):
        pg = self.page(viewport={"width": 1360, "height": 900})
        pg.goto(f"{self.hbase}/{card_hash('group', THIN)}")
        pg.wait_for_selector("#view[data-ready='ok']", timeout=20000)
        self.assertEqual(pg.locator("#download-png, a[href*='shot.png']").count(), 0)
        self.assertTrue(pg.locator("#alt-text").input_value().strip())
        self.assertIn("not ranked, thin sample", pg.locator("#alt-text").input_value())
        self.assertEqual(pg.evaluate("document.documentElement.scrollWidth"), 1360)
        box = pg.locator(".xcard").bounding_box()
        assert box
        self.assertLessEqual(box["x"] + box["width"], 1360)

    def test_hosted_gallery_has_no_png_links(self):
        pg = self.page()
        pg.goto(f"{self.hbase}/#/cards")
        pg.wait_for_selector("#view[data-ready='ok']", timeout=20000)
        self.assertEqual(pg.locator("a[href*='shot.png']").count(), 0)
        self.assertGreaterEqual(pg.locator("#pub-list .pub-row").count(), 1)


if __name__ == "__main__":
    unittest.main()
