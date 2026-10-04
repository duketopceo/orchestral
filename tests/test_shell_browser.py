"""U7: the shell, IA and layout primitives in a real browser.

Skipped without playwright. Zero network: loopback server over the key-free
U23 corpus. Run in CI by the non-required ``browser`` job (KTD13).
"""

from __future__ import annotations

import json
import unittest

import browser_corpus as bc

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

NAV = [("Now", "#/"), ("Runs", "#/runs"), ("Pairings", "#/leaderboard"),
       ("Compare", "#/compare"), ("Experiments", "#/experiment"), ("Publish", "#/cards"),
       ("Models", "#/models"), ("New run", "#/new"), ("Guide", "#/about")]


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = bc.shared()
        cls.routes = bc.routes(cls.srv)
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def page(self, width=1440, height=900, theme="paper", reduced=False, meta=None):
        ctx = self.browser.new_context(
            viewport={"width": width, "height": height}, has_touch=width <= 640,
            reduced_motion="reduce" if reduced else "no-preference")
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errors: list[str] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.add_init_script(f"try{{localStorage.setItem('orchestral.theme','{theme}')}}catch(e){{}}")
        if meta is not None:
            pg.route("**/api/meta", lambda r: r.fulfill(
                status=200, content_type="application/json", body=json.dumps(meta)))
        return pg

    def open(self, pg, route="/"):
        pg.goto(f"{self.srv.base}/#{route}")
        pg.wait_for_selector("#view[data-ready]", timeout=30000)
        return pg


class TestInformationArchitecture(_Base):
    def test_rail_lists_the_ia_in_order_with_stable_routes(self):
        pg = self.open(self.page())
        links = pg.locator("#nav a")
        got = [(links.nth(i).inner_text().strip(), links.nth(i).get_attribute("href"))
               for i in range(links.count())]
        self.assertEqual(got, NAV)

    def test_landmarks_and_one_h1_per_route(self):
        pg = self.page()
        for route in self.routes:
            self.open(pg, route)
            self.assertEqual(pg.locator("h1").count(), 1, route)
            self.assertEqual(pg.locator("main").count(), 1)
            self.assertEqual(pg.locator("nav[aria-label='Primary']").count(), 1)
            self.assertEqual(pg.locator("nav[aria-label='Mobile']").count(), 1)
            self.assertEqual(pg.locator("#live-region[aria-live='polite']").count(), 1)

    def test_active_link_is_marked_with_aria_current(self):
        pg = self.open(self.page(), "/runs")
        cur = pg.locator("#nav a[aria-current='page']")
        self.assertEqual(cur.count(), 1)
        self.assertEqual(cur.get_attribute("href"), "#/runs")

    def test_experiments_is_a_real_route(self):
        pg = self.open(self.page(), "/experiment")
        self.assertEqual(pg.get_attribute("#view", "data-ready"), "ok")
        self.assertIn("Experiments", pg.inner_text("h1"))
        self.assertNotIn("Unknown view", pg.inner_text("#view"))

    def test_new_run_is_hidden_when_capabilities_forbid_launch(self):
        meta = {"mode": "local", "synced_at": None, "source_commit": None,
                "capabilities": {"launch": False, "cancel": False, "flag_write": False,
                                 "thread": False, "png_capture": False, "live_stream": True}}
        pg = self.open(self.page(meta=meta))
        self.assertEqual(pg.locator("[data-route='/new']").count(), 0)
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.click("#more-open")
        self.assertEqual(pg.locator("#more-sheet [data-route='/new']").count(), 0)


