"""The Score, U15: Publish surface contracts that need no browser.

Alt text is generated from data (deterministic, no judge reasoning, no
descriptions, no model-authored text), holdout runs never reach a card, a
capture or the OG image, and the capture pipeline fails loudly instead of
shipping error-page bytes. Nothing here reaches a provider."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestral import shots
from orchestral.shots import CaptureError, ScreenshotUnavailable, capture_page, optimize_png, shot_name
from orchestral.storage import RunStore
from orchestral.web import snapshot, state
from orchestral.web.server import UI_DIR, Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus)

CANARY = "CANARY-U15-9c41d7"


class _Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        cls.store = RunStore(cls.tmp / "runs")
        cls.tasks = FIXTURES / "tasks"
        cls.groups = FIXTURES / "groups.yaml"  # absent: groups carry no labels
        runs = cls.store.list_runs(limit=None)
        cls.held = sorted(r.run_id for r in runs if (r.config or {}).get("holdout"))
        cls.held_groups = {r.run_group for r in runs if (r.config or {}).get("holdout")}
        assert cls.held and cls.held_groups, "fixture corpus lost its holdout arm"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestAltText(_Corpus):
    def _catalog(self, lens="overall"):
        return state.card_catalog_payload(
            snapshot._PublishedStore(self.store), tasks_dir=self.tasks,
            groups_file=self.groups, lens=lens)["cards"]

    def test_every_card_ships_alt_text_in_its_story(self):
        cards = self._catalog()
        self.assertTrue(cards)
        for card in cards:
            alt = card["story"]["alt"]
            self.assertTrue(alt.strip(), card["target"])
            self.assertLessEqual(len(alt), 1000, card["target"])

    def test_alt_text_is_deterministic(self):
        first = [c["story"]["alt"] for c in self._catalog()]
        second = [c["story"]["alt"] for c in self._catalog()]
        self.assertEqual(first, second)

    def test_pairing_alt_names_the_pairing_and_the_numbers(self):
        card = next(c for c in self._catalog()
                    if c["kind"] == "pairing" and c["finished"] >= 100)
        alt = card["story"]["alt"]
        self.assertTrue(alt.startswith("Pairing card."), alt)
        self.assertIn(card["orchestrator"], alt)
        self.assertIn(card["worker"], alt)
        self.assertIn(f"{card['passed']} of {card['finished']}", alt)
        lo, hi = card["pass_ci"]
        self.assertRegex(alt, rf"{int(lo * 100 + 0.5)} to {int(hi * 100 + 0.5)} percent")
        self.assertNotIn("not ranked", alt)

    def test_low_sample_subject_is_not_ranked(self):
        thin = [c for c in self._catalog()
                if c["story"]["confidence"]["mechanical"]["level"] == "low" or c["finished"] < 10]
        self.assertTrue(thin)
        for card in thin:
            self.assertIn("not ranked, thin sample", card["story"]["alt"], card["target"])

    def test_alt_text_carries_no_reasoning_description_or_model_text(self):
        runs = self.store.list_runs(limit=None)
        for meta in runs:
            run_dir = Path(meta.run_dir)
            report = run_dir / "report.json"
            body = json.loads(report.read_text()) if report.exists() else {}
            body.setdefault("judge", {})["reasoning"] = CANARY
            body["judge"]["summary"] = CANARY
            report.write_text(json.dumps(body))
            (run_dir / "artifact.html").write_text(f"<p>{CANARY}</p>")
        groups = self.tmp / "groups-canary.yaml"
        names = sorted({m.run_group for m in runs if m.run_group})
        groups.write_text("groups:\n" + "".join(
            f"  {json.dumps(n)}:\n    label: Label {n}\n    description: {CANARY}\n" for n in names))
        cards = state.card_catalog_payload(
            snapshot._PublishedStore(self.store), tasks_dir=self.tasks,
            groups_file=groups)["cards"]
        self.assertTrue(cards)
        for card in cards:
            self.assertNotIn(CANARY, card["story"]["alt"], card["target"])

    def test_run_card_alt_states_the_outcome(self):
        run = next(r for r in self.store.list_runs(limit=None)
                   if r.status == "finished" and r.run_id not in self.held)
        card = state.card_payload(
            snapshot._PublishedStore(self.store), "run", run.run_id,
            tasks_dir=self.tasks, groups_file=self.groups)
        assert card is not None
        alt = card["story"]["alt"]
        self.assertTrue(alt.startswith("Run card."), alt)
        self.assertRegex(alt, r"Mechanical (pass|fail)")

    def test_alt_text_follows_the_copy_rules(self):
        spec = importlib.util.spec_from_file_location(
            "copy_rules", ROOT / "tests" / "test_copy_rules.py")
        assert spec and spec.loader
        rules = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rules)
        lint_text = rules.lint_text
        for card in self._catalog():
            self.assertEqual(lint_text(card["story"]["alt"]), [], card["target"])


class TestHoldoutNeverPublished(_Corpus):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        obs = Observatory(cls.tmp / "runs", cls.tasks, FIXTURES / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def _get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_publish_catalog_lists_no_holdout_group(self):
        code, body = self._get("/api/cards")
        self.assertEqual(code, 200)
        cards = json.loads(body)["cards"]
        self.assertTrue(cards)
        self.assertFalse({c["target"] for c in cards} & self.held_groups)
        for held in self.held:
            self.assertNotIn(held, body)

    def test_holdout_group_card_is_not_found(self):
        for group in self.held_groups:
            code, _ = self._get(f"/api/card?kind=group&target={urllib.parse.quote(group)}")
            self.assertEqual(code, 404, group)

    def test_holdout_run_card_is_not_found(self):
        for run_id in self.held:
            code, body = self._get(f"/api/card?kind=run&target={run_id}")
            self.assertEqual(code, 404, run_id)
            self.assertIn("withheld", body)

    def test_pairing_card_counts_only_published_runs(self):
        runs = self.store.list_runs(limit=None)
        pair = ("corpus/orch-b", "corpus/worker-cheap")
        open_runs = [r for r in runs if (r.orchestrator, r.worker) == pair
                     and r.run_id not in self.held]
        held_runs = [r for r in runs if (r.orchestrator, r.worker) == pair
                     and r.run_id in self.held]
        self.assertTrue(held_runs, "fixture lost the holdout runs on this pairing")
        code, body = self._get(f"/api/card?kind=pairing&target={urllib.parse.quote('|'.join(pair))}")
        if open_runs:
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["runs"], len(open_runs))
        else:
            self.assertEqual(code, 404)

    def test_capture_of_a_holdout_run_is_refused_before_a_browser_starts(self):
        with patch("orchestral.shots.capture_page") as cap:
            for run_id in self.held:
                code, body = self._get(
                    "/api/shot.png?route=" + urllib.parse.quote(f"/card?kind=run&target={run_id}"))
                self.assertEqual(code, 404, run_id)
                self.assertIn("withheld", body)
            for group in self.held_groups:
                code, _ = self._get(
                    "/api/shot.png?route=" + urllib.parse.quote(f"/card?kind=group&target={group}"))
                self.assertEqual(code, 404, group)
        cap.assert_not_called()

    def test_batch_export_never_targets_a_holdout_card(self):
        import harness
        seen: list[str] = []

        def fake_capture(url, **kw):
            seen.append(urllib.parse.unquote(url))
            return b"\x89PNG-fake"

        out = self.tmp / "reports"
        args = type("A", (), {
            "runs_dir": str(self.tmp / "runs"), "tasks_dir": str(self.tasks),
            "models_dir": str(FIXTURES / "models"), "reports_dir": str(out),
            "group": None, "og": False})()
        @contextmanager
        def _session():
            yield MagicMock()

        with patch("orchestral.shots.capture_page", side_effect=fake_capture), \
                patch("orchestral.shots.browser_session", _session):
            harness.cmd_cards(args)
        self.assertTrue(seen)
        blob = "\n".join(seen)
        for group in self.held_groups:
            self.assertNotIn(group, blob)
        for run_id in self.held:
            self.assertNotIn(run_id, blob)

    def test_hosted_snapshot_cards_carry_alt_and_no_holdout(self):
        snap = snapshot.build_snapshot(self.store, self.tasks, FIXTURES / "models", self.groups,
                                       run_ids=[])
        card_keys = [k for k in snap if k.startswith(("card/", "cards."))]
        self.assertTrue(card_keys)
        blob = json.dumps({k: snap[k] for k in card_keys})
        for group in self.held_groups:
            self.assertNotIn(group, blob)
        for key in card_keys:
            if key.startswith("card/"):
                self.assertTrue(snap[key]["story"]["alt"], key)

    def test_capture_server_reads_the_published_view(self):
        from orchestral.web.server import published_observatory
        pub = published_observatory(Observatory(self.tmp / "runs", self.tasks, FIXTURES / "models"))
        listed = {m.run_id for m in pub.store.list_runs(limit=None)}
        self.assertTrue(listed)
        self.assertFalse(listed & set(self.held))

    def test_og_template_is_static_and_names_no_run_data(self):
        html = (UI_DIR / "og" / "default.html").read_text()
        for needle in (*self.held, *self.held_groups, "corpus"):
            self.assertNotIn(needle, html)
        self.assertNotRegex(html, r"""(?:src|href)=["']https?://""")
        self.assertNotRegex(html, r"""@import|url\(\s*['"]?https?://""")


