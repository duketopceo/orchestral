"""U14: New run, Models and Guide in a real browser, local and hosted.

Skipped without playwright. Zero network and zero spend: the key-free corpus
on loopback, and every POST that could launch is intercepted by the test, so
nothing here can start a run, paid or not."""

from __future__ import annotations

import json
import unittest
import urllib.parse

import browser_corpus as bc
import browser_hosted as bh

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


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

    def open(self, route, width=1440, height=900, base=None):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, has_touch=width < 500)
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errors: list[str] = []
        self.posts: list[dict] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.add_init_script("try{localStorage.setItem('orchestral.theme','paper')}catch(e){}")
        pg.goto(f"{base or self.base}/#{route}")
        pg.wait_for_selector("#view[data-ready]", timeout=30000)
        return pg

    def intercept_launch(self, pg, responses):
        """Every POST /api/run is recorded and answered from `responses` (a list
        consumed in order, the last one repeating). Nothing reaches the server."""
        def handler(route):
            if route.request.method != "POST":
                return route.continue_()
            self.posts.append(dict(urllib.parse.parse_qsl(route.request.post_data or "")))
            status, body = responses[min(len(self.posts) - 1, len(responses) - 1)]
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        pg.route("**/api/run", handler)


class TestNewRun(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.srv.base

    def pick_task(self, pg, text="landing"):
        pg.fill("#launch-task", text)
        pg.keyboard.press("ArrowDown")
        pg.keyboard.press("Enter")

    def test_task_combobox_filters_groups_and_works_by_keyboard(self):
        pg = self.open("/new")
        self.assertEqual(pg.locator("h1").inner_text().strip(), "New run")
        box = pg.locator("#launch-task")
        self.assertEqual(box.get_attribute("role"), "combobox")
        self.assertEqual(box.get_attribute("aria-autocomplete"), "list")
        box.focus()
        pg.keyboard.press("ArrowDown")
        self.assertEqual(box.get_attribute("aria-expanded"), "true")
        total = pg.locator("#launch-task-list [role=option]").count()
        self.assertGreaterEqual(total, 7)
        self.assertEqual(pg.locator("#launch-task-list [role=presentation]").first.inner_text().lower(), "html")  # grouped by type
        self.assertIn("about $", pg.locator("#launch-task-list").inner_text())  # expected cost from history
        pg.fill("#launch-task", "slug")
        self.assertEqual(pg.locator("#launch-task-list [role=option]").count(), 1)
        pg.keyboard.press("ArrowDown")
        pg.keyboard.press("Enter")
        self.assertEqual(pg.input_value("input[name=task]"), "corpus-code-slugify")
        self.assertEqual(pg.locator("#launch-task-list").is_visible(), False)

    def test_submit_with_no_task_focuses_the_field_and_announces(self):
        pg = self.open("/new")
        self.intercept_launch(pg, [(200, {"run_id": "x"})])
        pg.click("#launch-btn")
        self.assertEqual(pg.evaluate("document.activeElement.id"), "launch-task")
        self.assertEqual(pg.locator("#launch-task").get_attribute("aria-invalid"), "true")
        err = pg.locator("#launch-err")
        self.assertEqual(err.get_attribute("role"), "alert")
        self.assertIn("task", err.inner_text().lower())
        self.assertEqual(self.posts, [])

    def test_blur_validates_inline_and_input_is_kept_on_error(self):
        pg = self.open("/new")
        pg.fill("input[name=replicates]", "99")
        pg.focus("input[name=seed]")
        self.assertEqual(pg.locator("input[name=replicates]").get_attribute("aria-invalid"), "true")
        self.assertIn("50", pg.locator("#err-replicates").inner_text())
        self.assertEqual(pg.input_value("input[name=replicates]"), "99")

    def test_model_pickers_are_role_qualified_and_grouped_by_history(self):
        pg = self.open("/new")
        pg.focus("#launch-orchestrator")
        pg.keyboard.press("ArrowDown")
        text = pg.locator("#launch-orchestrator-list").inner_text()
        self.assertIn("corpus/orch-a", text)
        self.assertNotIn("corpus/worker-cheap", text)
        pg.keyboard.press("Escape")
        pg.focus("#launch-worker")
        pg.keyboard.press("ArrowDown")
        self.assertIn("corpus/worker-cheap", pg.locator("#launch-worker-list").inner_text())

    def test_dry_run_is_default_and_free(self):
        pg = self.open("/new")
        self.assertTrue(pg.is_checked("input[name=dry_run]"))
        btn = pg.locator("#launch-btn")
        self.assertIn("Launch dry run", btn.inner_text())
        self.assertIn("No API spend", pg.inner_text("#spend-line"))

    def test_paid_run_shows_price_segment_and_needs_an_explicit_click(self):
        pg = self.open("/new")
        self.intercept_launch(pg, [(200, {"run_id": "x"})])
        self.pick_task(pg)
        pg.focus("#launch-orchestrator")
        pg.fill("#launch-orchestrator", "orch-a")
        pg.keyboard.press("ArrowDown")
        pg.keyboard.press("Enter")
        pg.fill("#launch-worker", "worker-cheap")
        pg.keyboard.press("ArrowDown")
        pg.keyboard.press("Enter")
        pg.uncheck("input[name=dry_run]")
        pg.wait_for_function("!document.getElementById('launch-btn').disabled")
        btn = pg.locator("#launch-btn")
        self.assertIn("Launch paid run", btn.inner_text())
        self.assertRegex(btn.locator(".spend-price").inner_text(), r"est\.|unknown")
        self.assertIn("Estimated cost", pg.inner_text("#spend-line"))
        btn.click()
        pg.wait_for_selector("dialog.spend-dialog[open]")
        self.assertEqual(pg.evaluate("document.activeElement.dataset.act"), "cancel")
        pg.keyboard.press("Enter")  # Enter alone must never spend
        pg.wait_for_function("!document.querySelector('dialog.spend-dialog')")
        self.assertEqual(self.posts, [])
        self.assertIn("Nothing was spent", pg.inner_text("#launch-err"))

    def test_confirm_sends_the_confirm_flag_and_one_idempotency_key(self):
        pg = self.open("/new")
        self.intercept_launch(pg, [(200, {"run_id": "r-stub"})])
        self.pick_task(pg)
        pg.uncheck("input[name=dry_run]")
        pg.wait_for_function("!document.getElementById('launch-btn').disabled")
        pg.click("#launch-btn")
        pg.wait_for_selector("dialog.spend-dialog[open]")
        pg.click("dialog.spend-dialog [data-act=confirm]")
        pg.wait_for_function("location.hash === '#/run/r-stub'")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["confirm_spend"], "1")
        self.assertTrue(self.posts[0]["idempotency_key"])

    def test_run_dry_first_launches_a_dry_run_with_no_confirm_flag(self):
        pg = self.open("/new")
        self.intercept_launch(pg, [(200, {"run_id": "r-dry"})])
        self.pick_task(pg)
        pg.uncheck("input[name=dry_run]")
        pg.wait_for_function("!document.getElementById('launch-btn').disabled")
        pg.click("#launch-btn")
        pg.wait_for_selector("dialog.spend-dialog[open]")
        pg.click("dialog.spend-dialog [data-act=dry]")
        pg.wait_for_function("location.hash === '#/run/r-dry'")
        self.assertEqual(self.posts[0].get("dry_run"), "1")
        self.assertNotIn("confirm_spend", self.posts[0])

    def test_a_409_gate_reopens_the_sheet_and_cancel_spends_nothing(self):
        pg = self.open("/new")
        self.intercept_launch(pg, [(409, {"needs_confirm": True, "estimate": {"total_usd": None, "basis_label": "x"}})])
        self.pick_task(pg)
        pg.uncheck("input[name=dry_run]")
        pg.wait_for_function("!document.getElementById('launch-btn').disabled")
        pg.click("#launch-btn")
        pg.wait_for_selector("dialog.spend-dialog[open]")
        pg.click("dialog.spend-dialog [data-act=confirm]")
        pg.wait_for_function("document.querySelectorAll('dialog.spend-dialog').length === 1 && !!document.querySelector('dialog.spend-dialog[open]')")
        self.assertEqual(len(self.posts), 1)
        pg.click("dialog.spend-dialog [data-act=cancel]")
        pg.wait_for_function("!document.querySelector('dialog.spend-dialog')")
        self.assertIn("Nothing was spent", pg.inner_text("#launch-err"))
        self.assertEqual(len(self.posts), 1)

    def test_390_new_run_fits(self):
        pg = self.open("/new", 390, 844)
        self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390)
        self.assertEqual(self.errors, [])


