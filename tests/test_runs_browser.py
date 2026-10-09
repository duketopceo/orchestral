"""U11: the Runs view in a real browser over the key-free U23 corpus.

Skipped without playwright. Zero network: a loopback server over the corpus.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import browser_corpus as bc

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

PAGE = 200
TABLE_NODE_BUDGET = 1500


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class _Base(unittest.TestCase):
    shape = "full"

    @classmethod
    def setUpClass(cls):
        cls.srv = bc.shared(cls.shape)
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def page(self, width=1440, height=900):
        ctx = self.browser.new_context(
            viewport={"width": width, "height": height}, has_touch=width <= 640)
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errors: list[str] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        return pg

    def open(self, pg, query="", base=None):
        pg.goto(f"{base or self.srv.base}/#/runs{query}")
        pg.wait_for_selector("#view[data-ready]", timeout=30000)
        self.assertEqual(pg.locator("#view").get_attribute("data-ready"), "ok")
        return pg

    def page_rows(self, pg):
        return int(pg.locator("#runs-count").get_attribute("data-page-rows"))

    def total(self, pg):
        return int(pg.locator("#runs-count").get_attribute("data-total"))


class TestRunsList(_Base):
    def test_a_thousand_rows_page_at_200_with_a_correct_total_and_a_small_dom(self):
        pg = self.open(self.page())
        n = len(self.srv.api("/api/runs"))
        self.assertGreaterEqual(n, 1000)
        self.assertEqual(self.total(pg), n)
        self.assertEqual(self.page_rows(pg), PAGE)
        nodes = pg.evaluate("document.querySelectorAll('#view table *').length")
        self.assertLessEqual(nodes, TABLE_NODE_BUDGET)
        self.assertLess(pg.locator("#runs-body tr.run-row").count(), PAGE)
        self.assertIn(f"of {n:,}", pg.locator("#runs-count").inner_text())
        pg.locator("#runs-next").click()
        pg.wait_for_function("location.hash.includes('page=2')")
        self.assertEqual(self.page_rows(pg), PAGE)
        self.assertIn("201", pg.locator("#runs-count").inner_text())
        # the last page holds the remainder
        last = (n - 1) // PAGE + 1
        pg.goto(f"{self.srv.base}/#/runs?page={last}")
        pg.reload()
        pg.wait_for_selector("#view[data-ready]")
        self.assertEqual(self.page_rows(pg), n - PAGE * (last - 1))
        self.assertTrue(pg.locator("#runs-next").is_disabled())
        self.assertEqual(self.errors, [])

    def test_scrolling_swaps_the_window_and_keeps_the_dom_small(self):
        for width, height in ((1440, 900), (390, 844)):
            pg = self.open(self.page(width, height))
            first = pg.locator("#runs-body tr.run-row").first.get_attribute("data-run")
            pg.evaluate("""() => {
              for (const el of [document.querySelector('.tbl-wrap'), document.scrollingElement])
                if (el) el.scrollTop = 4000;
            }""")
            pg.wait_for_function(
                "(id) => document.querySelector('#runs-body tr.run-row')?.dataset.run !== id", arg=first)
            nodes = pg.evaluate("document.querySelectorAll('#view table *').length")
            self.assertLessEqual(nodes, TABLE_NODE_BUDGET, (width, nodes))

    def test_stalled_status_deep_link_lists_the_orphan(self):
        pg = self.open(self.page(), "?status=stalled")
        self.assertEqual(self.total(pg), 1)
        row = pg.locator("#runs-body tr.run-row")
        self.assertEqual(row.count(), 1)
        # days without a heartbeat: the quiet row is a lost ghost
        self.assertIn("Lost", row.inner_text())
        self.assertIn(self.srv.manifest["orphan_run_id"], row.locator("a").first.get_attribute("href"))

    def test_zero_matches_names_the_facets_to_remove(self):
        pg = self.open(self.page(), "?group=corpus-solo&q=zzzz-nothing")
        state = pg.locator("[data-state-kind='nomatch']")
        state.wait_for()
        text = state.inner_text()
        self.assertIn("Remove", text)
        self.assertIn("search", text.lower())
        self.assertIn("group", text.lower())
        self.assertGreaterEqual(state.locator("a, button").count(), 1)

    def test_zero_runs_uses_the_no_runs_copy(self):
        srv = bc.shared("empty")
        pg = self.open(self.page(), base=srv.base)
        state = pg.locator("[data-state-kind='empty']")
        state.wait_for()
        self.assertIn("No runs yet", state.inner_text())
        self.assertEqual(pg.locator("#runs-body tr.run-row").count(), 0)

    def test_unknown_facet_value_is_an_error_chip_not_a_silent_empty(self):
        pg = self.open(self.page(), "?group=no-such-group&task=corpus-landing-page")
        bad = pg.locator(".facet-chip.bad[data-facet='group']")
        bad.wait_for()
        self.assertIn("Not found in current data", bad.inner_text())
        self.assertEqual(pg.locator(".facet-chip.bad").count(), 1)
        pg.locator(".facet-chip.bad button").click()
        pg.wait_for_function("!location.hash.includes('no-such-group')")
        pg.wait_for_function("Number(document.getElementById('runs-count').dataset.total) > 0")
        self.assertEqual(pg.locator(".facet-chip.bad").count(), 0)

    def test_legacy_arrow_pairing_url_is_accepted_and_normalised(self):
        arrow = "corpus%2Forch-b%20%E2%86%92%20corpus%2Fworker-hot"
        pg = self.open(self.page(), f"?pairing={arrow}")
        want = len(self.srv.api("/api/runs?pairing=corpus%2Forch-b%7Ccorpus%2Fworker-hot"))
        self.assertGreater(want, 0)
        pg.wait_for_function(f"document.getElementById('runs-count').dataset.total === '{want}'")
        self.assertEqual(pg.locator("#f-pairing").input_value(), "corpus/orch-b|corpus/worker-hot")
        self.assertEqual(pg.locator(".facet-chip.bad").count(), 0)

    def test_facets_are_url_synced_and_removable(self):
        pg = self.open(self.page())
        pg.select_option("#f-pairing", "corpus/orch-b|corpus/worker-hot")
        pg.wait_for_function("location.hash.includes('pairing=')")
        want = len(self.srv.api("/api/runs?pairing=corpus%2Forch-b%7Ccorpus%2Fworker-hot"))
        pg.wait_for_function(f"document.getElementById('runs-count').dataset.total === '{want}'")
        pg.locator(".facet-chip[data-facet='pairing'] button").click()
        pg.wait_for_function("!location.hash.includes('pairing=')")
        self.assertGreater(self.total(pg), want)
        # reload keeps the filter
        pg.goto(f"{self.srv.base}/#/runs?judge=judged")
        pg.reload()
        pg.wait_for_selector("#view[data-ready]")
        self.assertEqual(pg.locator("#f-judge").input_value(), "judged")

    def test_sorting_by_a_header_changes_order_and_the_url(self):
        pg = self.open(self.page())
        pg.locator("th button[data-sort='cost']").click()
        pg.wait_for_function("location.hash.includes('sort=cost')")
        pg.wait_for_function("document.querySelector('th[aria-sort]')?.textContent.includes('Cost')")
        costs = self.srv.api("/api/runs?sort=cost")[0]["run_id"]
        self.assertIn(costs, pg.locator("#runs-body tr.run-row a").first.get_attribute("href"))

    def test_typing_quickly_sends_one_request_per_quiet_period(self):
        pg = self.open(self.page())
        reqs: list[str] = []
        pg.on("request", lambda r: reqs.append(r.url) if "/api/runs?" in r.url and "q=" in r.url else None)
        pg.locator("#f-q").click()
        pg.keyboard.type("landing", delay=25)
        pg.wait_for_function("location.hash.includes('q=landing')", timeout=5000)
        pg.wait_for_timeout(500)
        self.assertEqual(len(reqs), 1, reqs)
        self.assertIn("q=landing", reqs[0])

    def test_slash_focuses_the_search_box(self):
        pg = self.open(self.page())
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press("/")
        self.assertEqual(pg.evaluate("document.activeElement.id"), "f-q")

    def test_j_and_k_move_through_rows_and_enter_opens_one(self):
        pg = self.open(self.page())
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press("j")
        first = pg.locator("#runs-body tr.run-row").nth(0)
        self.assertEqual(first.get_attribute("aria-selected"), "true")
        pg.keyboard.press("j")
        self.assertEqual(pg.locator("#runs-body tr.run-row").nth(1).get_attribute("aria-selected"), "true")
        self.assertEqual(pg.locator("#runs-body tr[aria-selected='true']").count(), 1)
        pg.keyboard.press("k")
        self.assertEqual(first.get_attribute("aria-selected"), "true")
        href = first.locator("a").first.get_attribute("href")
        pg.keyboard.press("Enter")
        pg.wait_for_function(f"location.hash === {href!r}")


class TestLongKeyAndNarrow(_Base):
    def test_a_92_character_group_key_truncates_with_tooltip_and_copy(self):
        key = self.srv.manifest["long_group"]
        self.assertEqual(len(key), 92)
        pg = self.open(self.page(), f"?group={key}")
        cell = pg.locator("#runs-body tr.run-row .grp-key").first
        self.assertEqual(cell.get_attribute("title"), key)
        self.assertLess(cell.bounding_box()["width"], 400)
        pg.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.srv.base)
        pg.locator("#runs-body tr.run-row button.copy-key").first.click()
        self.assertEqual(pg.evaluate("navigator.clipboard.readText()"), key)

    def test_long_key_keeps_the_page_390_wide(self):
        key = self.srv.manifest["long_group"]
        pg = self.open(self.page(390, 844), f"?group={key}")
        self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 390)
        self.assertLessEqual(pg.evaluate("document.body.scrollWidth"), 390)

    def test_unfiltered_page_is_390_wide(self):
        pg = self.open(self.page(390, 844))
        self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 390)

    def test_row_expand_chevron_shares_a_line_with_the_verdict_chip_at_390(self):
        pg = self.open(self.page(390, 844))
        row = pg.locator("#runs-body tr.run-row").first
        btn = row.locator(".row-expand").bounding_box()
        chip = row.locator(".chip").first.bounding_box()
        self.assertLess(abs((btn["y"] + btn["height"] / 2) - (chip["y"] + chip["height"] / 2)), 10,
                        (btn, chip))
        self.assertLess(btn["x"], chip["x"])

    def test_row_expand_reveals_group_and_started_at_390(self):
        pg = self.open(self.page(390, 844))
        pg.locator("#runs-body tr.run-row .row-expand").first.click()
        detail = pg.locator("#runs-body tr.row-detail").first
        self.assertIn("group", detail.inner_text().lower())

    def test_facet_controls_fit_at_390(self):
        pg = self.open(self.page(390, 844))
        box = pg.locator(".facet-bar").bounding_box()
        self.assertLessEqual(box["x"] + box["width"], 390)


class TestNewRunsBanner(unittest.TestCase):
    """Rows that arrive while reading appear behind a banner, not above the reader."""

    @classmethod
    def setUpClass(cls):
        if not HAS_PLAYWRIGHT:
            raise unittest.SkipTest("playwright not installed")
        cls.srv = bc.CorpusServer("single")
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()
        cls.srv.close()

    def test_new_runs_show_a_banner_and_do_not_move_the_list(self):
        from datetime import UTC, datetime, timedelta

        from orchestral.storage import RunMeta, RunStore

        ctx = self.browser.new_context(viewport={"width": 1440, "height": 900})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        pg.goto(f"{self.srv.base}/#/runs")
        pg.wait_for_selector("#view[data-ready]")
        before = pg.locator("#runs-body tr.run-row").count()
        self.assertEqual(pg.locator("#runs-new").count(), 0)
        store = RunStore(self.srv.root)
        now = datetime.now(UTC)
        for i in range(3):
            store.index_meta(RunMeta(
                run_id=f"newrun{i:04d}", orchestrator="corpus/orch-a", task_id="corpus-landing-page",
                worker="corpus/worker-cheap", status="finished", passes=True,
                started_at=(now + timedelta(seconds=i)).isoformat(), run_dir=str(self.srv.root)))
        # the poller re-checks when the tab becomes visible again
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        banner = pg.locator("#runs-new")
        banner.wait_for(timeout=10000)
        self.assertIn("3 new runs", banner.inner_text())
        self.assertEqual(pg.locator("#runs-body tr.run-row").count(), before)
        banner.locator("button").click()
        pg.wait_for_function(f"document.querySelectorAll('#runs-body tr.run-row').length === {before + 3}")
        self.assertEqual(pg.locator("#runs-new").count(), 0)


if __name__ == "__main__":
    unittest.main()
