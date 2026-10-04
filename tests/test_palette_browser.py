"""U14: the command palette in a real browser, local and hosted.

Skipped without playwright. Zero network: the key-free U23 corpus on loopback,
and the same corpus as a hosted snapshot for the client-side path."""

from __future__ import annotations

import unittest

import browser_corpus as bc
import browser_hosted as bh

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

OPT = "#palette-list [role=option]"


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class _Base(unittest.TestCase):
    base = ""

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()
        cls.srv = bc.shared()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def open(self, route="/", width=1440, height=900):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, has_touch=width < 500)
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errors: list[str] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.add_init_script("try{localStorage.setItem('orchestral.theme','paper')}catch(e){}")
        pg.goto(f"{self.base}/#{route}")
        pg.wait_for_selector("#view[data-ready]", timeout=30000)
        return pg

    def palette(self, pg, q=""):
        pg.keyboard.press("Control+k")
        pg.wait_for_selector("#palette[open]")
        if q:
            pg.fill("#palette-q", q)
        pg.wait_for_selector(f"{OPT}, .palette-none")
        return pg

    def options(self, pg):
        return pg.locator(OPT)


class PaletteCases:
    """The behaviour both modes share."""

    def test_combobox_listbox_wiring(self):
        pg = self.palette(self.open())
        q = pg.locator("#palette-q")
        self.assertEqual(q.get_attribute("role"), "combobox")
        self.assertEqual(q.get_attribute("aria-expanded"), "true")
        self.assertEqual(q.get_attribute("aria-controls"), "palette-list")
        self.assertEqual(pg.locator("#palette-list").get_attribute("role"), "listbox")
        first = self.options(pg).first
        self.assertEqual(q.get_attribute("aria-activedescendant"), first.get_attribute("id"))
        self.assertEqual(first.get_attribute("aria-selected"), "true")
        self.assertEqual(self.errors, [])

    def test_empty_query_offers_views_and_commands(self):
        pg = self.palette(self.open())
        kinds = {o.get_attribute("data-kind") for o in self.options(pg).all()}
        self.assertTrue({"route", "command"} <= kinds, kinds)

    def test_run_id_prefix_ranks_that_run_first(self):
        rid = self.srv.manifest["failed_run_id"]
        pg = self.palette(self.open(), rid[:8])
        first = self.options(pg).first
        self.assertEqual(first.get_attribute("data-kind"), "run")
        self.assertEqual(first.locator("a").get_attribute("href"), f"#/run/{rid}")

    def test_finds_tasks_pairings_groups_models(self):
        pg = self.open()
        for query, kind, frag in (
            ("landing", "task", "task=corpus-landing-page"),
            ("worker-cheap", "model", "worker-cheap"),
            ("orch-a", "pairing", "pairing="),
        ):
            self.palette(pg, query)
            hits = [(o.get_attribute("data-kind"), o.locator("a").get_attribute("href"))
                    for o in self.options(pg).all()]
            self.assertTrue(any(k == kind and frag in h for k, h in hits), (query, hits[:8]))
            pg.keyboard.press("Escape")
        group = next(g["group"] for g in self.srv.api("/api/groups") if g["group"] != "(ungrouped)")
        self.palette(pg, group)
        self.assertTrue(any(o.get_attribute("data-kind") == "group" for o in self.options(pg).all()))

    def test_results_cap_at_fifty(self):
        pg = self.palette(self.open(), "corpus")
        self.assertEqual(self.options(pg).count(), 50)

    def test_arrows_move_the_active_option_and_enter_navigates(self):
        pg = self.palette(self.open(), "guide")
        q = pg.locator("#palette-q")
        ids = [o.get_attribute("id") for o in self.options(pg).all()]
        self.assertGreaterEqual(len(ids), 2)
        pg.keyboard.press("ArrowDown")
        self.assertEqual(q.get_attribute("aria-activedescendant"), ids[1])
        pg.keyboard.press("ArrowUp")
        self.assertEqual(q.get_attribute("aria-activedescendant"), ids[0])
        target = self.options(pg).first.locator("a").get_attribute("href")
        pg.keyboard.press("Enter")
        pg.wait_for_function("!document.getElementById('palette').open")
        self.assertEqual(pg.evaluate("location.hash"), target)

    def test_escape_closes_and_returns_focus(self):
        pg = self.open()
        pg.focus("#open-palette")
        pg.keyboard.press("Control+k")
        pg.wait_for_selector("#palette[open]")
        pg.keyboard.press("Escape")
        pg.wait_for_function("!document.getElementById('palette').open")
        self.assertEqual(pg.evaluate("document.activeElement.id"), "open-palette")

    def test_no_match_links_to_runs_search(self):
        pg = self.palette(self.open(), "zzqqxx")
        self.assertEqual(self.options(pg).count(), 0)
        self.assertEqual(pg.locator(".palette-none a").get_attribute("href"), "#/runs?q=zzqqxx")

    def test_recent_items_come_first_on_next_open_per_device(self):
        rid = self.srv.manifest["failed_run_id"]
        pg = self.palette(self.open(), rid[:8])
        pg.keyboard.press("Enter")
        pg.wait_for_function("!document.getElementById('palette').open")
        self.palette(pg)
        first = self.options(pg).first
        self.assertEqual(first.get_attribute("data-recent"), "true", pg.inner_html("#palette-list")[:300])
        self.assertEqual(first.locator("a").get_attribute("href"), f"#/run/{rid}")
        self.assertIn("recent", pg.locator("#palette-list").inner_text().lower())

    def test_theme_command_changes_the_theme(self):
        pg = self.palette(self.open(), "theme")
        before = pg.evaluate("document.documentElement.dataset.theme")
        cmd = pg.locator(f"{OPT}[data-kind=command]").first
        cmd.locator("a").click()
        pg.wait_for_function("!document.getElementById('palette').open")
        self.assertNotEqual(pg.evaluate("document.documentElement.dataset.theme"), before)

    def test_390_palette_fits_and_is_tappable(self):
        pg = self.open(width=390, height=844)
        pg.tap("#more-open")
        pg.tap("#more-sheet [data-open-palette]")
        pg.wait_for_selector("#palette[open]")
        pg.fill("#palette-q", "landing")
        pg.wait_for_selector(OPT)
        self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390)
        self.assertLessEqual(pg.evaluate("document.getElementById('palette').getBoundingClientRect().right"), 390)


class TestPaletteLocal(PaletteCases, _Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.srv.base

    def test_new_run_command_is_offered_locally(self):
        pg = self.palette(self.open(), "new run")
        self.assertTrue(any(o.locator("a").get_attribute("href") == "#/new" for o in self.options(pg).all()))


class TestPaletteHosted(PaletteCases, _Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = bh.hosted_base()

    def test_new_run_is_not_offered_on_a_read_only_build(self):
        pg = self.palette(self.open(), "new run")
        self.assertFalse(any(o.locator("a").get_attribute("href") == "#/new" for o in self.options(pg).all()))
        self.assertEqual(pg.evaluate("document.documentElement.dataset.mode"), "hosted")


if __name__ == "__main__":
    unittest.main()
