"""U10: Now and Experiments in a real browser, plus the rail-height fix.

Skipped without playwright. Zero network: a loopback server over the key-free
U23 corpus, with a few extra runs and one experiment matrix written into a
temp directory (never the repository)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import browser_corpus as bc

from orchestral.storage import RunMeta, RunStore
from orchestral.web.server import Observatory, make_handler

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

ORCH, WORKER = "corpus/orch-a", "corpus/worker-cheap"
TASK = "corpus-landing-page"
WATERMARK_KEY = "orchestral.now.seen"


class _Server:
    """The corpus plus U10 extras. `shape` is a corpus shape; `matrix` writes
    an experiment spec under tmp/experiments next to a copy of the tasks."""

    def __init__(self, shape: str = "full", matrix: bool = False) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            self.manifest = bc.corpus.build_corpus(self.root, shape)
        self.store = RunStore(self.root)
        self.tasks = self.tmp / "tasks"
        shutil.copytree(bc.FIXTURES / "tasks", self.tasks)
        if shape == "full":
            self._live_cli_run()
        if matrix:
            self._matrix()
        obs = Observatory(self.root, self.tasks, bc.FIXTURES / "models")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _live_cli_run(self) -> None:
        now = datetime.now(UTC)
        d = self.root / "cli-live0001"
        d.mkdir()
        (d / "events.jsonl").write_text(json.dumps({
            "type": "run.started", "timestamp": (now - timedelta(seconds=20)).isoformat()}) + "\n")
        self.store.index_meta(RunMeta(
            run_id="cli-live0001", orchestrator=ORCH, task_id=TASK, worker=WORKER, status="running",
            started_at=(now - timedelta(minutes=5)).isoformat(), run_dir=str(d)))

    def _matrix(self) -> None:
        exp = self.tmp / "experiments"
        exp.mkdir()
        (exp / "ab.yaml").write_text(
            f"name: ab\norchestrators: [{ORCH}]\nworkers: [{WORKER}]\n"
            f"tasks: [{TASK}, corpus-code-slugify]\n", encoding="utf-8")
        for arm, passes in (("baseline", (True, False, False)), ("jev", (True, True, True))):
            for i, ok in enumerate(passes):
                self.store.index_meta(RunMeta(
                    run_id=f"ab-{arm}-{i}", orchestrator=ORCH, task_id=TASK, worker=WORKER,
                    status="finished", started_at="2026-10-01T00:00:00+00:00",
                    finished_at="2026-10-01T00:01:00+00:00", passes=ok, total_cost_usd=0.01,
                    run_group=f"ab:{TASK}:{ORCH}:{WORKER}:{arm}", replicate=i,
                    config={"jev_assist": arm == "jev"}))

    def add_finished(self, run_id: str, *, passes: bool = True, minutes_ago: float = 1,
                     reason: str | None = None) -> None:
        done = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        self.store.index_meta(RunMeta(
            run_id=run_id, orchestrator=ORCH, task_id=TASK, worker=WORKER,
            status="finished" if passes or not reason else "failed",
            started_at=(done - timedelta(minutes=1)).isoformat(), finished_at=done.isoformat(),
            passes=passes, total_cost_usd=0.02, failure_reason=reason, run_group="u10-new"))

    def api(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read())

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def page(self, srv, width=1440, height=900, theme="paper"):
        ctx = self.browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errors: list[str] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.add_init_script(f"try{{localStorage.setItem('orchestral.theme','{theme}')}}catch(e){{}}")
        return pg

    def open(self, pg, srv, route="/"):
        pg.goto(f"{srv.base}/#{route}")
        pg.wait_for_selector("#view[data-ready]", timeout=30000)
        return pg


class TestNow(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = _Server("full", matrix=True)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        super().tearDownClass()

    def test_h1_says_now_and_there_are_three_bands(self):
        pg = self.open(self.page(self.srv), self.srv)
        self.assertEqual(pg.locator("h1").inner_text().strip(), "Now")
        self.assertEqual(pg.title().split(" · ")[0], "Now")
        for band in ("live", "changed", "look"):
            self.assertEqual(pg.locator(f"#band-{band}").count(), 1, band)
        self.assertEqual(self.errors, [])

    def test_live_band_lists_running_and_stalled_runs(self):
        pg = self.open(self.page(self.srv), self.srv)
        live = pg.locator("#band-live .live-lane")
        self.assertEqual(live.count(), 2)
        # the orphan is days past its last heartbeat: a lost ghost, still
        # abandonable, wearing the stalled glyph
        stalled = pg.locator(f"#band-live .live-lane[data-run='{self.srv.manifest['orphan_run_id']}']")
        self.assertEqual(stalled.get_attribute("data-state"), "lost")
        self.assertIn("lost", stalled.inner_text().lower())
        cli = pg.locator("#band-live .live-lane[data-run='cli-live0001']")
        self.assertEqual(cli.get_attribute("data-state"), "live")
        self.assertIn("Started from the CLI. Stop it there.", cli.inner_text())
        # unowned: never a Cancel, abandon only on the stalled one
        self.assertEqual(pg.locator("#band-live button.cancel").count(), 0)
        self.assertEqual(stalled.locator("button.abandon").count(), 1)

    def test_bands_fit_the_first_viewport_at_1440(self):
        pg = self.open(self.page(self.srv, 1440, 900), self.srv)
        bottom = pg.evaluate("document.getElementById('band-look').getBoundingClientRect().bottom")
        self.assertLessEqual(bottom, 900)
        # the item cap keeps a long list from pushing the heatmap away
        self.assertLessEqual(pg.locator("#band-look .look-item").count(), 5)

    def test_needs_a_look_names_each_kind_with_a_link(self):
        pg = self.open(self.page(self.srv), self.srv)
        items = pg.locator("#band-look .look-item")
        self.assertGreaterEqual(items.count(), 1)
        for i in range(items.count()):
            self.assertTrue(items.nth(i).locator("a").first.get_attribute("href").startswith("#/"))
        self.assertIn("more", pg.locator("#band-look").inner_text().lower())

    def test_first_visit_shows_last_24_hours_then_only_newer(self):
        self.srv.add_finished("u10-a-pass", passes=True, minutes_ago=30)
        self.srv.add_finished("u10-a-fail", passes=False, minutes_ago=20, reason="rate_limit")
        pg = self.page(self.srv)
        self.open(pg, self.srv)
        band = pg.locator("#band-changed")
        text = band.inner_text()
        self.assertIn("on this device", text)
        self.assertIn("last 24 hours", text)
        self.assertEqual(band.locator("[data-verdict='pass']").inner_text().split()[0], "1")
        self.assertEqual(band.locator("[data-verdict='fail']").inner_text().split()[0], "1")
        self.assertIn("rate-limited", band.inner_text().lower())
        # leaving Now commits the watermark; coming back shows nothing old
        pg.evaluate("location.hash = '#/runs'")
        pg.wait_for_selector("#view[data-ready]")
        stored = pg.evaluate(f"localStorage.getItem('{WATERMARK_KEY}')")
        self.assertTrue(stored)
        self.open(pg, self.srv, "/")
        self.assertEqual(pg.locator("#band-changed [data-verdict='pass']").count(), 0)
        self.assertIn("Nothing new", pg.locator("#band-changed").inner_text())
        # a run that finishes after the watermark shows up on the next visit
        self.srv.add_finished("u10-b-pass", passes=True, minutes_ago=0)
        pg.evaluate("location.hash = '#/runs'")
        pg.wait_for_selector("#view[data-ready]")
        self.open(pg, self.srv, "/")
        self.assertEqual(pg.locator("#band-changed [data-verdict='pass']").inner_text().split()[0], "1")

    def test_heatmap_cells_are_links_with_task_and_pairing_facets(self):
        pg = self.open(self.page(self.srv), self.srv)
        cell = pg.locator("table.hm a.hm-cell").first
        href = cell.get_attribute("href")
        self.assertIn("task=", href)
        self.assertIn("pairing=", href)
        cell.click()
        pg.wait_for_selector("#view[data-ready]")
        self.assertTrue(pg.url.split("#", 1)[1].startswith("/runs?"))
        q = pg.evaluate("Object.fromEntries(new URLSearchParams(location.hash.split('?')[1]))")
        self.assertTrue(q["task"].startswith("corpus-"))
        self.assertIn("corpus/", q["pairing"])

    def test_heatmap_cell_lands_on_runs_with_the_pairing_facet_and_its_rows(self):
        """Canonical URL form is `pairing=orch|worker` (URL-encoded), the same
        one Runs reads and writes: clicking a cell applies both facets."""
        pg = self.open(self.page(self.srv), self.srv)
        cell = pg.locator("table.hm a.hm-cell").first
        href = cell.get_attribute("href")
        self.assertIn("pairing=corpus%2F", href)
        self.assertIn("%7C", href)  # the pipe, encoded
        self.assertNotIn("%E2%86%92", href)  # never the arrow
        cell.click()
        pg.wait_for_selector("#view[data-ready]")
        pg.wait_for_selector("#runs-count")
        q = pg.evaluate("Object.fromEntries(new URLSearchParams(location.hash.split('?')[1]))")
        orch, worker = q["pairing"].split("|")
        self.assertEqual(pg.locator("#f-pairing").input_value(), q["pairing"])
        self.assertEqual(pg.locator("#f-task").input_value(), q["task"])
        self.assertEqual(pg.locator(".facet-chip.bad").count(), 0)
        want = [r for r in self.srv.api("/api/runs")
                if r["task_id"] == q["task"] and r["orchestrator"] == orch and r["worker"] == worker]
        self.assertTrue(want)
        pg.wait_for_function(
            f"document.getElementById('runs-count').dataset.total === '{len(want)}'")

    def test_low_n_cell_is_hatched_and_says_low_n_without_a_warning_color(self):
        pg = self.open(self.page(self.srv), self.srv)
        low = pg.locator("table.hm a.hm-thin").first
        self.assertIn("low n", low.inner_text().lower())
        bg = low.evaluate("e => getComputedStyle(e).backgroundImage")
        self.assertIn("gradient", bg)  # the hatch
        live_fill = pg.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--live-fill').trim()")
        color = low.evaluate("e => getComputedStyle(e).backgroundColor")
        self.assertNotEqual(self._rgb(pg, live_fill), color)

    @staticmethod
    def _rgb(pg, css):
        return pg.evaluate("c => { const d = document.createElement('i'); d.style.color = c;"
                           " document.body.appendChild(d); const v = getComputedStyle(d).color;"
                           " d.remove(); return v; }", css)

    def test_heatmap_text_meets_contrast_in_both_themes(self):
        from test_charts_browser import CONTRAST_JS
        for theme in ("paper", "stage"):
            pg = self.open(self.page(self.srv, theme=theme), self.srv)
            ratios = pg.evaluate(CONTRAST_JS)
            self.assertTrue(ratios)
            for cls, ratio in ratios:
                self.assertGreaterEqual(ratio, 4.5, f"{theme} {cls}")

    def test_heatmap_row_head_is_sticky_and_the_grid_scrolls_in_its_own_region(self):
        pg = self.open(self.page(self.srv, width=390, height=800), self.srv)
        self.assertEqual(pg.locator("#now-matrix .chart-scroll[role='region']").count(), 1)
        pos = pg.locator("#now-matrix tbody th.hm-h").first.evaluate("e => getComputedStyle(e).position")
        self.assertEqual(pos, "sticky")

    def test_never_attempted_cell_is_empty_with_a_name(self):
        pg = self.open(self.page(self.srv), self.srv)
        none = pg.locator("table.hm td.hm-none .hm-empty").first
        self.assertEqual(none.get_attribute("role"), "img")
        self.assertEqual(none.get_attribute("aria-label"), "never attempted")
        self.assertEqual(none.inner_text().strip(), "")

    def test_task_titles_are_not_uppercased(self):
        pg = self.open(self.page(self.srv), self.srv)
        th = pg.locator("table.hm tbody th.hm-h a").first
        self.assertEqual(th.evaluate("e => getComputedStyle(e.closest('th')).textTransform"), "none")
        text = th.inner_text()
        self.assertNotEqual(text, text.upper())

    def test_matrix_failure_is_an_inline_band_error_and_the_rest_render(self):
        pg = self.page(self.srv)
        pg.route("**/api/matrix", lambda r: r.fulfill(status=500, content_type="application/json",
                                                       body='{"error":"boom"}'))
        self.open(pg, self.srv)
        err = pg.locator("#band-heatmap [role='alert']")
        self.assertEqual(err.count(), 1)
        self.assertIn("could not load", err.inner_text().lower())
        self.assertEqual(pg.locator("#view[data-ready='ok']").count(), 1)
        self.assertEqual(pg.locator("#band-live").count(), 1)
        self.assertEqual(pg.locator("table.data").count() >= 1, True)  # groups still render

    def test_groups_table_uses_auto_labels(self):
        pg = self.open(self.page(self.srv), self.srv)
        txt = pg.locator("#band-groups").inner_text()
        self.assertIn("ab · corpus-landing-page · orch-a / worker-cheap · jev", txt)

    def test_experiment_link_summarises_the_ledger(self):
        pg = self.open(self.page(self.srv), self.srv)
        link = pg.locator("a.exp-summary")
        self.assertEqual(link.get_attribute("href"), "#/experiment?matrix=ab")
        self.assertIn("2 cells", link.inner_text())

    def test_the_experiment_ledger_no_longer_lives_on_now(self):
        pg = self.open(self.page(self.srv), self.srv)
        self.assertEqual(pg.locator("th:text('Diff CI')").count(), 0)


class TestNowEmpty(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = _Server("empty")

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        super().tearDownClass()

    def test_empty_corpus_shows_rest_with_new_dry_run_locally(self):
        pg = self.open(self.page(self.srv), self.srv)
        self.assertEqual(pg.locator("h1").inner_text().strip(), "Now")
        st = pg.locator(".state[data-state-kind='empty']")
        self.assertEqual(st.count(), 1)
        self.assertEqual(st.get_attribute("data-rest"), "r-empty")
        self.assertEqual(st.locator("use").get_attribute("href"), "#r-empty")
        self.assertEqual(st.locator("a.btn").inner_text().strip(), "New dry run")
        self.assertEqual(st.locator("a.btn").get_attribute("href"), "#/new")

    def test_empty_corpus_on_hosted_shows_a_cli_line(self):
        pg = self.page(self.srv)
        meta = {"mode": "hosted", "synced_at": None, "source_commit": None,
                "capabilities": dict.fromkeys(("launch", "cancel", "flag_write", "thread", "png_capture", "live_stream"), False)}
        pg.route("**/api/meta", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(meta)))
        self.open(pg, self.srv)
        st = pg.locator(".state[data-state-kind='empty']")
        self.assertEqual(st.count(), 1)
        self.assertEqual(st.locator("a.btn").count(), 0)
        self.assertIn("harness.py run --dry-run", st.locator("code.cmd").inner_text())


class TestExperiments(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = _Server("full", matrix=True)
        cls.bare = _Server("single")

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.bare.close()
        super().tearDownClass()

    def test_no_matrices_shows_the_empty_state(self):
        pg = self.open(self.page(self.bare), self.bare, "/experiment")
        self.assertEqual(pg.locator(".state[data-state-kind='empty']").count(), 1)
        self.assertIn("No experiment", pg.locator(".state-title").inner_text())

    def test_unknown_matrix_is_a_readable_404_state(self):
        pg = self.open(self.page(self.srv), self.srv, "/experiment?matrix=nope")
        st = pg.locator("#view [data-state-kind]")
        self.assertEqual(st.count(), 1)
        self.assertIn("nope", st.inner_text())
        self.assertEqual(pg.locator("#view[data-ready='ok']").count(), 1)
        self.assertEqual(pg.locator("h1").count(), 1)

    def test_ledger_groups_by_state_with_pending_collapsed(self):
        pg = self.open(self.page(self.srv), self.srv, "/experiment")
        self.assertEqual(pg.locator("h1").inner_text().strip(), "Experiments")
        pending = pg.locator("details.ledger-group[data-state='pending']")
        self.assertEqual(pending.count(), 1)
        self.assertFalse(pending.evaluate("e => e.open"))
        open_groups = pg.locator("details.ledger-group:not([data-state='pending'])")
        for i in range(open_groups.count()):
            self.assertTrue(open_groups.nth(i).evaluate("e => e.open"))

    def test_each_cell_has_an_interval_dumbbell_with_a_text_alternative(self):
        pg = self.open(self.page(self.srv), self.srv, "/experiment")
        svg = pg.locator("svg.ch-dumbbell").first
        self.assertEqual(svg.get_attribute("role"), "img")
        self.assertTrue(svg.locator("title").text_content())
        self.assertIn("Baseline", svg.locator("desc").text_content())
        self.assertGreaterEqual(svg.locator(".ch-pt").count(), 2)
        # the same numbers are in the table beside it
        self.assertIn("1/3", pg.locator("table.ledger").first.inner_text())

    def test_spend_gauge_shows_spend_and_says_no_budget_recorded(self):
        pg = self.open(self.page(self.srv), self.srv, "/experiment")
        gauge = pg.locator("#spend-gauge")
        self.assertIn("Spend", gauge.text_content())
        self.assertIn("$0.06", gauge.text_content())
        self.assertIn("No budget recorded", gauge.text_content())
        self.assertEqual(gauge.locator("meter, progress, .gauge-fill").count(), 0)

    def test_matrix_param_selects_the_ledger(self):
        pg = self.open(self.page(self.srv), self.srv, "/experiment?matrix=ab")
        self.assertIn("ab", pg.locator("#view").inner_text())
        self.assertEqual(pg.locator("table.ledger").count() >= 1, True)


class TestRailHeight(_Base):
    """The left rail must reach the bottom of a long page at desktop widths,
    stay a 56px icon rail at 1100px and below, and be hidden at 640px."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = bc.shared("full")

    def _long_page(self, width, height=800):
        pg = self.page(self.srv, width, height)
        pg.goto(f"{self.srv.base}/#/")
        pg.wait_for_selector("#view[data-ready='ok']")
        return pg

    def test_rail_covers_the_viewport_after_scrolling_a_long_page(self):
        pg = self._long_page(1440)
        total = pg.evaluate("document.documentElement.scrollHeight")
        self.assertGreater(total, 1000)  # the page is genuinely longer than the window
        pg.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        self.assertGreater(pg.evaluate("window.scrollY"), 500)
        box = pg.evaluate("""() => { const r = document.querySelector('#rail .rail-inner').getBoundingClientRect();
          return { top: r.top, bottom: r.bottom, vh: innerHeight } }""")
        self.assertLessEqual(box["top"], 1)
        self.assertGreaterEqual(box["bottom"], box["vh"] - 1)

    def test_rail_background_runs_the_full_page_height(self):
        pg = self._long_page(1440)
        r = pg.evaluate("""() => { const a = document.getElementById('rail').getBoundingClientRect();
          return { h: a.height, doc: document.documentElement.scrollHeight } }""")
        self.assertGreaterEqual(r["h"], r["doc"] - 1)

    def test_rail_inner_is_sticky_dvh_and_scrolls_itself(self):
        pg = self._long_page(1440)
        css = pg.evaluate("""() => { const s = getComputedStyle(document.querySelector('#rail .rail-inner'));
          return { pos: s.position, top: s.top, ov: s.overflowY, h: s.height, vh: innerHeight } }""")
        self.assertEqual(css["pos"], "sticky")
        self.assertEqual(css["top"], "0px")
        self.assertEqual(css["ov"], "auto")
        self.assertEqual(float(css["h"][:-2]), css["vh"])

    def test_icon_rail_at_1100_keeps_its_width_and_height(self):
        pg = self._long_page(1100)
        pg.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        box = pg.evaluate("""() => { const r = document.getElementById('rail').getBoundingClientRect();
          const i = document.querySelector('#rail .rail-inner').getBoundingClientRect();
          return { w: r.width, ibottom: i.bottom, vh: innerHeight, labels:
            getComputedStyle(document.querySelector('.nav-label')).display } }""")
        self.assertEqual(box["w"], 56)
        self.assertEqual(box["labels"], "none")
        self.assertGreaterEqual(box["ibottom"], box["vh"] - 1)

    def test_bottom_bar_at_640_hides_the_rail(self):
        pg = self._long_page(640, 844)
        self.assertEqual(pg.evaluate("getComputedStyle(document.getElementById('rail')).display"), "none")
        self.assertEqual(pg.evaluate("getComputedStyle(document.getElementById('tabbar')).display"), "grid")


if __name__ == "__main__":
    unittest.main()
