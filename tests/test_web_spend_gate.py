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
        for key in ("low_usd", "high_usd", "per_run_usd", "basis_label",
                    "month_to_date_billed_usd", "monthly_cap_usd"):
            self.assertIn(key, body["estimate"])
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
        calls: list[tuple] = []

        def estimate(task, orch, worker, *, exclude_task_id=None):
            calls.append((task, orch, worker))
            return SimpleNamespace(per_run_usd=0.05, low_usd=0.03, high_usd=0.08, n=4,
                                   basis="task_pairing")

        store = SimpleNamespace(
            billed_estimate=estimate,
            cost_calibration=lambda: SimpleNamespace(
                ratio_for=lambda m: SimpleNamespace(ratio=2.0, source="own", n=30)),
            month_to_date_billed_usd=lambda now=None: 1.5)
        est = state.launch_estimate(store, self.models, {
            "task": "t-task", "orchestrator": "o/model", "worker": "w/model", "replicates": "2"})
        self.assertAlmostEqual(est["total_usd"], 0.10)
        self.assertAlmostEqual(est["total_low_usd"], 0.06)
        self.assertAlmostEqual(est["total_high_usd"], 0.16)
        self.assertEqual(est["basis"], "task_pairing")
        self.assertEqual(est["month_to_date_billed_usd"], 1.5)
        self.assertEqual(calls, [("t-task", "o/model", "w/model")])

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