class TestResponsive(_Base):
    def test_every_route_fits_390_in_both_themes(self):
        for theme in ("paper", "stage"):
            pg = self.page(390, 844, theme)
            for route in self.routes:
                self.open(pg, route)
                self.assertEqual(pg.evaluate("document.body.scrollWidth"), 390, f"{theme} {route}")
                self.assertEqual(pg.evaluate("document.documentElement.scrollWidth"), 390, f"{theme} {route}")

    def test_at_1100_the_rail_is_icons_with_accessible_names(self):
        pg = self.open(self.page(1100, 800))
        self.assertEqual(pg.evaluate("document.querySelector('#rail').getBoundingClientRect().width"), 56)
        links = pg.locator("#nav a")
        self.assertEqual(links.count(), len(NAV))
        for i, (label, _) in enumerate(NAV):
            a = links.nth(i)
            self.assertEqual(a.get_attribute("aria-label"), label)
            self.assertTrue(a.locator("svg use").count() >= 1)
            self.assertFalse(a.locator(".nav-label").is_visible())

    def test_at_1440_the_rail_is_232_wide_with_visible_labels(self):
        pg = self.open(self.page(1440, 900))
        self.assertEqual(pg.evaluate("document.querySelector('#rail').getBoundingClientRect().width"), 232)
        self.assertTrue(pg.locator("#nav a .nav-label").first.is_visible())

    def test_at_640_the_bottom_bar_has_five_items(self):
        pg = self.open(self.page(640, 800))
        self.assertFalse(pg.locator("#rail").is_visible())
        bar = pg.locator("nav[aria-label='Mobile']")
        self.assertTrue(bar.is_visible())
        self.assertEqual(bar.locator("a, button").all_inner_texts(),
                         ["Now", "Runs", "Pairings", "Publish", "More"])
        box = bar.bounding_box()
        self.assertAlmostEqual(box["y"] + box["height"], 800, delta=1)

    def test_at_768_the_bar_is_not_shown(self):
        pg = self.open(self.page(768, 800))
        self.assertFalse(pg.locator("nav[aria-label='Mobile']").is_visible())
        self.assertTrue(pg.locator("#rail").is_visible())

    def test_390_reaches_every_route_theme_and_palette_by_tap_alone(self):
        pg = self.open(self.page(390, 844))
        bar = {"Runs": "#/runs", "Pairings": "#/leaderboard", "Publish": "#/cards", "Now": "#/"}
        for label, hash_ in bar.items():
            pg.tap(f"nav[aria-label='Mobile'] >> text={label}")
            pg.wait_for_function("h => (location.hash || '#/') === h", arg=hash_)
        sheet_routes = {"Compare": "#/compare", "Experiments": "#/experiment", "Models": "#/models",
                        "Guide": "#/about", "New run": "#/new"}
        for label, hash_ in sheet_routes.items():
            pg.tap("#more-open")
            pg.wait_for_selector("#more-sheet[open]")
            pg.tap(f"#more-sheet >> text={label}")
            pg.wait_for_function("h => location.hash === h", arg=hash_)
            self.assertFalse(pg.locator("#more-sheet").evaluate("e => e.open"), label)
        # theme toggle inside the sheet
        before = pg.evaluate("document.documentElement.dataset.theme")
        pg.tap("#more-open")
        pg.tap("#more-sheet [data-theme-cycle]")
        self.assertNotEqual(pg.evaluate("document.documentElement.dataset.theme"), before)
        pg.keyboard.press("Escape")
        # palette
        pg.tap("#more-open")
        pg.tap("#more-sheet [data-open-palette]")
        pg.wait_for_selector("#palette[open]")
        self.assertTrue(pg.locator("#palette input").is_visible())

    def test_more_sheet_is_a_dialog_that_traps_focus_and_restores_it(self):
        pg = self.open(self.page(390, 844))
        pg.focus("#more-open")
        pg.keyboard.press("Enter")
        pg.wait_for_selector("#more-sheet[open]")
        self.assertEqual(pg.get_attribute("#more-sheet", "aria-modal"), "true")
        self.assertIsNotNone(pg.get_attribute("#more-sheet", "aria-label"))
        seen = set()
        for _ in range(40):
            pg.keyboard.press("Tab")
            inside = pg.evaluate("document.querySelector('#more-sheet').contains(document.activeElement)")
            self.assertTrue(inside, "focus left the sheet")
            seen.add(pg.evaluate("document.activeElement.textContent.trim().slice(0,20)"))
        self.assertGreater(len(seen), 4)
        pg.keyboard.press("Escape")
        self.assertFalse(pg.locator("#more-sheet").evaluate("e => e.open"))
        self.assertEqual(pg.evaluate("document.activeElement.id"), "more-open")

    def test_more_sheet_lists_activity(self):
        pg = self.open(self.page(390, 844))
        pg.tap("#more-open")
        pg.wait_for_selector("#more-sheet .rail-job")
        self.assertGreaterEqual(pg.locator("#more-sheet .rail-job").count(), 1)


