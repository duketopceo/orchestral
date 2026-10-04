"""Browser smoke for the web observatory — skipped without playwright.

Mirrors test_shots' optional-extra pattern: the test only runs when the
`[shots]`-grade playwright install is present; otherwise it skips clean.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.web import snapshot
from orchestral.web.server import UI_DIR, Observatory, make_handler

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
                self.assertIn("Now", pg.inner_text("h1"))
                self.assertIn("orchestral", pg.inner_text("body"))
                self.assertIn("t-task", pg.inner_text("body"))
                pg.goto(f"http://127.0.0.1:{self.port}/#/new")
                pg.wait_for_selector("button.primary")
                self.assertIn("Launch", pg.inner_text("body"))
            finally:
                browser.close()

    def test_cards_studio_filters_and_opens_universal_card(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page(viewport={"width": 390, "height": 844})
                pg.goto(f"http://127.0.0.1:{self.port}/#/cards")
                pg.wait_for_selector("#view[data-ready='ok']")
                self.assertIn("Publish", pg.inner_text("h1"))
                self.assertIn("preview the card", pg.inner_text("body").lower())
                self.assertGreaterEqual(pg.locator(".pub-row").count(), 1)
                self.assertEqual(pg.locator("body").evaluate("e => e.scrollWidth"), 390)

                pg.select_option("#cards-lens", "divergence")
                pg.wait_for_selector("#view[data-ready='ok']")
                self.assertIn("lens=divergence", pg.url)
                pg.locator(".pub-preview a.primary").click()
                pg.wait_for_selector(".xcard")
                self.assertIn("run group", pg.inner_text("body").lower())
                self.assertEqual(pg.locator(".xcard .pc-measure").count(), 3)
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
                pg.wait_for_selector("#view[data-ready='ok']")
                self.assertEqual(pg.locator("body").evaluate("e => e.scrollWidth"), 390)
                self.assertLessEqual(pg.locator(".pcard-fit").evaluate("e => e.scrollWidth"), 390)
                card = pg.locator(".xcard")
                self.assertLessEqual(card.evaluate("e => e.getBoundingClientRect().right"), 390)
            finally:
                browser.close()


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # aborted fetches are expected here
        pass


def _serve(handler) -> tuple[ThreadingHTTPServer, int]:
    httpd = _QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


HIDDEN = """
Object.defineProperty(Document.prototype, 'hidden', {get() { return true; }, configurable: true});
Object.defineProperty(Document.prototype, 'visibilityState', {get() { return 'hidden'; }, configurable: true});
"""
SHOW = """
Object.defineProperty(Document.prototype, 'hidden', {get() { return false; }, configurable: true});
Object.defineProperty(Document.prototype, 'visibilityState', {get() { return 'visible'; }, configurable: true});
document.dispatchEvent(new Event('visibilitychange'));
"""


class _Browser(unittest.TestCase):
    """Playwright lifecycle shared by the runtime suites."""

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def page(self, **kw):
        pg = self.browser.new_page(**kw)
        self.addCleanup(pg.close)
        self.errors: list[str] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        return pg


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestSpaRuntimeLocal(_Browser):
    """U6 against the U23 corpus on the live server."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        obs = Observatory(cls.tmp / "runs", FIXTURES / "tasks", FIXTURES / "models")
        cls.httpd, cls.port = _serve(make_handler(obs))
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def test_nested_module_imports_carry_the_asset_version(self):
        pg = self.page()
        urls: list[str] = []
        pg.on("request", lambda r: urls.append(r.url) if "/static/js/" in r.url else None)
        pg.goto(f"{self.base}/")
        pg.wait_for_selector("#view[data-ready='ok']")
        modules = [u for u in urls if u.split("?")[0].endswith(".js")]
        self.assertGreater(len(modules), 15, modules)
        self.assertEqual([u for u in modules if not re.search(r"\?v=[0-9a-f]+$", u)], [])
        self.assertEqual(self.errors, [])

    def test_every_route_settles_ok_without_script_errors(self):
        pg = self.page()
        run = self.manifest["failed_run_id"]
        for route in ("/", "/runs", f"/run/{run}", f"/run/{run}?tab=events", "/compare", "/leaderboard",
                      "/cards", "/card?kind=group&target=corpus-thin", "/models", "/new", "/about"):
            pg.goto(f"{self.base}/#{route}")
            pg.wait_for_selector("#view[data-ready]", timeout=20000)
            self.assertEqual(pg.get_attribute("#view", "data-ready"), "ok", route)
            self.assertEqual(self.errors, [], route)
        self.assertEqual(pg.evaluate("document.documentElement.dataset.mode"), "local")

    def test_hidden_tab_fires_no_poll_requests_and_resumes_on_return(self):
        pg = self.page()
        pg.add_init_script(HIDDEN)
        reqs: list[str] = []
        pg.on("request", lambda r: reqs.append(r.url) if "/api/" in r.url else None)
        pg.goto(f"{self.base}/#/run/{self.manifest['orphan_run_id']}?tab=events")
        pg.wait_for_selector("#view[data-ready='ok']")
        settled = len(reqs)
        pg.wait_for_timeout(7000)  # longer than every poll interval
        self.assertEqual(reqs[settled:], [], "a poll fired while the tab was hidden")
        pg.evaluate(SHOW)
        pg.wait_for_timeout(1500)
        self.assertGreater(len(reqs), settled, "polling did not resume when the tab became visible")

    def test_four_503s_show_error_with_retry_and_retry_recovers(self):
        pg = self.page()
        seen = {"n": 0}

        def flaky(route):
            seen["n"] += 1
            if seen["n"] <= 4:
                route.fulfill(status=503, content_type="application/json", body="{}")
            else:
                route.continue_()
        # Now degrades a failed matrix to an inline band error (U10), so the router's
        # retry path is exercised on a route whose only required read is this one.
        pg.route("**/api/groups*", flaky)
        pg.goto(f"{self.base}/#/leaderboard")
        pg.wait_for_selector("#status-line:not([hidden])", timeout=5000)
        self.assertIn("Reconnecting", pg.inner_text("#status-line"))
        pg.wait_for_selector("#view[data-ready='error']", timeout=20000)
        self.assertEqual(seen["n"], 4, "first try plus the 1s, 2s and 4s retries")
        self.assertIn("Retry", pg.inner_text("#view"))
        self.assertIn("Offline", pg.inner_text("#status-line"))
        pg.click("#retry")
        pg.wait_for_selector("#view[data-ready='ok']", timeout=10000)
        self.assertEqual(pg.locator("h1").count(), 1)
        self.assertTrue(pg.locator("#status-line").is_hidden())

    def test_client_errors_are_not_retried(self):
        pg = self.page()
        seen = {"n": 0}

        def gone(route):
            seen["n"] += 1
            route.fulfill(status=404, content_type="application/json", body='{"error": "no such run"}')
        pg.route("**/api/run/corpfail0001", gone)
        pg.goto(f"{self.base}/#/run/corpfail0001")
        pg.wait_for_selector("#view[data-ready='error']", timeout=5000)
        self.assertEqual(seen["n"], 1)
        self.assertIn("no such run", pg.inner_text("#view"))

    def test_two_quick_filter_changes_render_only_the_second_response(self):
        pg = self.page()
        pg.goto(f"{self.base}/#/runs")
        pg.wait_for_selector("#view[data-ready='ok']")
        with urllib.request.urlopen(f"{self.base}/api/runs?status=passed") as r:
            passed = len(json.load(r))
        with urllib.request.urlopen(f"{self.base}/api/runs?status=failed") as r:
            failed = len(json.load(r))
        self.assertNotEqual(passed, failed)

        def slow_first(route):
            if "status=failed" in route.request.url:
                time.sleep(1.5)
            route.continue_()
        pg.route("**/api/runs?*", slow_first)
        pg.select_option("#f-status", "failed")
        pg.select_option("#f-status", "passed")
        pg.wait_for_timeout(3500)
        body = pg.inner_text("#runs-body")
        self.assertEqual(int(pg.get_attribute("#runs-count", "data-total")), passed)
        self.assertNotIn("Failed", body)

    def test_finish_noticed_after_switching_to_events_tab(self):
        pg = self.page()
        runs = self.tmp / "runs"
        from orchestral.storage import RunStore
        store = RunStore(runs)
        rid = self.manifest["orphan_run_id"]
        orig = store.get_run(rid)
        assert orig is not None
        try:
            pg.goto(f"{self.base}/#/run/{rid}")
            pg.wait_for_selector("#view[data-ready='ok']")
            pg.click(".tabs button[data-tab='events']")
            pg.wait_for_selector(".ev-wrap")
            self.assertIn("Running", pg.inner_text(".run-stats"))
            store.update_meta(replace(orig, status="finished", finished_at="2026-09-30T08:05:00+00:00",
                                      passes=True, score=1.0))
            pg.wait_for_function(
                "!document.querySelector('.run-stats').innerText.includes('Running')", timeout=7000)
            self.assertIn("Pass", pg.inner_text(".run-stats"))
        finally:
            store.update_meta(orig)

    def test_poller_backs_off_on_failure_and_stops_on_demand(self):
        pg = self.page()
        pg.goto(f"{self.base}/#/about")
        pg.wait_for_selector("#view[data-ready='ok']")
        stamps = pg.evaluate("""async () => {
          const P = await import('/static/js/poller.js');
          const t = []; let n = 0;
          P.start('probe', async () => { t.push(performance.now()); n++; throw new Error('down'); }, {ms: 40});
          await new Promise(r => setTimeout(r, 900));
          P.stop('probe');
          const seen = t.length;
          await new Promise(r => setTimeout(r, 300));
          return {t, stoppedAt: seen, after: t.length};
        }""")
        gaps = [b - a for a, b in zip(stamps["t"], stamps["t"][1:], strict=False)]
        self.assertGreaterEqual(len(gaps), 3)
        self.assertGreater(gaps[2], gaps[0] * 1.8, gaps)
        self.assertEqual(stamps["after"], stamps["stoppedAt"], "stop() must end the task")

    def test_poller_task_stops_itself_with_STOP(self):
        pg = self.page()
        pg.goto(f"{self.base}/#/about")
        pg.wait_for_selector("#view[data-ready='ok']")
        calls = pg.evaluate("""async () => {
          const P = await import('/static/js/poller.js');
          let n = 0;
          P.start('once', async () => { n++; return P.STOP; }, {ms: 20});
          await new Promise(r => setTimeout(r, 300));
          return {n, active: P.active().includes('once')};
        }""")
        self.assertEqual(calls, {"n": 1, "active": False})