class TestShotName(unittest.TestCase):
    def test_capture_flag_never_changes_the_download_name(self):
        base = "/card?kind=pairing&target=o%2Fm%7Cw%2Fm&group=g&lens=cheapest"
        self.assertEqual(shot_name(base, stamp="20261004"),
                         shot_name(base + "&capture=1", stamp="20261004"))
        self.assertEqual(shot_name("/leaderboard?sort=cost", stamp="x"),
                         shot_name("/leaderboard?sort=cost&capture=1", stamp="x"))


def _fake_browser(ready: str = "ok"):
    page = MagicMock()
    page.get_attribute.return_value = ready
    page.locator.return_value.first.screenshot.return_value = b"png-bytes"
    browser = MagicMock()
    browser.new_page.return_value = page
    return browser, page


class TestCapturePipeline(unittest.TestCase):
    def test_error_view_is_a_capture_error_never_png_bytes(self):
        browser, page = _fake_browser("error")
        with self.assertRaises(CaptureError) as cm:
            capture_page("http://x/#/card?kind=run&target=x", element=".xcard", browser=browser)
        self.assertEqual(cm.exception.code, "view_error")
        page.locator.return_value.first.screenshot.assert_not_called()
        page.close.assert_called_once()

    def test_ready_view_is_captured(self):
        browser, _ = _fake_browser("ok")
        self.assertEqual(capture_page("http://x/#/card", element=".xcard", browser=browser),
                         b"png-bytes")

    def test_card_capture_uses_a_fixed_viewport_and_2x(self):
        browser, _ = _fake_browser()
        capture_page("http://x/#/card", element=".xcard", width=1200, height=675,
                     scale=2, browser=browser)
        kw = browser.new_page.call_args.kwargs
        self.assertEqual((kw["viewport"], kw["device_scale_factor"]),
                         ({"width": 1200, "height": 675}, 2))
        self.assertEqual(kw["color_scheme"], "light")
        self.assertEqual(kw["reduced_motion"], "reduce")

    def test_timeout_is_a_timeout_capture_error(self):
        class TimeoutError_(Exception):
            pass
        TimeoutError_.__name__ = "TimeoutError"
        browser, page = _fake_browser()
        page.wait_for_selector.side_effect = TimeoutError_("Timeout 20000ms exceeded")
        with self.assertRaises(CaptureError) as cm:
            capture_page("http://x/#/card", element=".xcard", browser=browser)
        self.assertEqual(cm.exception.code, "timeout")

    def test_missing_playwright_reports_its_code(self):
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}), \
                self.assertRaises(ScreenshotUnavailable) as cm:
            capture_page("http://x/")
        self.assertEqual(cm.exception.code, "playwright_missing")

    def test_missing_chromium_reports_its_code(self):
        fake = MagicMock()
        fake.return_value.__enter__.return_value.chromium.launch.side_effect = RuntimeError(
            "BrowserType.launch: Executable doesn't exist at /x/chrome")
        with patch("orchestral.shots._import_playwright", return_value=fake), \
                self.assertRaises(ScreenshotUnavailable) as cm:
            capture_page("http://x/")
        self.assertEqual(cm.exception.code, "chromium_missing")

    def test_optimizer_absent_returns_the_unoptimized_png(self):
        with patch("shutil.which", return_value=None):
            self.assertEqual(optimize_png(b"\x89PNG-raw"), b"\x89PNG-raw")

    def test_optimizer_failure_returns_the_unoptimized_png(self):
        with patch("shutil.which", return_value="/usr/bin/oxipng"), \
                patch("subprocess.run", side_effect=OSError("boom")):
            self.assertEqual(optimize_png(b"\x89PNG-raw"), b"\x89PNG-raw")

    def test_optimizer_output_is_used_when_it_is_smaller(self):
        def fake_run(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"small")
            return MagicMock(returncode=0)
        with patch("shutil.which", return_value="/usr/bin/oxipng"), \
                patch("subprocess.run", side_effect=fake_run):
            self.assertEqual(optimize_png(b"a-much-longer-png-body"), b"small")


