"""The Score, U16: server error pages and static reports.

Error pages (orchestral/web/render.py) and the HTML reports
(orchestral/reporter.py) are single-file pages: tokens and the Rest drawing
are inlined, nothing is fetched, nothing needs JavaScript, and the copy follows
DESIGN.md section 10. Nothing here reaches a provider.
"""

from __future__ import annotations

import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestral import design_tokens, reporter
from orchestral.storage import RunMeta
from orchestral.web import render, server
from orchestral.web.server import Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FUNC_COLOR = re.compile(r"\b(?:rgba?|hsla?)\(", re.I)
# Same rules as the U5 copy lint (tests/test_copy_rules.py on feat/score-u5-format).
DASH_EMOJI = re.compile("[—–☀-➿⭐⭕️\U0001F000-\U0001FAFF]")
BANNED = re.compile(r"\bAI\b|(?i:\bseamless(?:ly)?\b|\belevat(?:e|es|ed|ing)\b|\bunlock(?:s|ed|ing)?\b)")
# anything that makes the browser fetch: external urls, scripts, imports
EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//|url\(\s*["']?(?:https?:)?//|@import|<script\b|<link\b""", re.I)


def visible_text(html: str) -> str:
    """Page text outside <style>, <script> and tags (attributes excluded)."""
    html = re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


class CleanPage:
    def assertClean(self, page: str) -> None:
        body = design_tokens.strip_token_block(page)
        self.assertNotRegex(body, HEX, "colour literal outside the token block")
        self.assertNotRegex(body, FUNC_COLOR)
        self.assertNotRegex(page, EXTERNAL)
        self.assertNotRegex(page, DASH_EMOJI)
        self.assertNotRegex(page, BANNED)
        self.assertNotIn("<script", page.lower())


