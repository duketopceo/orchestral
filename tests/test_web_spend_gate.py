"""Observatory spend safety: dry-run default, confirm gates, estimates,
favicon, readable errors, and muted-text contrast.

Nothing here reaches a provider: paid paths are asserted to stop at the
confirm gate, and the one thread test that pretends a key is configured
patches the provider factory to fail loudly if it is ever called.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from orchestral import design_tokens
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.web import server, state
from orchestral.web.server import Observatory, make_handler

UI = Path(__file__).resolve().parents[1] / "ui"


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


def _write_specs(root: Path) -> tuple[Path, Path]:
    tasks, models = root / "tasks", root / "models"
    tasks.mkdir()
    models.mkdir()
    (tasks / "t-task.yaml").write_text("id: t-task\ntype: html\nprompt: p\n")
    (models / "m.yaml").write_text(
        "models:\n"
        "  - slug: o/model\n    name: o\n    role: orchestrator\n"
        "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
        "  - slug: w/model\n    name: w\n    role: worker\n"
        "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
        "  - slug: paid/writer\n    name: writer\n    role: reference\n"
        "    input_price_per_mtok: 1.00\n    output_price_per_mtok: 2.00\n"
    )
    return tasks, models


class _Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        Runner(dry_run=True, runs_dir=cls.tmp, store=RunStore(cls.tmp), run_group="g1").run(
            TaskSpec(id="t-task", type="html", prompt="p"),
            _model("o/model", "orchestrator"), _model("w/model", "worker"),
        )
        cls.tasks, cls.models = _write_specs(Path(cls.tmp))
        cls.obs = Observatory(Path(cls.tmp), cls.tasks, cls.models)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path: str) -> tuple[int, str, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type", ""), e.read().decode(errors="replace")

    def _post(self, path: str, fields: dict[str, str]) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode())


class TestLaunchConfirmGate(_Server):
    PAID: ClassVar[dict[str, str]] = {"task": "t-task", "orchestrator": "o/model", "worker": "w/model", "replicates": "2"}

    def test_paid_launch_without_confirm_is_refused_and_starts_nothing(self):
        before = len(self.obs.registry.jobs)
        code, body = self._post("/api/run", self.PAID)
        self.assertEqual(code, 409)
        self.assertTrue(body["needs_confirm"])
        self.assertIn("Confirm", body["error"])
        self.assertEqual(body["estimate"]["replicates"], 2)
        self.assertIsNone(body["estimate"]["total_usd"])  # no paid history: unknown, not $0
        self.assertEqual(body["estimate"]["basis"], "unknown")
        self.assertEqual(len(self.obs.registry.jobs), before)

    def test_legacy_form_route_is_gated_too(self):
        before = len(self.obs.registry.jobs)
        code, _ = self._post("/run", self.PAID)
        self.assertEqual(code, 409)
        self.assertEqual(len(self.obs.registry.jobs), before)

    def test_dry_run_needs_no_confirm(self):
        code, body = self._post("/api/run", {**self.PAID, "replicates": "1", "dry_run": "1"})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["run_id"])

    def test_confirm_spend_is_not_a_launch_field(self):
        # transport-only: TUI/web LAUNCH_FIELDS parity must not change
        self.assertNotIn("confirm_spend", state.LAUNCH_FIELDS)

    def test_launch_error_is_a_sentence(self):
        code, body = self._post("/api/run", {"task": "nope", "orchestrator": "o/model",
                                             "worker": "w/model", "dry_run": "1"})
        self.assertEqual(code, 400)
        self.assertTrue(body["error"].startswith("The run could not start:"), body)


class TestEstimates(_Server):
    def test_estimate_route_dry_and_paid(self):
        code, _, raw = self._get("/api/estimate?task=t-task&orchestrator=o/model&worker=w/model&dry_run=1")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["total_usd"], 0.0)
        code, _, raw = self._get("/api/estimate?task=t-task&orchestrator=o/model&worker=w/model&replicates=3")
        est = json.loads(raw)
        self.assertIsNone(est["total_usd"])
        self.assertEqual(est["rates"]["worker"]["output_per_mtok"], 0.10)
        self.assertTrue(est["caveat"])

    def test_launch_estimate_uses_history(self):
        store = SimpleNamespace(mean_cell_cost=lambda *a, **k: 0.05,
                                mean_run_cost=lambda **k: 9.0)
        est = state.launch_estimate(store, self.models, {
            "task": "t-task", "orchestrator": "o/model", "worker": "w/model", "replicates": "2"})
        self.assertAlmostEqual(est["total_usd"], 0.10)
        self.assertEqual(est["basis"], "task_pairing")
        store = SimpleNamespace(mean_cell_cost=lambda *a, **k: None,
                                mean_run_cost=lambda **k: 0.2)
        est = state.launch_estimate(store, self.models, {
            "task": "t-task", "orchestrator": "o/model", "worker": "w/model"})
        self.assertEqual(est["basis"], "pairing")
        self.assertAlmostEqual(est["total_usd"], 0.2)

    def test_thread_estimate(self):
        est = state.thread_estimate(self.models, "paid/writer", provider_ready=True)
        self.assertTrue(est["will_spend"])
        self.assertAlmostEqual(est["max_usd"], (4000 * 1.0 + 6000 * 2.0) / 1e6)
        self.assertFalse(state.thread_estimate(self.models, "paid/writer", provider_ready=False)["will_spend"])
        self.assertIsNone(state.thread_estimate(self.models, "x/unknown", provider_ready=True)["max_usd"])
        code, _, raw = self._get("/api/thread-estimate?model=paid/writer")
        self.assertEqual(code, 200)
        self.assertIn("will_spend", json.loads(raw))


class TestThreadConfirmGate(_Server):
    def test_paid_writer_requires_confirm_and_never_calls_provider(self):
        def boom(*a, **k):
            raise AssertionError("provider must not be constructed without confirm")
        with patch.object(server, "_provider_ready", return_value=True), \
                patch("orchestral.providers.provider_for", side_effect=boom):
            code, body = self._post("/api/thread", {"kind": "group", "target": "g1",
                                                    "model": "paid/writer", "n": "3"})
        self.assertEqual(code, 409)
        self.assertTrue(body["needs_confirm"])
        self.assertGreater(body["estimate"]["max_usd"], 0)

    def test_blank_writer_uses_templates(self):
        code, body = self._post("/api/thread", {"kind": "group", "target": "g1", "n": "3"})
        self.assertEqual(code, 200)
        self.assertTrue(body["templated"])


class TestFaviconAndErrors(_Server):
    def test_favicon(self):
        code, ctype, body = self._get("/static/favicon.svg")
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "image/svg+xml")
        self.assertIn("<svg", body)
        code, ctype, _ = self._get("/favicon.ico")
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "image/x-icon")
        _, _, html = self._get("/")
        self.assertIn('rel="icon"', html)

    def test_internal_error_is_readable(self):
        msg = server._internal_error(KeyError("task"))
        self.assertIn("unexpected error", msg)
        self.assertNotIn("Traceback", msg)
        self.assertNotIn("\n", server._internal_error(ValueError("a\nb")))


class TestUiDefaults(unittest.TestCase):
    """Static contract on the SPA source — the browser suite covers behavior."""

    js = (UI / "app.js").read_text()

    def test_new_run_defaults_to_dry_run(self):
        tag = re.search(r'<input type="checkbox" name="dry_run"[^>]*>', self.js)
        self.assertIsNotNone(tag)
        self.assertIn(" checked", tag.group(0))

    def test_thread_writer_is_not_prefilled(self):
        tag = re.search(r'<input id="thread-model"[^>]*>', self.js)
        self.assertIsNotNone(tag)
        self.assertIn('value=""', tag.group(0))
        self.assertNotIn("kimi", tag.group(0))

    def test_paid_actions_send_confirm_only_after_dialog(self):
        self.assertIn("function confirmSpend", self.js)
        # every confirm_spend=1 is set right after an awaited confirm
        self.assertEqual(self.js.count('body.set("confirm_spend", "1")'), 4)


def _lum(hex_color: str) -> float:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class TestMutedTextContrast(unittest.TestCase):
    """The full DESIGN.md 6.1 table lives in tests/test_design_tokens.py; this
    keeps the original regression (muted text readable) against the new
    token source in both themes."""

    def test_text_3_meets_aa_on_every_surface(self):
        toks = design_tokens.load(UI)
        for theme in ("paper", "stage"):
            for surface in ("canvas", "surface"):
                ratio = _contrast(toks[theme]["--ink-3"], toks[theme][f"--{surface}"])
                self.assertGreaterEqual(ratio, 4.5, f"{theme} --ink-3 on --{surface}: {ratio:.2f}")


try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestNewRunBrowser(_Server):
    def test_dry_run_default_and_cancelled_confirm_spends_nothing(self):
        before = len(self.obs.registry.jobs)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page()
                posts: list[str] = []
                pg.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
                pg.goto(f"http://127.0.0.1:{self.port}/#/new")
                pg.wait_for_selector("#launch-btn")
                self.assertTrue(pg.is_checked("input[name=dry_run]"))
                self.assertEqual(pg.inner_text("#launch-btn"), "Launch dry run")

                pg.uncheck("input[name=dry_run]")
                pg.wait_for_function(
                    "document.getElementById('spend-line').textContent.includes('Estimated cost')")
                self.assertIn("unknown", pg.inner_text("#spend-line"))
                self.assertEqual(pg.inner_text("#launch-btn"), "Launch paid run")

                pg.click("#launch-btn")
                pg.wait_for_selector("dialog.spend-dialog[open]")
                # Cancel holds focus: Enter alone must not spend
                self.assertEqual(pg.evaluate("document.activeElement.dataset.act"), "cancel")
                self.assertIn("Unknown", pg.inner_text("dialog.spend-dialog"))
                pg.keyboard.press("Escape")
                pg.wait_for_selector("dialog.spend-dialog", state="detached")
                self.assertEqual(posts, [])
            finally:
                browser.close()
        self.assertEqual(len(self.obs.registry.jobs), before)


if __name__ == "__main__":
    unittest.main()