class _WorkerSim(BaseHTTPRequestHandler):
    """Worker-like mapping for the hosted simulation: /api/<name> serves the
    snapshot key <name>.json, query parameters ignored, /api/meta served too."""

    snap: ClassVar[dict] = {}

    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        raw = self.path.split("?")[0]
        if raw == "/":
            html = (UI_DIR / "app.html").read_text(encoding="utf-8").replace(
                'data-default-theme="system"', 'data-default-theme="paper"').replace("__V__", "t")
            return self._send(html.encode(), "text/html; charset=utf-8")
        if raw.startswith("/static/"):
            f = UI_DIR / raw[len("/static/"):]
            if f.is_file():
                ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}.get(
                    f.suffix.lstrip("."), "application/octet-stream")
                return self._send(f.read_bytes(), ctype)
        if raw.startswith("/api/"):
            key = raw[len("/api/"):] + ".json"
            if key in self.snap:
                return self._send(json.dumps(self.snap[key]).encode(), "application/json")
            return self._send(b'{"error": "not found"}', "application/json", 404)
        self._send(b"{}", "application/json", 404)

    def do_POST(self):
        self._send(b'{"error": "hosted observatory is read-only"}', "application/json", 501)


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestSpaRuntimeHosted(_Browser):
    """The hosted path: snapshot.py output behind a Worker-like mapping."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        from orchestral.storage import RunStore
        cls.store = RunStore(cls.tmp / "runs")
        cls.run_ids = [cls.manifest["failed_run_id"], cls.manifest["orphan_run_id"],
                       *[r.run_id for r in cls.store.list_runs(limit=6)]]
        cls.snap = snapshot.build_snapshot(
            cls.store, FIXTURES / "tasks", FIXTURES / "models", run_ids=cls.run_ids,
            synced_at="2026-10-02T11:00:00+00:00", source_commit="abc1234")
        sim = type("Sim", (_WorkerSim,), {"snap": cls.snap})
        cls.hosted, port = _serve(sim)
        cls.hbase = f"http://127.0.0.1:{port}"
        obs = Observatory(cls.tmp / "runs", FIXTURES / "tasks", FIXTURES / "models")
        cls.local, lport = _serve(make_handler(obs))
        cls.lbase = f"http://127.0.0.1:{lport}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.hosted, cls.local):
            s.shutdown()
            s.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def test_meta_mode_not_http_status_selects_the_adapter(self):
        pg = self.page()
        pg.goto(f"{self.hbase}/")
        pg.wait_for_selector("#view[data-ready='ok']")
        self.assertEqual(pg.evaluate("document.documentElement.dataset.mode"), "hosted")
        self.assertIn("Read-only snapshot", pg.inner_text("#status-line"))
        self.assertIn("Now", pg.inner_text("h1"))
        self.assertEqual(self.errors, [])

    def test_hosted_runs_pairing_filter_takes_pipe_and_legacy_arrow(self):
        rows = self.snap["runs.json"]
        orch, worker = rows[0]["orchestrator"], rows[0]["worker"]
        want = len([r for r in rows if r["orchestrator"] == orch and r["worker"] == worker])
        for form in (f"{orch}|{worker}", f"{orch} \u2192 {worker}"):
            pg = self.page()
            pg.goto(f"{self.hbase}/#/runs?pairing={urllib.parse.quote(form, safe='')}")
            pg.wait_for_selector("#view[data-ready='ok']")
            pg.wait_for_function(f"document.getElementById('runs-count').dataset.total === '{want}'")
            self.assertEqual(pg.locator("#f-pairing").input_value(), f"{orch}|{worker}")

    def test_hosted_ui_has_no_launch_flag_or_cancel_controls(self):
        pg = self.page()
        for route in ("/", "/runs", f"/run/{self.manifest['failed_run_id']}", "/leaderboard", "/cards",
                      "/card?kind=group&target=corpus-thin"):
            pg.goto(f"{self.hbase}/#{route}")
            pg.wait_for_selector("#view[data-ready='ok']", timeout=20000)
            self.assertEqual(pg.locator(".flag-btn, #launch, #cancel-btn, #btn-thread, .nav-cta").count(), 0, route)
        pg.goto(f"{self.hbase}/#/card?kind=group&target=corpus-thin")
        pg.wait_for_selector("#view[data-ready='ok']")
        self.assertNotIn("Download PNG", pg.inner_text("#view"))
        # the hosted rail is one static read: nothing moves, nothing can be cancelled
        pg.goto(f"{self.hbase}/")
        pg.wait_for_selector("#rail-jobs .rail-job")
        self.assertIn("Running at last sync", pg.inner_text("#rail-jobs"))
        self.assertEqual(pg.locator("#rail-jobs button").count(), 0)
        self.assertEqual(pg.locator("#rail-jobs .live-ring:not(.still)").count(), 0)

    def test_runs_filtered_by_status_failed_shows_only_failed_rows(self):
        expected = [r for r in self.snap["runs.json"]
                    if r["status"] == "failed" or (
                        r["status"] == "finished" and not r["passes"] and not r.get("holdout"))]
        self.assertTrue(expected)
        pg = self.page()
        pg.goto(f"{self.hbase}/#/runs?status=failed")
        pg.wait_for_selector("#view[data-ready='ok']")
        pg.wait_for_selector("#runs-body tr .chip")
        self.assertEqual(int(pg.get_attribute("#runs-count", "data-total")), len(expected))
        chips = pg.locator("#runs-body tr td:first-child .chip").all_inner_texts()
        self.assertTrue(chips and all(c in {"Failed", "Fail"} for c in chips), set(chips))

    def test_local_only_route_renders_the_read_only_state(self):
        pg = self.page()
        pg.goto(f"{self.hbase}/#/new")
        pg.wait_for_selector("#view[data-ready='ok']")
        self.assertIn("Not available on this read-only build", pg.inner_text("#view"))
        self.assertEqual(pg.get_attribute("#not-available", "data-rest"), "r-missing")
        self.assertEqual(pg.get_attribute("#not-available a", "href"), "#/")
        self.assertEqual(pg.locator("#launch").count(), 0)

    def test_stale_snapshot_escalates_the_status_line(self):
        pg = self.page()
        old = dict(self.snap["meta.json"], synced_at="2026-09-20T00:00:00+00:00")
        pg.route("**/api/meta", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(old)))
        pg.goto(f"{self.hbase}/")
        pg.wait_for_selector("#view[data-ready='ok']")
        self.assertEqual(pg.get_attribute("#status-line", "data-tone"), "warn")
        self.assertIn("days ago", pg.inner_text("#status-line"))

    def test_fetch_failure_on_hosted_offers_sign_in_again(self):
        pg = self.page()
        pg.goto(f"{self.hbase}/")
        pg.wait_for_selector("#view[data-ready='ok']")
        pg.route("**/api/runs", lambda r: r.abort())
        pg.goto(f"{self.hbase}/#/runs")
        pg.wait_for_selector("#view[data-ready='error']", timeout=20000)
        self.assertIn("Sign in again", pg.inner_text("#view"))
        self.assertIn("Sign in again", pg.inner_text("#status-line"))

    def test_meta_falls_back_to_meta_json_when_api_meta_is_missing(self):
        pg = self.page()
        seen: list[str] = []
        pg.on("request", lambda r: seen.append(r.url) if "/api/meta" in r.url else None)
        pg.route("**/api/meta", lambda r: r.fulfill(status=404, content_type="application/json", body="{}"))
        sim_snap = dict(self.snap)
        sim_snap["meta.json.json"] = sim_snap["meta.json"]
        pg.route("**/api/meta.json", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(self.snap["meta.json"])))
        pg.goto(f"{self.hbase}/")
        pg.wait_for_selector("#view[data-ready='ok']")
        self.assertEqual(pg.evaluate("document.documentElement.dataset.mode"), "hosted")
        self.assertTrue(any(u.endswith("/api/meta.json") for u in seen), seen)

    def _adapter_calls(self, pg):
        groups = [g["group"] for g in self.snap["groups.json"]]
        a, b = groups[0], groups[1]
        rid = self.manifest["failed_run_id"]
        return pg.evaluate("""async ([a, b, rid]) => {
          const D = await import('/static/js/data.js');
          await D.loadMeta();
          const d = D.data;
          return {
            groups: await d.groups(),
            matrix: await d.matrix(),
            runsAll: await d.runs({}),
            runsFailed: await d.runs({status: 'failed'}),
            runsGroup: await d.runs({group: a, q: 'api'}),
            run: (await d.run(rid)).meta,
            compare: await d.compare(a, b),
            pairings: await d.pairings(a),
            card: await d.card({kind: 'group', target: a, lens: 'overall'}),
            cards: (await d.cards({scope: 'group', lens: 'overall'})).cards.map(c => c.target),
            flags: await d.flags(),
          };
        }""", [a, b, rid])

    def test_adapter_contract_is_identical_on_both_backends(self):
        local_pg, hosted_pg = self.page(), self.page()
        local_pg.goto(f"{self.lbase}/#/about")
        hosted_pg.goto(f"{self.hbase}/#/about")
        for pg in (local_pg, hosted_pg):
            pg.wait_for_selector("#view[data-ready='ok']")
        local = self._adapter_calls(local_pg)
        hosted = self._adapter_calls(hosted_pg)
        self.assertEqual(local_pg.evaluate("document.documentElement.dataset.mode"), "local")
        self.assertEqual(hosted_pg.evaluate("document.documentElement.dataset.mode"), "hosted")
        # Holdout runs are the one deliberate difference: hosted never aggregates them and
        # masks their outcome on the runs list (tests/test_privacy_holdout_config.py).
        held = {r["run_id"] for r in local["runsAll"] if (r.get("config") or {}).get("holdout")}
        self.assertTrue(held)
        local["groups"] = [g for g in local["groups"] if g["group"] != "corpus-holdout"]
        local["cards"] = [c for c in local["cards"] if c != "corpus-holdout"]
        for rows in (local["runsAll"], local["runsFailed"], local["runsGroup"]):
            for r in rows:
                if r["run_id"] in held:
                    r.update(dict.fromkeys(("passes", "score", "judge_score", "judge_passed",
                                            "failure_reason")), holdout=True,
                             judge_state="not_judged", judge_reason="Withheld: holdout arm.")
        local["runsFailed"] = [r for r in local["runsFailed"]
                               if not (r["run_id"] in held and r["status"] == "finished")]
        n_held = len(held)
        cells = lambda m: sum(c["n"] for t in m["tasks"] for c in t["cells"].values())  # noqa: E731
        self.assertEqual(cells(local["matrix"]) - n_held, cells(hosted.pop("matrix")))
        local.pop("matrix")
        for name in local:
            self.assertEqual(local[name], hosted[name], name)
        self.assertGreater(len(local["runsAll"]), 1000)

    def test_snapshot_has_a_key_for_every_resource_the_hosted_adapter_requests(self):
        groups = [g["group"] for g in self.snap["groups.json"]]
        rid = self.manifest["failed_run_id"]
        pg = self.page()
        pg.goto(f"{self.hbase}/#/about")
        pg.wait_for_selector("#view[data-ready='ok']")
        names = pg.evaluate("""async ([a, b, rid]) => {
          const K = (await import('/static/js/data.js')).HOSTED_KEYS;
          return [K.overview(), K.runs(), K.groups(), K.matrix(), K.run(rid), K.runLive(rid), K.runEvidence(rid),
                  K.compare(a, b), K.leaderboard(), K.pairings(), K.pairings(a), K.cards('overall'),
                  K.card('group', a, 'overall'), K.card('pairing', 'x|y', 'low_cost'), K.flags(), K.modelsCatalog()];
        }""", [groups[0], groups[1], rid])
        missing = [n for n in names
                   if f"{n}.json" not in self.snap and not n.startswith("card/pairing/x")]
        self.assertEqual(missing, [])
        self.assertEqual(len(names), 16)


if __name__ == "__main__":
    unittest.main()