class TestErrorPages(CleanPage, unittest.TestCase):
    def pages(self):
        return {
            "not_found": render.render_not_found("/run/ghost"),
            "server_error": render.render_server_error("internal error (ValueError)"),
            "bad_request": render.render_bad_request("ui/app.html missing"),
        }

    def test_pages_are_clean_and_link_home(self):
        for name, page in self.pages().items():
            with self.subTest(name):
                self.assertClean(page)
                self.assertIn('href="/"', page)
                self.assertIn("<title>", page)

    def test_rests(self):
        pages = self.pages()
        self.assertIn('href="#r-missing"', pages["not_found"])
        self.assertIn('id="r-missing"', pages["not_found"])
        self.assertNotIn("r-error", pages["not_found"])
        self.assertIn('href="#r-error"', pages["server_error"])
        self.assertIn('id="r-error"', pages["server_error"])
        # r-starting is pending the logo decision (PR #127): never used here
        for page in pages.values():
            self.assertNotIn("r-starting", page)
            self.assertNotIn("r-empty", page)

    def test_not_found_offers_search(self):
        page = render.render_not_found("/nope")
        self.assertIn('href="/#/runs"', page)

    def test_both_themes_without_js(self):
        for page in self.pages().values():
            self.assertIn("prefers-color-scheme: dark", page)
            self.assertIn(':root[data-theme="stage"]', page)
            self.assertIn("--canvas", page)
            self.assertIn("var(--canvas)", page)
            self.assertIn("var(--ink)", page)
        t = design_tokens.load()
        page = self.pages()["not_found"]
        for name in design_tokens.THEMES:
            self.assertIn(t[name]["--canvas"], page)

    def test_escaping(self):
        evil = "<script>alert(1)</script>"
        for page in (render.render_not_found(evil), render.render_server_error(evil),
                     render.render_bad_request(evil)):
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script", page)

    def test_without_ui_dir_still_renders(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(design_tokens, "UI_DIR", Path(tmp)):
            page = render.render_not_found("/x")
        self.assertIn("--canvas", page)
        self.assertIn('href="/"', page)


class TestErrorPagesOverHttp(CleanPage, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "tasks").mkdir()
        (tmp / "models").mkdir()
        cls.obs = Observatory(runs_dir=tmp / "runs", tasks_dir=tmp / "tasks", models_dir=tmp / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.headers.get("Content-Type", ""), e.read().decode()
            finally:
                e.close()

    def test_404_status_and_content(self):
        code, ctype, body = self.get("/definitely/not/here")
        self.assertEqual(code, 404)
        self.assertIn("text/html", ctype)
        self.assertIn("r-missing", body)
        self.assertIn("/definitely/not/here", body)
        self.assertClean(body)

    def test_static_404_is_the_same_page(self):
        code, _, body = self.get("/static/nope.css")
        self.assertEqual(code, 404)
        self.assertIn("r-missing", body)

    def test_500_when_app_shell_missing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "UI_DIR", Path(tmp)):
            code, ctype, body = self.get("/")
        self.assertEqual(code, 500)
        self.assertIn("text/html", ctype)
        self.assertIn("r-error", body)
        self.assertClean(body)

    def test_api_errors_stay_json(self):
        code, ctype, _ = self.get("/api/nope")
        self.assertEqual(code, 404)
        self.assertIn("application/json", ctype)


def _meta(rid, orch, worker, *, cost, score=None, passes=True, judge_score=None):
    return RunMeta(
        run_id=rid, orchestrator=orch, task_id="t", worker=worker, status="finished",
        started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:01:00Z",
        total_cost_usd=cost, total_input_tokens=10, total_output_tokens=20,
        score=score, passes=passes, judge_score=judge_score, run_dir="/tmp/x",
        config={"planner": "raw"},
    )


def _runs(pairings=2, unjudged=1):
    out = [_meta(f"j{i}", f"o{i}", f"w{i}", cost=0.01 * (i + 1), judge_score=0.5 + 0.05 * i)
           for i in range(pairings)]
    out += [_meta(f"u{i}", "o0", "w0", cost=0.02, score=0.9) for i in range(unjudged)]
    return out


SUMMARY = {"runs": 3, "total_cost_usd": 0.03, "total_tokens": 90}


class TestStaticReports(CleanPage, unittest.TestCase):
    def test_dashboard_clean_and_paper_only(self):
        page = reporter._dashboard_html(_runs(), SUMMARY)
        self.assertClean(page)
        self.assertNotIn(':root[data-theme="stage"]', page)
        self.assertIn("var(--canvas)", page)

    def test_index_gallery_and_run_pages_clean(self):
        runs = _runs()
        self.assertClean(reporter._index_html(runs))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertClean(reporter.generate_gallery(runs, Path(tmp)))

    def test_run_page_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "events.jsonl").write_text("")
            meta = _meta("r1", "o", "w", cost=0.01, judge_score=0.7)
            page = reporter._run_card(meta, run_dir)
        self.assertClean(page)

    def test_no_colour_literals_in_sources(self):
        for rel in ("orchestral/web/render.py", "orchestral/reporter.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotRegex(src, HEX, rel)
            self.assertNotRegex(src, FUNC_COLOR, rel)

    def test_scatter_excludes_unjudged_and_says_so(self):
        svg = reporter._scatter_svg(_runs(pairings=2, unjudged=3))
        self.assertEqual(svg.count("<circle"), 2)
        self.assertIn("3 runs without a judge score are not plotted", svg)

    def test_scatter_one_unjudged_singular(self):
        svg = reporter._scatter_svg(_runs(pairings=1, unjudged=1))
        self.assertIn("1 run without a judge score is not plotted", svg)

    def test_scatter_no_judged_runs(self):
        svg = reporter._scatter_svg([_meta("a", "o", "w", cost=0.01, score=0.9)])
        self.assertNotIn("<circle", svg)
        self.assertIn("1 run without a judge score is not plotted", svg)

    def test_categorical_from_okabe_ito_in_order(self):
        svg = reporter._scatter_svg(_runs(pairings=7, unjudged=0))
        used = re.findall(r"var\(--cat-(\d)\)", svg)
        self.assertEqual(sorted(set(used), key=int), [str(i) for i in range(1, 8)])
        page = reporter._dashboard_html(_runs(pairings=7, unjudged=0), SUMMARY)
        for i, colour in enumerate(design_tokens.CATEGORICAL, 1):
            self.assertIn(f"--cat-{i}: {colour};", page)
        self.assertEqual(len(design_tokens.CATEGORICAL), 7)
        self.assertEqual(design_tokens.CATEGORICAL[:3], ("#0072B2", "#E69F00", "#009E73"))

    def test_eighth_pairing_labeled_without_new_hue(self):
        svg = reporter._scatter_svg(_runs(pairings=9, unjudged=0))
        self.assertNotRegex(svg, r"--cat-(?:[89]|\d\d)")
        self.assertEqual(svg.count("<circle"), 9)
        for i in range(9):
            self.assertIn(f"o{i} → w{i}", svg)  # direct label, no legend

    def test_scatter_is_accessible(self):
        svg = reporter._scatter_svg(_runs())
        self.assertIn("role='img'", svg)
        self.assertIn("<desc ", svg)

    def test_reports_are_single_file(self):
        page = reporter._dashboard_html(_runs(), SUMMARY)
        self.assertNotRegex(page, EXTERNAL)


if __name__ == "__main__":
    unittest.main()