class TestKeyboard(_Base):
    def test_skip_link_is_first_tab_stop_and_moves_focus_to_main(self):
        pg = self.open(self.page())
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press("Tab")
        self.assertEqual(pg.evaluate("document.activeElement.className"), "skip-link")
        pg.keyboard.press("Enter")
        self.assertEqual(pg.evaluate("document.activeElement.id"), "main")

    def test_g_sequences_navigate(self):
        pg = self.open(self.page())
        for keys, hash_ in (("r", "#/runs"), ("p", "#/leaderboard"), ("c", "#/compare"),
                            ("e", "#/experiment"), ("o", "#/")):
            pg.keyboard.press("g")
            pg.keyboard.press(keys)
            pg.wait_for_function("h => (location.hash || '#/') === h", arg=hash_)

    def test_question_mark_opens_key_map_and_escape_restores_focus(self):
        pg = self.open(self.page())
        pg.focus("#nav a[data-route='/runs']")
        pg.keyboard.press("?")
        pg.wait_for_selector("#keymap[open]")
        self.assertIn("g r", pg.inner_text("#keymap"))
        pg.keyboard.press("Escape")
        self.assertFalse(pg.locator("#keymap").evaluate("e => e.open"))
        self.assertEqual(pg.evaluate("document.activeElement.getAttribute('data-route')"), "/runs")

    def test_slash_focuses_the_page_filter(self):
        pg = self.open(self.page(), "/runs")
        pg.keyboard.press("/")
        self.assertEqual(pg.evaluate("document.activeElement.type"), "search")

    def test_keys_are_ignored_while_typing(self):
        pg = self.open(self.page(), "/runs")
        pg.focus("input[type=search]")
        pg.keyboard.type("g?")
        self.assertFalse(pg.locator("#keymap").evaluate("e => !!e.open"))

    def test_cmd_k_opens_the_palette(self):
        pg = self.open(self.page())
        pg.keyboard.press("Control+k")
        pg.wait_for_selector("#palette[open]")
        pg.keyboard.type("comp")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => location.hash === '#/compare'")


class TestTable(_Base):
    def test_priority_3_columns_hide_at_390_and_row_expand_reveals_them(self):
        pg = self.open(self.page(390, 844), "/runs")
        pg.wait_for_selector("#runs-body tr .chip")
        th = pg.locator("table.data th[data-pri='3']").first
        self.assertFalse(th.is_visible())
        btn = pg.locator("#runs-body .row-expand").first
        self.assertEqual(btn.get_attribute("aria-expanded"), "false")
        btn.tap()
        self.assertEqual(btn.get_attribute("aria-expanded"), "true")
        detail = pg.locator("#runs-body tr.row-detail").first
        self.assertTrue(detail.is_visible())
        self.assertIn("group", detail.inner_text().lower())
        self.assertIn("started", detail.inner_text().lower())

    def test_priority_3_columns_show_at_1440_without_expand(self):
        pg = self.open(self.page(1440, 900), "/runs")
        pg.wait_for_selector("#runs-body tr .chip")
        self.assertTrue(pg.locator("table.data th[data-pri='3']").first.is_visible())
        self.assertFalse(pg.locator("#runs-body .row-expand").first.is_visible())

    def test_tables_scroll_inside_a_container_with_sticky_head_and_first_column(self):
        pg = self.open(self.page(390, 844), "/leaderboard")
        wrap = pg.locator(".tbl-wrap").first
        self.assertEqual(wrap.evaluate("e => getComputedStyle(e).overflowX"), "auto")
        pos = pg.evaluate("""() => {
          const t = document.querySelector('.tbl-wrap table');
          return [getComputedStyle(t.querySelector('tr.thead-row > th')).position,
                  getComputedStyle(t.querySelector('tbody td:first-child, tr:not(.thead-row) > *:first-child')).position];
        }""")
        self.assertEqual(pos, ["sticky", "sticky"])

    def test_densities(self):
        probe = """() => {
          document.querySelector('#view').insertAdjacentHTML('beforeend',
            '<div class="tbl-wrap" id="d1"><table class="data"><tr><th>a</th></tr><tr><td>x</td></tr></table></div>' +
            '<div class="tbl-wrap" id="d2" data-density="compact"><table class="data"><tr><th>a</th></tr><tr><td>x</td></tr></table></div>');
          const h = id => document.querySelector(id + ' tr:last-child').getBoundingClientRect().height;
          return [h('#d1'), h('#d2')]; }"""
        pg = self.open(self.page(1440, 900), "/about")
        self.assertEqual(pg.evaluate(probe), [32, 28])
        pg = self.open(self.page(390, 844), "/about")
        self.assertEqual(pg.evaluate(probe)[0], 40)


