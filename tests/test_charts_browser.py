"""U13: the chart kit, Pairings and Compare in a real browser.

Skipped without playwright. Zero network: loopback server over the key-free
U23 corpus. Run in CI by the non-required ``browser`` job (KTD13).
"""

from __future__ import annotations

import unittest
from urllib.parse import quote

import browser_corpus as bc

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

BIG = "corpus-main:r3"          # 4 pairings, 232 finished runs
LENSES = ["overall", "high_cost", "low_cost", "sweet_spot", "divergence"]

CONTRAST_JS = """() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', {willReadFrequently: true});
  const rgb = c => { cx.clearRect(0,0,1,1); cx.fillStyle = '#000'; cx.fillStyle = c; cx.fillRect(0,0,1,1);
    const d = cx.getImageData(0,0,1,1).data; return [d[0], d[1], d[2]]; };
  const lum = ([r,g,b]) => { const f = v => { v /= 255; return v <= .03928 ? v/12.92 : ((v+.055)/1.055)**2.4; };
    return .2126*f(r) + .7152*f(g) + .0722*f(b); };
  const out = [];
  for (const el of document.querySelectorAll('a.hm-cell:not(.hm-none)')) {
    const cs = getComputedStyle(el);
    // composite the cell's own background over the page surface
    const bgc = rgb(cs.backgroundColor);
    const l1 = lum(bgc), l2 = lum(rgb(cs.color));
    const ratio = (Math.max(l1,l2) + .05) / (Math.min(l1,l2) + .05);
    out.push([el.className, ratio]);
  }
  return out;
}"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = bc.shared()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def page(self, width=1440, height=900, theme="paper", reduced=False):
        ctx = self.browser.new_context(
            viewport={"width": width, "height": height}, has_touch=width <= 640,
            reduced_motion="reduce" if reduced else "no-preference")
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errors: list[str] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.add_init_script(f"try{{localStorage.setItem('orchestral.theme','{theme}')}}catch(e){{}}")
        return pg

    def open(self, pg, route):
        pg.goto(f"{self.srv.base}/#{route}")
        pg.wait_for_selector("#view[data-ready]", timeout=30000)
        return pg

    def board(self, pg, group=BIG, lens="overall"):
        return self.open(pg, f"/leaderboard?group={quote(group)}&lens={lens}")


class TestPairingsStripPlot(_Base):
    def test_strip_rows_equal_ranking_table_rows_in_order_for_each_lens(self):
        pg = self.page()
        for lens in LENSES:
            self.board(pg, lens=lens)
            strip = pg.eval_on_selector_all("figure.chart-strip a.ch-mark", "els => els.map(e => e.dataset.key)")
            table = pg.eval_on_selector_all("table.rank tr[data-target]", "els => els.map(e => e.dataset.target)")
            self.assertTrue(strip, lens)
            self.assertEqual(strip, table, lens)

    def test_cost_axis_has_labeled_ticks(self):
        pg = self.board(self.page())
        ticks = pg.eval_on_selector_all("figure.chart-strip text.ch-tick", "els => els.map(e => e.textContent)")
        self.assertIn("$0.01", ticks)
        self.assertIn("$0.1", ticks)
        self.assertEqual(ticks.count("0%"), 1)
        self.assertEqual(ticks.count("100%"), 1)

    def test_every_chart_has_role_title_desc_and_a_working_table_toggle(self):
        pg = self.board(self.page())
        charts = pg.locator("svg[role='img']")
        self.assertGreaterEqual(charts.count(), 1)
        for i in range(charts.count()):
            svg = charts.nth(i)
            self.assertEqual(svg.locator("title").count(), 1)
            self.assertGreater(len(svg.locator("desc").text_content().strip()), 20)
            ids = svg.get_attribute("aria-labelledby").split()
            for ident in ids:
                self.assertEqual(pg.locator(f"[id='{ident}']").count(), 1)
        fig = pg.locator("figure.chart-strip")
        table = fig.locator("details.chart-table table")
        self.assertFalse(table.is_visible())
        fig.locator("summary").click()
        self.assertTrue(table.is_visible())
        self.assertEqual(table.locator("tbody tr").count(), pg.locator("figure.chart-strip a.ch-mark").count())

    def test_marks_are_focusable_and_enter_opens_the_pairing(self):
        pg = self.board(self.page())
        mark = pg.locator("figure.chart-strip a.ch-mark").first
        mark.focus()
        self.assertTrue(pg.evaluate("document.activeElement.classList.contains('ch-mark')"))
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => location.hash.startsWith('#/card')")
        self.assertIn("kind=pairing", pg.evaluate("location.hash"))

    def test_a_group_with_one_pairing_states_it_instead_of_five_lenses(self):
        pg = self.board(self.page(), group="corpus-solo")
        self.assertEqual(pg.locator(".lens-tab").count(), 0)
        self.assertIn("one pairing", pg.locator(".single-row-note").inner_text().lower())
        self.assertEqual(pg.locator("figure.chart-strip a.ch-mark").count(), 1)

    def test_default_scope_is_never_a_degenerate_cohort(self):
        pg = self.open(self.page(), "/leaderboard")
        value = pg.locator("#lb-group").input_value()
        default = self.srv.api("/api/pairings")["default_group"]
        self.assertEqual(value, default)
        self.assertGreaterEqual(pg.locator("figure.chart-strip a.ch-mark").count(), 3)

    def test_all_low_n_cohort_says_no_pairing_has_n_10(self):
        pg = self.board(self.page(), group="corpus-thin")
        self.assertIn("No pairing has n≥10 yet", pg.locator("#view").inner_text())
        self.assertGreaterEqual(pg.locator("figure.chart-strip [fill='url(#hatch)']").count(), 1)

    def test_unmetered_pairings_are_labeled_and_not_plotted_on_cost(self):
        pg = self.board(self.page(), group="corpus-dry")
        text = pg.locator("figure.chart-strip").inner_text()
        self.assertIn("unmetered", text)
        self.assertEqual(pg.locator("figure.chart-strip .ch-cost-pt").count(), 0)

    def test_heatmap_cells_are_links_and_enter_opens_the_pairing(self):
        pg = self.board(self.page())
        cell = pg.locator("a.hm-cell:not(.hm-none)").first
        cell.focus()
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => location.hash.startsWith('#/card')")

    def test_heatmap_text_meets_contrast_in_both_themes(self):
        for theme in ("paper", "stage"):
            pg = self.board(self.page(theme=theme))
            for cls, ratio in pg.evaluate(CONTRAST_JS):
                self.assertGreaterEqual(ratio, 4.5, f"{theme} {cls}")

    def test_chart_marks_do_not_animate_in_either_motion_mode(self):
        for reduced in (False, True):
            pg = self.board(self.page(reduced=reduced))
            dur = pg.evaluate("getComputedStyle(document.querySelector('figure.chart-strip a.ch-mark *'))"
                              ".transitionDuration")
            self.assertEqual(dur, "0s")

    def test_no_page_errors(self):
        self.board(self.page())
        self.assertEqual(self.errors, [])


class TestPairingsNarrow(_Base):
    def test_390_stacks_the_strip_and_keeps_the_page_in_bounds(self):
        for theme in ("paper", "stage"):
            pg = self.board(self.page(390, 844, theme))
            self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390, theme)
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth"), 390, theme)
            self.assertEqual(pg.locator("figure.chart-strip svg").get_attribute("data-layout"), "stacked")
            mark = pg.locator("figure.chart-strip a.ch-mark").first
            mark.focus()
            self.assertTrue(pg.evaluate("document.activeElement.classList.contains('ch-mark')"))
            svg_w = pg.evaluate("document.querySelector('figure.chart-strip svg').getBoundingClientRect().width")
            self.assertLessEqual(svg_w, 390)

    def test_390_matrix_scrolls_inside_its_own_container_with_a_caption(self):
        pg = self.board(self.page(390, 844))
        box = pg.locator(".chart-scroll").first
        self.assertEqual(box.get_attribute("tabindex"), "0")
        self.assertEqual(box.get_attribute("role"), "region")
        self.assertIn("scroll", box.locator("xpath=preceding-sibling::*[1]").inner_text().lower())
        self.assertGreater(pg.evaluate("e => e.scrollWidth - e.clientWidth", box.element_handle()), 0)
        first_col = pg.evaluate(
            "() => getComputedStyle(document.querySelector('.chart-scroll tbody th')).position")
        self.assertEqual(first_col, "sticky")
        self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390)

    def test_390_every_matrix_cell_is_reachable_by_scrolling_the_container(self):
        pg = self.board(self.page(390, 844))
        last = pg.locator("a.hm-cell:not(.hm-none)").last
        last.focus()
        self.assertTrue(pg.evaluate("document.activeElement.classList.contains('hm-cell')"))
        self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390)


class TestCompare(_Base):
    def _disjoint_pair(self):
        names = ["corpus-orphan", "corpus-failed", "corpus-thin", "corpus-solo", "corpus-unpriced"]
        for a in names:
            for b in names:
                if a != b and self.srv.api(f"/api/compare?a={quote(a)}&b={quote(b)}")["shared"] == 0:
                    return a, b
        self.fail("corpus has no disjoint pair")

    def test_disjoint_groups_report_zero_shared_cells_and_list_one_sided_rows(self):
        a, b = self._disjoint_pair()
        pg = self.open(self.page(), f"/compare?a={quote(a)}&b={quote(b)}")
        text = pg.locator("#cmp-out").inner_text()
        self.assertIn("0 shared cells", text)
        self.assertGreaterEqual(pg.locator(".cmp-row").count(), 1)
        self.assertEqual(pg.locator(".cmp-row[data-verdict='one-sided']").count(), pg.locator(".cmp-row").count())

    def test_same_group_is_blocked_with_a_message(self):
        pg = self.open(self.page(), f"/compare?a={quote(BIG)}&b={quote(BIG)}")
        self.assertIn("different", pg.locator("#cmp-out [role='alert']").inner_text())
        self.assertEqual(pg.locator(".cmp-row").count(), 0)

    def test_summary_counts_and_rows_sorted_by_regression_with_dumbbells(self):
        pg = self.open(self.page(), "/compare?a=corpus-main:r0&b=corpus-main:r1")
        api = self.srv.api("/api/compare?a=corpus-main:r0&b=corpus-main:r1")
        summary = pg.locator(".cmp-summary").inner_text()
        for verdict, n in api["verdicts"].items():
            self.assertIn(f"{n}", summary, verdict)
        deltas = pg.eval_on_selector_all(".cmp-row", "els => els.map(e => e.dataset.delta)")
        nums = [float(d) for d in deltas if d != ""]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(pg.locator(".cmp-row svg[role='img']").count(), pg.locator(".cmp-row").count())
        self.assertEqual(pg.locator(".cmp-row").first.get_attribute("data-verdict"), "regressed")
        self.assertIn("cost", pg.locator("#cmp-out").inner_text().lower())

    def test_compare_has_a_table_fallback(self):
        pg = self.open(self.page(), "/compare?a=corpus-main:r0&b=corpus-main:r1")
        table = pg.locator("details.chart-table table")
        self.assertFalse(table.is_visible())
        pg.locator("details.chart-table summary").click()
        self.assertTrue(table.is_visible())

    def test_verdicts_carry_glyph_and_word_not_just_color(self):
        pg = self.open(self.page(), "/compare?a=corpus-main:r0&b=corpus-main:r1")
        chip = pg.locator(".cmp-row .chip").first
        self.assertTrue(chip.inner_text().strip())
        self.assertGreaterEqual(chip.locator("svg").count(), 1)

    def test_390_compare_fits_and_stays_readable(self):
        for theme in ("paper", "stage"):
            pg = self.open(self.page(390, 844, theme), "/compare?a=corpus-main:r0&b=corpus-main:r1")
            self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390, theme)
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth"), 390, theme)
            row = pg.locator(".cmp-row").first
            self.assertTrue(row.locator("svg[role='img']").is_visible())
            w = row.evaluate("e => e.getBoundingClientRect().width")
            self.assertLessEqual(w, 390)
            self.assertGreaterEqual(row.locator("svg").first.evaluate("e => e.getBoundingClientRect().width"), 120)


if __name__ == "__main__":
    unittest.main()
