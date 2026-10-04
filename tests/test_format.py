"""U5: one formatter contract (KTD3) for Python and the SPA, one shared fixture."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestral import format as fmt
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.tui import state as tui_state
from orchestral.web.server import Observatory, make_handler

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "tests" / "fixtures" / "format_cases.json").read_text(encoding="utf-8"))

PY_FN = {
    "money": fmt.fmt_money,
    "percent": fmt.fmt_percent,
    "score": fmt.fmt_score,
    "duration": fmt.fmt_duration_ms,
    "tokens": fmt.fmt_tokens,
    "delta": fmt.fmt_delta,
    "rangePct": fmt.fmt_range_pct,
    "shortSlug": fmt.short_slug,
    "lowNCell": fmt.is_low_n_cell,
    "lowNBest": fmt.is_low_n_best,
}


class TestFormatCases(unittest.TestCase):
    def test_every_case_matches_python(self):
        for case in CASES["cases"]:
            with self.subTest(fn=case["fn"], inp=case["in"]):
                self.assertEqual(PY_FN[case["fn"]](*case["in"]), case["out"])

    def test_fixture_covers_the_planned_shapes(self):
        fns = {c["fn"] for c in CASES["cases"]}
        self.assertEqual(fns, set(PY_FN))
        durations = [c["in"][0] for c in CASES["cases"] if c["fn"] == "duration"]
        self.assertTrue(any(d is not None and 0 <= d < 1000 for d in durations), "sub-second")
        self.assertTrue(any(d is not None and d >= 7_200_000 for d in durations), "multi-hour")
        self.assertTrue(any(len(c["in"][0] or "") == 92 for c in CASES["cases"] if c["fn"] == "shortSlug"))
        self.assertTrue(any(c["fn"] == "delta" and c["out"].startswith("-") for c in CASES["cases"]))

    def test_thresholds_and_null_glyph_are_the_published_constants(self):
        self.assertEqual(fmt.LOW_N_CELL, CASES["thresholds"]["lowNCell"])
        self.assertEqual(fmt.LOW_N_BEST, CASES["thresholds"]["lowNBest"])
        self.assertEqual(fmt.NULL_GLYPH, CASES["nullGlyph"])


class TestFormatRules(unittest.TestCase):
    def test_money_tiers(self):
        self.assertEqual(fmt.fmt_money(0.0072), "$0.0072")
        self.assertEqual(fmt.fmt_money(0.187), "$0.187")
        self.assertEqual(fmt.fmt_money(12.4), "$12.40")

    def test_percent_is_an_integer_and_score_has_two_decimals(self):
        self.assertEqual(fmt.fmt_percent(0.4949), "49%")
        self.assertEqual(fmt.fmt_score(0.5), "0.50")

    def test_none_is_the_null_glyph_never_a_dash_character(self):
        for f in (fmt.fmt_money, fmt.fmt_percent, fmt.fmt_score, fmt.fmt_duration_ms,
                  fmt.fmt_tokens, fmt.short_slug):
            out = f(None)
            self.assertEqual(out, fmt.NULL_GLYPH)
            self.assertNotIn("—", out)
            self.assertNotIn("–", out)

    def test_short_slug_drops_vendor_and_keeps_version(self):
        self.assertEqual(fmt.short_slug("deepseek/deepseek-v4-flash-0731"), "deepseek-v4-flash-0731")

    def test_short_slug_bounds_long_slugs(self):
        out = fmt.short_slug("v/" + "x" * 90)
        self.assertEqual(len(out), 36)

    def test_rounding_is_half_up_like_javascript(self):
        # 0.125 is exactly representable; JS toFixed(2) gives 0.13, Python's own
        # format would give 0.12. The contract is the JS answer.
        self.assertEqual(fmt.fmt_score(0.125), "0.13")

    def test_tui_helpers_are_the_shared_formatter(self):
        self.assertIs(tui_state.fmt_cost, fmt.fmt_money)
        self.assertIs(tui_state.fmt_tokens, fmt.fmt_tokens)
        self.assertIs(tui_state.fmt_ms, fmt.fmt_duration_ms)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestJsParity(unittest.TestCase):
    def test_format_js_matches_the_same_fixture(self):
        script = (
            "import {readFileSync} from 'node:fs';"
            f"import * as F from {(ROOT / 'ui' / 'js' / 'format.js').as_uri()!r};"
            f"const fx = JSON.parse(readFileSync({str(ROOT / 'tests' / 'fixtures' / 'format_cases.json')!r}, 'utf8'));"
            "const bad = [];"
            "for (const c of fx.cases) { const got = F[c.fn](...c.in);"
            " if (got !== c.out) bad.push([c.fn, c.in, got, c.out]); }"
            "if (F.LOW_N_CELL !== fx.thresholds.lowNCell || F.LOW_N_BEST !== fx.thresholds.lowNBest"
            " || F.NULL_GLYPH !== fx.nullGlyph) bad.push(['constants']);"
            "console.log(JSON.stringify(bad));"
        )
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])


class TestSpaWiring(unittest.TestCase):
    def test_app_html_loads_the_shared_formatter_before_app_js(self):
        html = (ROOT / "ui" / "app.html").read_text(encoding="utf-8")
        self.assertLess(html.index("/static/js/format.js"), html.index("/static/app.js"))

    def test_app_js_has_no_private_number_formatting_left(self):
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        for fn in ("fmtMoney", "fmtPct", "fmtScore", "fmtMs", "fmtTok"):
            body = re.search(rf"function {fn}\(v\) \{{(.*?)\}}", js, re.S)
            self.assertIsNotNone(body, fn)
            self.assertIn("F.", body.group(1), f"{fn} must delegate to ui/js/format.js")


_SPEC = importlib.util.spec_from_file_location("build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
_corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_corpus)
BANNED = re.compile("[\u2014\u2013\u26a0\u2605\u2713\u2715]")


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role, input_price_per_mtok=0.03, output_price_per_mtok=0.10)


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed")
class TestBrowserFormatter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        Runner(dry_run=True, runs_dir=cls.tmp, store=RunStore(cls.tmp), run_group="g").run(
            TaskSpec(id="t-task", type="html", prompt="p"),
            _model("vendor/orch-model", "orchestrator"), _model("vendor/work-model", "worker"))
        obs = Observatory(Path(cls.tmp), Path(cls.tmp) / "tasks", Path(cls.tmp) / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_browser_runs_the_same_fixture_and_shows_no_dashes(self):
        banned = BANNED
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page()
                pg.goto(f"http://127.0.0.1:{self.port}/")
                pg.wait_for_selector("h1")
                got = pg.evaluate(
                    "(cases) => cases.map(c => window.OrchFormat[c.fn](...c.in))", CASES["cases"])
                self.assertEqual(got, [c["out"] for c in CASES["cases"]])
                for route in ("/", "/runs", "/leaderboard", "/compare", "/models", "/cards", "/about"):
                    pg.goto(f"http://127.0.0.1:{self.port}/#{route}")
                    pg.wait_for_selector("h1")
                    self.assertIsNone(banned.search(pg.inner_text("body")), route)
            finally:
                browser.close()


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed")
class TestCorpusCopy(unittest.TestCase):
    """The U23 corpus (92-character keys, orphaned rows, low-n groups, failed runs
    with spend) rendered end to end: no dash, no glyph, and the long key shortened."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = _corpus.build_corpus(cls.tmp / "runs", "full")
        obs = Observatory(cls.tmp / "runs", ROOT / "tests" / "fixtures" / "observatory" / "tasks",
                          ROOT / "tests" / "fixtures" / "observatory" / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_every_route_renders_without_dashes_or_status_glyphs(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                pg = browser.new_page()
                for route in ("/", "/runs", "/leaderboard", "/compare", "/models", "/cards", "/about"):
                    pg.goto(f"http://127.0.0.1:{self.port}/#{route}")
                    pg.wait_for_selector("h1")
                    pg.wait_for_timeout(300)
                    text = pg.inner_text("body")
                    self.assertIsNone(BANNED.search(text), f"{route}: {BANNED.search(text)}")
                    for tip in pg.eval_on_selector_all("[title]", "els => els.map(e => e.getAttribute('title'))"):
                        self.assertIsNone(BANNED.search(tip), f"{route} title: {tip}")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