class TestIcons(_Base):
    def test_every_use_in_every_route_resolves_and_paints(self):
        pg = self.page()
        for route in self.routes:
            self.open(pg, route)
            bad = pg.evaluate("""() => [...document.querySelectorAll('use')].filter(u => {
              const id = (u.getAttribute('href') || '').replace('#', '');
              const t = document.getElementById(id);
              if (!t || t.tagName.toLowerCase() !== 'symbol' && t.tagName.toLowerCase() !== 'pattern') return true;
              return false; }).map(u => u.getAttribute('href'))""")
            self.assertEqual(bad, [], route)
            self.assertGreaterEqual(pg.locator("#nav svg use").count(), len(NAV))

    def test_sprite_is_served_once_as_svg(self):
        pg = self.page()
        seen = []
        pg.on("response", lambda r: seen.append((r.url, r.headers.get("content-type", "")))
              if "icons.svg" in r.url else None)
        self.open(pg)
        self.assertEqual(len(seen), 1, seen)
        self.assertIn("image/svg+xml", seen[0][1])
        self.assertRegex(seen[0][0], r"\?v=[0-9a-f]+$")

    def test_icons_are_drawn_with_ink_and_hidden_from_assistive_tech(self):
        pg = self.open(self.page())
        self.assertEqual(pg.locator("#nav svg:not([aria-hidden='true'])").count(), 0)
        w = pg.evaluate("document.querySelector('#nav svg').getBoundingClientRect().width")
        self.assertEqual(w, 16)

    def test_hatch_resolves_ink_2_in_both_themes(self):
        seen = {}
        for theme in ("paper", "stage"):
            pg = self.open(self.page(theme=theme))
            seen[theme] = pg.evaluate("""() => {
              const ink = (() => { const d = document.createElement('i'); d.style.color = 'var(--ink-2)';
                document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; })();
              return [getComputedStyle(document.getElementById('hatch')).color, ink]; }""")
            self.assertEqual(seen[theme][0], seen[theme][1], theme)
        self.assertNotEqual(seen["paper"][0], seen["stage"][0], "hatch must follow the theme")


