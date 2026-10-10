"""U12: run detail in a real browser, on the U23 corpus. Skipped without playwright.

Zero network: loopback server, key-free corpus. The corpus has no artifact, plan or
manifest files, so the test writes a few into its own private copy of the runs dir.
Set ORCH_U12_SHOTS to a directory to also capture screenshots."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import tempfile
import threading
import unittest
import zlib
from contextlib import closing
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestral.storage import RunMeta, RunStore
from orchestral.tui.state import Job, JobStatus
from orchestral.web import state
from orchestral.web.server import Observatory, make_handler
from tests.browser_corpus import FIXTURES, corpus

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

SHOT_DIR = os.environ.get("ORCH_U12_SHOTS")


def _png() -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    raw = b"\x00\xff\x00\x00" * 4
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw * 2)) + chunk(b"IEND", b""))


def _ev(seq: int, typ: str, ago_s: float, now: datetime, **kw) -> dict:
    return {"sequence": seq, "type": typ, "phase": kw.pop("phase", "plan"), "role": kw.pop("role", "harness"),
            "worker_id": None, "timestamp": (now - timedelta(seconds=ago_s)).isoformat(),
            "latency_ms": kw.pop("latency_ms", 0.0), "error": None, "input": {}, "output": {}, "cost": {},
            "model": "corpus/orch-a", **kw}


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed")
class TestRunDetailBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.root, "full")
        cls.store = RunStore(cls.root)
        cls.failed = cls.manifest["failed_run_id"]
        cls.orphan = cls.manifest["orphan_run_id"]
        cls.now = datetime.now(UTC)

        def pick(group: str, **want) -> RunMeta:
            for r in cls.store.list_runs(limit=None):
                if r.run_group == group and all(getattr(r, k) == v for k, v in want.items()):
                    return r
            raise AssertionError(group)

        # a passed run with an html artifact, a plan, a manifest with hashes
        cls.passed = pick("corpus-main:r0", status="finished", passes=True)
        d = Path(cls.passed.run_dir)
        (d / "artifact.html").write_text("<!doctype html><title>t</title><h1>corpus page</h1>")
        (d / "plan.json").write_text(json.dumps({"plan": "Build the page", "subtasks": [
            {"id": 1, "title": "Skeleton", "description": "HTML boilerplate", "acceptance_criteria": "valid HTML"},
            {"id": 2, "title": "Styles", "description": "CSS tokens"}]}))
        (d / "manifest.json").write_text(json.dumps({"run_id": cls.passed.run_id, "task_hash": "a" * 64,
                                                      "git_commit": "8664f13"}))
        # a second passed run with an image artifact
        cls.imaged = pick("corpus-main:r1", status="finished", passes=True)
        (Path(cls.imaged.run_dir) / "artifact.png").write_bytes(_png())
        # a dry run whose calls were never recorded
        cls.dry = pick("corpus-dry")
        with closing(sqlite3.connect(cls.root / "index.db")) as conn, conn:
            conn.execute("DELETE FROM calls WHERE run_id = ?", (cls.dry.run_id,))
        cls.hold = pick("corpus-holdout")
        # a failed run that died before its first event, with a report on disk
        cls._indexed("u12-noevents", "failed", failure="config", report={"errors": ["bad model"]})
        # a run that is alive right now, not owned (CLI-launched)
        cls._indexed("u12-live0001", "running", events=[_ev(1, "run.started", 20, cls.now, phase="init")])
        # a run this server owns, with a registered job
        cls._indexed("u12-owned001", "running", events=[_ev(1, "run.started", 5, cls.now, phase="init")])
        obs = Observatory(cls.root, FIXTURES / "tasks", FIXTURES / "models")
        cls.job = Job(label="owned", status=JobStatus.RUNNING)
        cls.job.run_ids = ["u12-owned001"]
        obs.registry.jobs.append(cls.job)
        cls.obs = obs
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def _indexed(cls, run_id: str, status: str, *, failure: str | None = None,
                 events: list[dict] | None = None, report: dict | None = None) -> None:
        d = cls.root / run_id
        d.mkdir()
        if events:
            (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        if report is not None:
            (d / "report.json").write_text(json.dumps(report))
        cls.store.index_meta(RunMeta(
            run_id=run_id, orchestrator="corpus/orch-a", task_id="corpus-landing-page",
            worker="corpus/worker-cheap", status=status, started_at=(cls.now - timedelta(minutes=5)).isoformat(),
            run_dir=str(d), failure_reason=failure))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def page(self, width: int = 1280, theme: str = "paper"):
        ctx = self.browser.new_context(viewport={"width": width, "height": 900})
        ctx.add_init_script(f"try{{localStorage.setItem('orchestral.theme','{theme}')}}catch(e){{}}")
        pg = ctx.new_page()
        self.errors: list[str] = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(ctx.close)
        return pg

    def open(self, run_id: str, tab: str | None = None, extra: str = "", pg=None):
        pg = pg or self.page()
        q = f"?tab={tab}" if tab else ""
        pg.goto(f"{self.base}/#/run/{run_id}{q}{extra}")
        pg.wait_for_selector("#view[data-ready='ok']", timeout=20000)
        return pg

    def tearDown(self):
        self.assertEqual(self.errors, [])

    # ---- failure summary ------------------------------------------------

    def test_failed_run_names_the_check_and_links_to_its_event(self):
        pg = self.open(self.failed)
        box = pg.locator(".rd-fail")
        self.assertIn("non_empty", box.inner_text())
        self.assertIn("worker_timeout", box.inner_text())
        link = box.locator("a.rd-fail-ev")
        self.assertIn("event=", link.get_attribute("href"))
        link.click()
        pg.wait_for_selector("#ev-wrap")
        self.assertIn("tab=events", pg.url)
        target = pg.locator(".ev-item.ev-target .ev-row")
        self.assertEqual(target.get_attribute("aria-expanded"), "true")
        self.assertIn("run.completed", target.inner_text())

    def test_failed_run_shows_billed_cost_beside_the_rate_card(self):
        pg = self.open(self.failed)
        stats = pg.inner_text(".run-stats")
        self.assertIn("$0.74", stats)
        self.assertIn("$0.11", pg.inner_text("[data-rate-card]"))

    def test_failed_run_with_no_events_says_so_and_links_the_report(self):
        pg = self.open("u12-noevents")
        box = pg.locator(".rd-fail")
        self.assertIn("before it recorded any event", box.inner_text())
        self.assertGreater(len(box.inner_text()), 40)
        box.get_by_text("Open the raw report").click()
        pg.wait_for_selector(".jv")
        self.assertIn("tab=report", pg.url)
        self.assertIn("bad model", pg.inner_text("#tab-body"))

    def test_passed_run_has_no_failure_block(self):
        pg = self.open(self.passed.run_id)
        self.assertEqual(pg.locator(".rd-fail").count(), 0)

    # ---- evidence states --------------------------------------------------

    def test_dry_run_calls_tab_is_empty_by_design(self):
        pg = self.open(self.dry.run_id, "calls")
        st = pg.locator("#tab-body [data-evidence='empty_by_design']")
        self.assertEqual(st.count(), 1)
        self.assertIn("dry run", st.inner_text().lower())

    def test_missing_and_not_yet_are_told_apart(self):
        pg = self.open(self.failed, "artifact")
        self.assertEqual(pg.locator("#tab-body [data-evidence='missing']").count(), 1)
        pg = self.open("u12-live0001", "report")
        self.assertEqual(pg.locator("#tab-body [data-evidence='not_yet']").count(), 1)

    def test_hosted_holdout_run_shows_the_withheld_copy(self):
        meta = {"mode": "hosted", "synced_at": "2026-10-03T00:00:00+00:00", "source_commit": "x",
                "capabilities": {"launch": False, "cancel": False, "flag_write": False,
                                 "thread": False, "png_capture": False, "live_stream": False}}
        payload = state.run_detail_payload(self.store, self.hold.run_id, FIXTURES / "tasks", hosted=True)
        pg = self.page()
        pg.route("**/api/meta", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(meta)))
        pg.route(f"**/api/run/{self.hold.run_id}",
                 lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(payload)))
        self.open(self.hold.run_id, "artifact", pg=pg)
        st = pg.locator("#tab-body [data-evidence='withheld']")
        self.assertEqual(st.count(), 1)
        self.assertIn("holdout", st.inner_text().lower())
        for tab in ("events", "calls", "report", "plan", "manifest"):
            pg.click(f".tabs button[data-tab='{tab}']")
            pg.wait_for_selector("#tab-body [data-evidence='withheld']")
        self.assertEqual(pg.locator("#cancel-btn, #abandon-btn").count(), 0)

    # ---- trees and lists ----------------------------------------------------

    def test_report_is_a_tree_with_a_check_list_and_copy_path(self):
        pg = self.open(self.passed.run_id, "report")
        self.assertGreaterEqual(pg.locator(".rd-checks li").count(), 1)
        self.assertGreaterEqual(pg.locator(".jv details.jv-node").count(), 1)
        btn = pg.locator(".jv .jv-copy").first
        self.assertTrue(btn.get_attribute("aria-label").startswith("Copy path report"))
        pg.fill(".jv-search", "zzz-no-such-key")
        self.assertFalse(pg.locator(".jv-none").is_hidden())
        pg.fill(".jv-search", "")
        self.assertTrue(pg.locator(".jv-none").is_hidden())

    def test_plan_is_a_subtask_list_with_a_json_toggle(self):
        pg = self.open(self.passed.run_id, "plan")
        items = pg.locator(".rd-subtasks li")
        self.assertEqual(items.count(), 2)
        self.assertIn("Skeleton", items.first.inner_text())
        pg.click("button[data-view='json']")
        self.assertEqual(pg.locator("#plan-body .jv").count(), 1)
        pg.click("button[data-view='list']")
        self.assertEqual(pg.locator(".rd-subtasks li").count(), 2)

    def test_manifest_hashes_have_copy(self):
        pg = self.open(self.passed.run_id, "manifest")
        row = pg.locator("table.data tbody tr", has_text="task_hash")
        self.assertEqual(row.locator("button[data-copy]").get_attribute("data-copy"), "a" * 64)
        self.assertGreaterEqual(pg.locator(".jv").count(), 1)

    def test_calls_show_billed_against_rate_card(self):
        pg = self.open(self.failed, "calls")
        head = pg.inner_text("table.calls thead").lower()
        self.assertIn("billed", head)
        self.assertIn("rate card", head)
        self.assertIn("billed by provider", pg.inner_text("table.calls tbody"))
        self.assertIn("$0.74", pg.inner_text("table.calls tfoot"))

    def test_event_rows_are_buttons_with_aria_expanded(self):
        pg = self.open(self.passed.run_id, "events")
        row = pg.locator(".ev-item .ev-row").first
        self.assertEqual(row.evaluate("e => e.tagName"), "BUTTON")
        self.assertEqual(row.get_attribute("aria-expanded"), "false")
        row.click()
        self.assertEqual(row.get_attribute("aria-expanded"), "true")
        self.assertTrue(pg.locator(".ev-detail").first.is_visible())

    # ---- artifact viewer ------------------------------------------------------

    def test_html_artifact_viewer_toolbar(self):
        pg = self.open(self.passed.run_id)
        self.assertEqual(pg.locator(".av-viewports button").count(), 4)
        self.assertIn("/artifact", pg.locator(".av-bar a").get_attribute("href"))
        pg.click(".av-viewports button[data-w='375']")
        self.assertEqual(pg.locator(".av-viewports button[data-w='375']").get_attribute("aria-pressed"), "true")
        width = pg.locator(".av-stage iframe").evaluate("e => e.getBoundingClientRect().width")
        self.assertAlmostEqual(width, 375, delta=2)
        self.assertIn(self.passed.run_id[:4], pg.locator(".av-copy").get_attribute("data-path") or "")

    def test_image_artifact_alt_is_the_task_title(self):
        pg = self.open(self.imaged.run_id)
        title = state.run_detail_payload(self.store, self.imaged.run_id, FIXTURES / "tasks")["task_title"]
        self.assertEqual(pg.locator(".av-stage img").get_attribute("alt"), title or self.imaged.task_id)

    # ---- tabs ---------------------------------------------------------------------

    def test_tabs_have_counts_and_arrow_keys_update_the_url(self):
        pg = self.open(self.passed.run_id)
        tabs = pg.locator('[role="tablist"] [role="tab"]')
        self.assertEqual(tabs.count(), 7)
        self.assertRegex(pg.inner_text("#tab-events"), r"Events\s+\d+")
        pg.focus("#tab-artifact")
        pg.keyboard.press("ArrowRight")
        pg.wait_for_function("location.hash.includes('tab=events')")
        pg.wait_for_selector("#ev-wrap")
        self.assertEqual(pg.evaluate("document.activeElement.id"), "tab-events")
        pg.keyboard.press("ArrowRight")
        pg.wait_for_function("location.hash.includes('tab=calls')")
        pg.keyboard.press("End")
        pg.wait_for_function("location.hash.includes('tab=manifest')")
        pg.wait_for_selector("#tab-manifest[aria-selected='true']")

    # ---- live polling and actions ---------------------------------------------------

    def test_unowned_live_run_polls_events_then_notices_the_finish(self):
        run = self.store.get_run("u12-live0001")
        assert run is not None
        d = Path(run.run_dir)
        pg = self.open("u12-live0001", "events")
        self.assertIn("Live", pg.inner_text("#rd-live"))
        self.assertIn("Started from the CLI. Stop it there.", pg.inner_text("#rd-live"))
        self.assertEqual(pg.locator("#cancel-btn, #abandon-btn").count(), 0)
        before = pg.locator(".ev-item").count()
        with (d / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(_ev(2, "llm_call", 0, datetime.now(UTC), phase="plan", role="orchestrator",
                                    latency_ms=900.0)) + "\n")
        pg.wait_for_function(f"document.querySelectorAll('.ev-item').length > {before}", timeout=8000)
        self.assertIn("llm_call", pg.inner_text("#ev-wrap"))
        self.assertEqual(pg.get_attribute("#ev-follow", "aria-pressed"), "true")
        self.store.update_meta(RunMeta(**{**run.__dict__, "status": "finished", "passes": True,
                                          "finished_at": datetime.now(UTC).isoformat()}))
        pg.wait_for_function("!document.querySelector('.run-stats').innerText.includes('Running')", timeout=9000)
        self.assertIn("Pass", pg.inner_text(".run-stats"))
        self.assertEqual(pg.locator("#rd-live").count(), 0)

    def test_owned_run_cancel_asks_first(self):
        posts: list[str] = []

        def record(route):
            posts.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body='{"cancelled": true}')

        pg = self.page()
        pg.route("**/api/run/u12-owned001/cancel", record)
        self.open("u12-owned001", pg=pg)
        self.assertEqual(pg.locator("#abandon-btn").count(), 0)
        pg.click("#cancel-btn")
        dlg = pg.locator("dialog.action-dialog[open]")
        self.assertEqual(dlg.count(), 1)
        # the safe button has initial focus, and Escape declines
        self.assertEqual(pg.evaluate("document.activeElement.dataset.act"), "keep")
        pg.keyboard.press("Escape")
        pg.wait_for_selector("dialog.action-dialog", state="detached")
        self.assertEqual(posts, [])
        self.assertEqual(pg.evaluate("document.activeElement.id"), "cancel-btn")
        pg.click("#cancel-btn")
        pg.click("dialog.action-dialog [data-act='confirm']")
        pg.wait_for_selector("dialog.action-dialog", state="detached")
        pg.wait_for_function("true")
        self.assertEqual(len(posts), 1)

    def test_owned_run_cancel_reaches_the_job(self):
        self.job.cancel_event.clear()
        pg = self.open("u12-owned001")
        pg.click("#cancel-btn")
        pg.click("dialog.action-dialog [data-act='confirm']")
        pg.wait_for_selector("dialog.action-dialog", state="detached")
        pg.wait_for_function("true")
        for _ in range(40):
            if self.job.cancel_event.is_set():
                break
            pg.wait_for_timeout(50)
        self.assertTrue(self.job.cancel_event.is_set())

    def test_stalled_orphan_offers_abandon_not_cancel_and_abandon_retires_it(self):
        self.addCleanup(self._clear_annotations)
        pg = self.open(self.orphan)
        # days past its last heartbeat: the orphan is a lost ghost, still abandonable
        self.assertIn("Lost", pg.inner_text("#rd-live"))
        self.assertEqual(pg.locator("#cancel-btn").count(), 0)
        pg.click("#abandon-btn")
        self.assertEqual(pg.locator("dialog.action-dialog[open]").count(), 1)
        pg.keyboard.press("Escape")
        pg.wait_for_selector("dialog.action-dialog", state="detached")
        self.assertEqual(self.store.annotations(), [])
        pg.click("#abandon-btn")
        pg.click("dialog.action-dialog [data-act='confirm']")
        pg.wait_for_function("document.querySelector('#rd-live')?.dataset.state === 'abandoned'", timeout=8000)
        self.assertEqual(pg.locator("#abandon-btn").count(), 0)
        self.assertEqual(self.store.get_run(self.orphan).status, "running")

    def _clear_annotations(self):
        with closing(sqlite3.connect(self.root / "index.db")) as conn, conn:
            conn.execute("DELETE FROM annotations")

    def test_read_only_capabilities_hide_both_actions(self):
        meta = {"mode": "local", "synced_at": None, "source_commit": None,
                "capabilities": {"launch": False, "cancel": False, "flag_write": False,
                                 "thread": False, "png_capture": False, "live_stream": True}}
        pg = self.page()
        pg.route("**/api/meta", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(meta)))
        for rid in ("u12-owned001", self.orphan):
            self.open(rid, pg=pg)
            self.assertEqual(pg.locator("#cancel-btn, #abandon-btn").count(), 0, rid)

    # ---- layout --------------------------------------------------------------------

    def test_header_is_one_column_and_390_does_not_scroll_sideways(self):
        pg = self.open(self.failed, pg=self.page(390))
        self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 390)
        pg = self.open(self.passed.run_id, pg=self.page(1440))
        h1 = pg.locator("h1").bounding_box()
        stats = pg.locator(".run-stats").bounding_box()
        self.assertGreater(stats["y"], h1["y"] + h1["height"] - 1)
        self.assertLess(abs(stats["x"] - h1["x"]), 4)

    def test_lane_timeline_lists_lanes_and_bars_link_to_events(self):
        pg = self.open(self.passed.run_id)
        rows = pg.locator(".ln-row")
        self.assertGreaterEqual(rows.count(), 2)
        bar = pg.locator(".ln-bar").first
        self.assertIn("event=", bar.get_attribute("href"))
        self.assertTrue(bar.get_attribute("aria-label"))

    @unittest.skipUnless(SHOT_DIR, "set ORCH_U12_SHOTS to capture screenshots")
    def test_capture_screenshots(self):
        out = Path(SHOT_DIR or "")
        out.mkdir(parents=True, exist_ok=True)
        runs = {"passed": self.passed.run_id, "failed": self.failed, "running": "u12-live0001",
                "stalled": self.orphan, "dry": self.dry.run_id, "noevents": "u12-noevents"}
        for width in (1440, 390):
            for theme in ("paper", "stage"):
                for name, rid in runs.items():
                    pg = self.open(rid, pg=self.page(width, theme))
                    pg.wait_for_timeout(300)
                    pg.screenshot(path=str(out / f"run-{name}-{width}-{theme}.png"), full_page=True)


if __name__ == "__main__":
    unittest.main()