class TestIdempotency(_Server):
    PAID: ClassVar[dict[str, str]] = {"task": "t-task", "orchestrator": "o/model",
                                      "worker": "w/model", "confirm_spend": "1"}

    def _fake_launch(self):
        started: list[state.Job] = []

        def launch(spec):
            job = state.Job(label=f"job{len(started)}")
            job.run_ids.append(f"run{len(started)}")
            started.append(job)
            return job
        return started, launch

    def test_same_key_starts_one_job_and_both_return_its_id(self):
        started, launch = self._fake_launch()
        with patch.object(self.obs.registry, "launch", side_effect=launch):
            c1, b1 = self._post("/api/run", {**self.PAID, "idempotency_key": "k-same"})
            c2, b2 = self._post("/api/run", {**self.PAID, "idempotency_key": "k-same"})
        self.assertEqual((c1, c2), (200, 200))
        self.assertEqual(len(started), 1)
        self.assertEqual(b1["run_id"], b2["run_id"])

    def test_different_key_starts_a_second_job_only_after_confirm(self):
        started, launch = self._fake_launch()
        with patch.object(self.obs.registry, "launch", side_effect=launch):
            self._post("/api/run", {**self.PAID, "idempotency_key": "k-a"})
            unconfirmed = {k: v for k, v in self.PAID.items() if k != "confirm_spend"}
            code, body = self._post("/api/run", {**unconfirmed, "idempotency_key": "k-b"})
            self.assertEqual(code, 409)
            self.assertTrue(body["needs_confirm"])
            self.assertEqual(len(started), 1)
            code, body = self._post("/api/run", {**self.PAID, "idempotency_key": "k-b"})
        self.assertEqual(code, 200)
        self.assertEqual(len(started), 2)
        self.assertNotEqual(body["run_id"], "run0")

    def test_a_key_never_bypasses_the_confirm_gate(self):
        started, launch = self._fake_launch()
        unconfirmed = {k: v for k, v in self.PAID.items() if k != "confirm_spend"}
        with patch.object(self.obs.registry, "launch", side_effect=launch):
            self._post("/api/run", {**self.PAID, "idempotency_key": "k-gate"})
            code, _ = self._post("/api/run", {**unconfirmed, "idempotency_key": "k-gate"})
        self.assertEqual(code, 409)
        self.assertEqual(len(started), 1)

    def test_failed_launch_does_not_poison_the_key(self):
        started, launch = self._fake_launch()
        attempts = iter([ValueError("no such task")])

        def flaky(spec):
            exc = next(attempts, None)
            if exc:
                raise exc
            return launch(spec)

        with patch.object(self.obs.registry, "launch", side_effect=flaky):
            code, _ = self._post("/api/run", {**self.PAID, "idempotency_key": "k-flaky"})
            self.assertEqual(code, 400)
            code, _ = self._post("/api/run", {**self.PAID, "idempotency_key": "k-flaky"})
        self.assertEqual(code, 200)
        self.assertEqual(len(started), 1)

    def test_thread_draft_retried_with_same_key_is_one_paid_call(self):
        calls: list[int] = []

        def draft(**kw):
            calls.append(1)
            return {"posts": ["a", "b"], "templated": False, "model": "paid/writer"}

        fake_client = SimpleNamespace(close=lambda: None)
        form = {"kind": "group", "target": "g1", "model": "paid/writer", "n": "3",
                "confirm_spend": "1", "idempotency_key": "k-thread"}
        with patch.object(server, "_provider_ready", return_value=True), \
                patch("orchestral.providers.provider_for", return_value=fake_client), \
                patch("orchestral.judge.draft_thread", side_effect=draft):
            c1, b1 = self._post("/api/thread", form)
            c2, b2 = self._post("/api/thread", form)
        self.assertEqual((c1, c2), (200, 200))
        self.assertEqual(len(calls), 1)
        self.assertEqual(b1, b2)

    def test_cache_entries_expire_after_ten_minutes(self):
        now = [1000.0]
        cache = server.IdempotencyCache(clock=lambda: now[0])
        runs: list[int] = []
        first = cache.run("k", lambda: runs.append(1) or ("ok", 1))
        now[0] += server.IDEMPOTENCY_TTL_S - 1
        cache.run("k", lambda: runs.append(1) or ("ok", 2))
        self.assertEqual(len(runs), 1)
        now[0] += 2
        again = cache.run("k", lambda: runs.append(1) or ("ok", 3))
        self.assertEqual((first, again, len(runs)), (("ok", 1), ("ok", 3), 2))


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

    js = "\n".join(p.read_text() for p in sorted((UI / "js").rglob("*.js")))

    def test_no_unconditional_timers_remain(self):
        # Every live surface goes through ui/js/poller.js, which pauses while hidden.
        for path in (UI / "js").rglob("*.js"):
            text = path.read_text()
            self.assertNotIn("setInterval", text, path.name)
            if path.name not in {"poller.js", "router.js"}:
                self.assertNotIn("setTimeout", text, path.name)

    def test_import_map_versions_every_module(self):
        # Nested imports bust the cache through the map in app.html, so a module
        # missing from it would be served stale forever.
        html = (UI / "app.html").read_text()
        mapped = set(re.findall(r'"(/static/js/[^"]+\.js)": "\1\?v=__V__"', html))
        modules = {"/static/" + p.relative_to(UI).as_posix() for p in (UI / "js").rglob("*.js")}
        self.assertEqual(modules - mapped - {"/static/js/main.js"}, set())
        self.assertEqual(mapped - modules, set())

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

    def test_paid_posts_carry_an_idempotency_key_and_estimate_rows(self):
        self.assertIn("crypto.randomUUID", self.js)
        # the launch confirm routes both of its attempts through keyed()
        self.assertEqual(self.js.count("keyed();"), 2)
        self.assertEqual(self.js.count('body.set("idempotency_key"'), 3)
        for row in ("Range", "Spent this month"):
            self.assertIn(row, self.js)

    def test_spend_button_is_disabled_while_the_estimate_loads(self):
        # the price segment is disabled while "loading" (components/spend.js) and new.js enters that state
        self.assertIn('btn.disabled = !dry && status === "loading"', self.js)
        self.assertIn('setSpendButton(btn, { dry: false, status: "loading" });\n    line.textContent = "Paid run. Estimating cost', self.js)


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
                self.assertEqual(pg.inner_text("#launch-btn .spend-label"), "Launch dry run")

                pg.fill("#launch-task", "t-task")
                pg.keyboard.press("ArrowDown")
                pg.keyboard.press("Enter")
                pg.uncheck("input[name=dry_run]")
                pg.wait_for_function(
                    "document.getElementById('spend-line').textContent.includes('Estimated cost')")
                self.assertIn("unknown", pg.inner_text("#spend-line"))
                self.assertEqual(pg.inner_text("#launch-btn .spend-label"), "Launch paid run")

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