class TestLiveIndicator(_Base):
    def test_stalled_row_uses_the_stalled_glyph_and_says_unowned_in_words(self):
        pg = self.open(self.page())
        pg.wait_for_selector("#rail-jobs .rail-job")
        row = pg.locator(f"#rail-jobs .rail-job[data-run='{self.srv.manifest['orphan_run_id']}']")
        self.assertEqual(row.locator("svg use").first.get_attribute("href"), "#i-stalled")
        self.assertIn("Started from the CLI. Stop it there.", row.inner_text())

    def test_live_row_uses_the_baton_ring_and_animates(self):
        pg = self.open(self.page())
        pg.wait_for_selector("#rail-jobs .rail-job")
        ring = pg.locator("#rail-jobs .rail-job:not(.stalled) .live-ring").first
        if ring.count() == 0:
            self.skipTest("no live row in this corpus")
        self.assertEqual(ring.evaluate("e => getComputedStyle(e).animationName"), "baton")

    def test_reduced_motion_shows_no_animation_and_the_word_live(self):
        pg = self.page(reduced=True)
        self.open(pg)
        pg.wait_for_selector("#rail-jobs .rail-job")
        names = pg.evaluate("""() => [...document.querySelectorAll('#rail-jobs *, .live-ring')]
          .map(e => getComputedStyle(e).animationName).filter(n => n !== 'none')""")
        self.assertEqual(names, [])
        pg.evaluate("document.body.insertAdjacentHTML('beforeend', '<span id=probe class=live-ring></span>')")
        self.assertEqual(pg.evaluate("getComputedStyle(document.getElementById('probe')).animationName"), "none")
        self.assertEqual(pg.evaluate("getComputedStyle(document.getElementById('probe'), '::after').display"), "none")
        live = pg.locator("#rail-jobs .rail-job[data-state='live']")
        self.assertIn("live", live.first.inner_text().lower())

    def test_live_ring_is_baton_not_opacity_pulse(self):
        css = (bc.ROOT / "ui" / "app.css").read_text(encoding="utf-8")
        self.assertIn("@keyframes baton", css)
        self.assertNotIn("@keyframes pulse", css)
        self.assertIn("prefers-reduced-motion", css)


class TestStates(_Base):
    def test_unavailable_state_has_a_rest_and_one_action(self):
        meta = {"mode": "hosted", "synced_at": "2026-10-02T11:00:00+00:00", "source_commit": "abc",
                "capabilities": {"launch": False, "cancel": False, "flag_write": False,
                                 "thread": False, "png_capture": False, "live_stream": False}}
        pg = self.open(self.page(meta=meta), "/new")
        st = pg.locator("#not-available")
        self.assertEqual(st.locator("svg use").get_attribute("href"), "#r-missing")
        self.assertEqual(st.locator("a.btn, button").count(), 1)

    def test_unknown_route_is_a_state_not_bare_text(self):
        pg = self.open(self.page(), "/nope")
        self.assertEqual(pg.locator("#view .state svg use").first.get_attribute("href"), "#r-nomatch")

    def test_empty_corpus_runs_shows_the_empty_rest(self):
        srv = bc.shared("empty")
        pg = self.page()
        pg.goto(f"{srv.base}/#/runs")
        pg.wait_for_selector("#view[data-ready='ok']")
        pg.wait_for_selector("#runs-body .state")
        self.assertEqual(pg.locator("#runs-body .state svg use").first.get_attribute("href"), "#r-empty")


class TestCarryOvers(_Base):
    def test_group_cards_shorten_long_keys_and_keep_the_full_key_available(self):
        pg = self.open(self.page(390, 844))
        names = pg.locator("#band-groups td > a[title]")
        texts = names.all_inner_texts()
        self.assertTrue(texts)
        self.assertTrue(all(len(t) <= 36 for t in texts), texts)
        long_key = self.srv.manifest["long_group"]
        link = pg.locator(f"#band-groups td > a[href*='{long_key[:20]}']").first
        self.assertEqual(link.get_attribute("title"), long_key)

    def test_stat_lines_use_the_nil_mark_never_a_text_hyphen(self):
        pg = self.open(self.page(), "/")
        stats = pg.locator("#band-groups td.t-num").all_inner_texts()
        self.assertTrue(stats)
        for s in stats:
            self.assertNotRegex(s, r"(^|\s)-($|\s)")
        self.assertGreaterEqual(pg.locator("#band-groups td.t-num .nil").count(), 1)


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestCaptureMatrix(unittest.TestCase):
    def test_capture_script_writes_one_png_per_route_width_and_theme(self):
        import importlib.util
        import tempfile
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "capture_ui_matrix", bc.ROOT / "scripts" / "capture-ui-matrix.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as out:
            files = mod.capture(Path(out), routes=["/runs", "/about"], widths=[390])
            names = sorted(p.name for p in files)
        self.assertEqual(names, ["about-390-paper.png", "about-390-stage.png",
                                 "runs-390-paper.png", "runs-390-stage.png"])


if __name__ == "__main__":
    unittest.main()