class TestShotEndpointErrors(_Corpus):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        obs = Observatory(cls.tmp / "runs", cls.tasks, FIXTURES / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def _get(self, route: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/api/shot.png?route={urllib.parse.quote(route)}") as r:
                return r.status, {"png": r.read()}
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _group_route(self) -> str:
        group = next(g["group"] for g in state.groups_payload(
            snapshot._PublishedStore(self.store), self.groups)
            if g["group"] not in self.held_groups)
        return f"/card?kind=group&target={urllib.parse.quote(group)}"

    def test_playwright_missing_is_a_readable_503_with_the_install_command(self):
        with patch("orchestral.shots.capture_page", side_effect=ScreenshotUnavailable(
                "playwright is not installed", code="playwright_missing")):
            code, body = self._get(self._group_route())
        self.assertEqual(code, 503)
        self.assertEqual(body["code"], "playwright_missing")
        self.assertIn("pip install", body["install"])
        self.assertIn("playwright install chromium", body["install"])

    def test_chromium_missing_names_the_browser_install(self):
        with patch("orchestral.shots.capture_page", side_effect=ScreenshotUnavailable(
                "no chromium", code="chromium_missing")):
            code, body = self._get(self._group_route())
        self.assertEqual((code, body["code"]), (503, "chromium_missing"))
        self.assertIn("playwright install chromium", body["install"])

    def test_timeout_is_a_504(self):
        with patch("orchestral.shots.capture_page", side_effect=CaptureError(
                "took too long", code="timeout")):
            code, body = self._get(self._group_route())
        self.assertEqual((code, body["code"]), (504, "timeout"))

    def test_error_view_is_a_502_not_an_image(self):
        with patch("orchestral.shots.capture_page", side_effect=CaptureError(
                "the view reported an error", code="view_error")):
            code, body = self._get(self._group_route())
        self.assertEqual((code, body["code"]), (502, "view_error"))

    def test_card_capture_request_is_fixed_size_and_flagged(self):
        route = self._group_route()
        with patch("orchestral.shots.capture_page", return_value=b"\x89PNG-fake") as cap:
            code, body = self._get(route)
        self.assertEqual(code, 200)
        self.assertEqual(body["png"], b"\x89PNG-fake")
        url = cap.call_args.args[0]
        self.assertIn("capture=1", url)
        kw = cap.call_args.kwargs
        self.assertEqual((kw["width"], kw["height"], kw["scale"], kw["element"]),
                         (1200, 675, 2, ".xcard"))


class TestOgMeta(unittest.TestCase):
    def setUp(self):
        self.html = (UI_DIR / "app.html").read_text()

    def test_shell_has_open_graph_and_twitter_tags(self):
        for needle in (
            'property="og:title"', 'property="og:description"', 'property="og:type"',
            'property="og:image"', 'property="og:image:width" content="1200"',
            'property="og:image:height" content="630"', 'property="og:image:alt"',
            'name="twitter:card" content="summary_large_image"',
            'name="twitter:image"', 'name="twitter:image:alt"',
        ):
            self.assertIn(needle, self.html)
        self.assertRegex(self.html, r'og:image" content="https://[^"]+/static/og/og-default\.png"')

    def test_og_image_exists_and_is_1200x630_under_150kb(self):
        import struct
        png = UI_DIR / "og" / "og-default.png"
        data = png.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (1200, 630))
        self.assertLessEqual(len(data), 150 * 1024)

    def test_og_render_goes_through_shots(self):
        self.assertTrue(hasattr(shots, "render_og"))