class ModelsGuideCases:
    def test_models_show_capability_icons_usage_bars_and_the_sync_stamp(self):
        pg = self.open("/models")
        stamp = pg.locator("#provider-sync")
        self.assertIn(stamp.get_attribute("data-state"), {"never", "synced"})
        if stamp.get_attribute("data-state") == "never":
            self.assertIn("Never synced", stamp.inner_text())
        self.assertGreaterEqual(pg.locator(".usage-bar").count(), 1)
        for bar in pg.locator(".usage-bar").all()[:5]:
            self.assertIn(bar.get_attribute("role"), {"img", None})
        icons = pg.locator(".cap-icon use").evaluate_all("els => els.map(e => e.getAttribute('href'))")
        self.assertTrue(all(h.startswith("#i-") for h in icons), icons)
        self.assertEqual(self.errors, [])

    def test_models_filter_reads_q_from_the_url(self):
        pg = self.open("/models?q=worker-cheap")
        self.assertEqual(pg.input_value("#cat-q"), "worker-cheap")
        shown = pg.locator(".cat-row:visible").count()
        self.assertGreaterEqual(shown, 1)
        self.assertLess(shown, pg.locator(".cat-row").count())

    def test_guide_s_param_scrolls_to_the_section_and_keeps_the_router(self):
        pg = self.open("/about?s=judge", height=500)
        sec = pg.locator("#s-judge")
        self.assertEqual(sec.count(), 1)
        top = pg.evaluate("document.getElementById('s-judge').getBoundingClientRect().top")
        self.assertLess(top, 300)
        self.assertEqual(pg.locator("h1").count(), 1)
        pg.evaluate("location.hash = '#/about?s=naming'")
        pg.wait_for_function("document.getElementById('s-naming').getBoundingClientRect().top < 300")
        pg.evaluate("location.hash = '#/runs'")
        pg.wait_for_selector("#runs-body")
        self.assertEqual(self.errors, [])

    def test_guide_has_legend_and_help_popovers(self):
        pg = self.open("/about")
        legend = pg.locator(".legend").first
        rests = pg.locator(".legend-rests")
        self.assertGreaterEqual(legend.locator("svg use[href='#i-pass']").count(), 1)
        self.assertGreaterEqual(legend.locator("svg use[href='#i-low-n']").count(), 1)
        self.assertGreaterEqual(rests.locator("svg use[href^='#r-']").count(), 3)  # the Rests
        btn = pg.locator("button.term-help").first
        self.assertTrue(btn.get_attribute("aria-label"))
        btn.click()
        pop = pg.locator(f"#{btn.get_attribute('popovertarget')}")
        self.assertTrue(pop.is_visible())
        self.assertIn("#/about?s=", pop.locator("a").get_attribute("href"))
        pg.keyboard.press("Escape")
        self.assertFalse(pop.is_visible())

    def test_390_models_and_guide_fit(self):
        for route in ("/models", "/about"):
            pg = self.open(route, 390, 844)
            self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390, route)


class TestModelsGuideLocal(ModelsGuideCases, _Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = cls.srv.base


class TestHosted(ModelsGuideCases, _Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = bh.hosted_base()

    def test_new_run_stays_read_only_with_no_form_and_no_estimate_calls(self):
        pg = self.open("/new")
        self.assertIn("Not available on this read-only build", pg.inner_text("#view"))
        self.assertEqual(pg.locator("#launch, #launch-task").count(), 0)


if __name__ == "__main__":
    unittest.main()
