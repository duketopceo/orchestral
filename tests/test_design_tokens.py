"""The Score, U1: token source of truth, theme contract, static serving.

``ui/tokens.css`` is the only token source (KTD2). These tests pin the
contrast table from DESIGN.md 6.1 for both themes, keep raw colour literals
out of the app stylesheet, and check that the static server answers with the
right MIME types even on a Python with no system MIME table (KTD6).

Nothing here reaches a provider.
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

from orchestral import design_tokens
from orchestral.web.server import Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"

# DESIGN.md 6.1 "Contrast (measured)": text pairs need 4.5:1, graphics 3:1.
TEXT_ON_SURFACES = (
    "ink", "ink-2", "ink-3",
    "pass-text", "fail-text", "judge-text", "live-text",
)
SURFACES = ("canvas", "surface")
# live-fill is a dot/bar that always travels with live-text or a glyph; 6.1
# only claims 3:1 for the pass and fail fills (paper live-fill is 2.25).
GRAPHIC_FILLS = ("pass-fill", "fail-fill", "judge-fill")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", re.I)


class TestTokenParsing(unittest.TestCase):
    def test_both_themes_have_canvas_and_ink(self):
        toks = design_tokens.load()
        for theme in ("paper", "stage"):
            self.assertIn("--canvas", toks[theme])
            self.assertIn("--ink", toks[theme])
        self.assertEqual(toks["paper"]["--canvas"].upper(), "#F7F8F8")
        self.assertEqual(toks["stage"]["--canvas"].upper(), "#0E1012")

    def test_theme_names_and_keys_match(self):
        toks = design_tokens.load()
        self.assertEqual(set(toks["paper"]), set(toks["stage"]))

    def test_missing_ui_dir_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory() as d:
            toks = design_tokens.load(Path(d) / "nope")
        for theme in ("paper", "stage"):
            self.assertIn("--canvas", toks[theme])
            self.assertIn("--ink", toks[theme])

    def test_embedded_fallback_agrees_with_tokens_css(self):
        real = design_tokens.load()
        for theme, fb in design_tokens.FALLBACK.items():
            for k, v in fb.items():
                self.assertEqual(real[theme][k].upper(), v.upper(), f"{theme} {k}")

    def test_dark_media_block_mirrors_explicit_stage_block(self):
        # The stage tokens are written twice (OS preference, explicit choice);
        # the copies must never drift.
        css = (UI / "tokens.css").read_text()
        media = design_tokens.parse_blocks(css)['@dark :root:not([data-theme="paper"])']
        explicit = design_tokens.parse_blocks(css)[':root[data-theme="stage"]']
        self.assertEqual(media, explicit)
        self.assertEqual(set(media), set(design_tokens.load()["stage"]))


class TestContrastTable(unittest.TestCase):
    def test_text_pairs_meet_aa_in_both_themes(self):
        toks = design_tokens.load()
        for theme in ("paper", "stage"):
            for name in TEXT_ON_SURFACES:
                for surf in SURFACES:
                    r = design_tokens.contrast(toks[theme][f"--{name}"], toks[theme][f"--{surf}"])
                    self.assertGreaterEqual(r, 4.5, f"{theme}: --{name} on --{surf} = {r:.2f}")

    def test_graphics_pairs_meet_3_to_1_in_both_themes(self):
        toks = design_tokens.load()
        for theme in ("paper", "stage"):
            for name in (*GRAPHIC_FILLS, "control-border"):
                r = design_tokens.contrast(toks[theme][f"--{name}"], toks[theme]["--surface"])
                self.assertGreaterEqual(r, 3.0, f"{theme}: --{name} vs --surface = {r:.2f}")

    def test_text_on_fills_meets_aa(self):
        toks = design_tokens.load()
        for theme in ("paper", "stage"):
            t = toks[theme]
            for fill in ("pass-fill", "fail-fill", "live-fill"):
                r = design_tokens.contrast(t["--on-fill"], t[f"--{fill}"])
                self.assertGreaterEqual(r, 4.5, f"{theme}: on-fill on --{fill} = {r:.2f}")
            r = design_tokens.contrast(t["--on-judge"], t["--judge-fill"])
            self.assertGreaterEqual(r, 4.5, f"{theme}: on-judge on judge-fill = {r:.2f}")
            r = design_tokens.contrast(t["--on-ink"], t["--ink"])
            self.assertGreaterEqual(r, 4.5)

    def test_ink_3_meets_aa_on_canvas_and_surface(self):
        toks = design_tokens.load()
        for theme in ("paper", "stage"):
            for surf in SURFACES:
                r = design_tokens.contrast(toks[theme]["--ink-3"], toks[theme][f"--{surf}"])
                self.assertGreaterEqual(r, 4.5, f"{theme}: ink-3 on {surf} = {r:.2f}")


class TestNoRawColourLiterals(unittest.TestCase):
    def test_app_css_and_html_have_no_colour_literals(self):
        for name in ("app.css", "app.html"):
            text = (UI / name).read_text()
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            # <meta name="theme-color"> cannot reference a CSS variable; those
            # two are pinned to tokens.css by test_color_scheme_and_paired_...
            text = re.sub(r'<meta name="theme-color"[^>]*>', "", text)
            self.assertEqual(HEX.findall(text), [], f"raw colour literal in ui/{name}")

    def test_tokens_css_is_where_literals_live(self):
        self.assertTrue(HEX.search((UI / "tokens.css").read_text()))


class TestShellContract(unittest.TestCase):
    def setUp(self):
        self.html = (UI / "app.html").read_text()

    def test_color_scheme_and_paired_theme_color_metas(self):
        self.assertIn('<meta name="color-scheme" content="light dark">', self.html)
        metas = re.findall(r'<meta name="theme-color"[^>]*>', self.html)
        self.assertEqual(len(metas), 2)
        self.assertTrue(any("(prefers-color-scheme: light)" in m for m in metas))
        self.assertTrue(any("(prefers-color-scheme: dark)" in m for m in metas))
        toks = design_tokens.load()
        self.assertIn(toks["paper"]["--canvas"].upper(), self.html.upper())
        self.assertIn(toks["stage"]["--canvas"].upper(), self.html.upper())

    def test_shell_declares_default_theme_and_loads_tokens_first(self):
        self.assertIn('data-default-theme="system"', self.html)
        self.assertLess(self.html.index("tokens.css"), self.html.index("app.css"))

    def test_prepaint_script_precedes_stylesheets(self):
        self.assertLess(self.html.index("data-theme"), self.html.index('rel="stylesheet"'))

    def test_assets_are_versioned(self):
        for ref in ("tokens.css", "app.css", "js/main.js"):
            self.assertRegex(self.html, rf"/static/{re.escape(ref)}\?v=__V__")


class _Served(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "tasks").mkdir()
        (cls.tmp / "models").mkdir()
        obs = Observatory(cls.tmp, cls.tmp / "tasks", cls.tmp / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path: str) -> tuple[int, str, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type", ""), e.read()


class TestStaticServing(_Served):

    def test_mime_map_without_system_table(self):
        from orchestral.web import server
        fonts = server.UI_DIR / "fonts"
        made = not fonts.exists()
        fonts.mkdir(exist_ok=True)
        probe = fonts / "x.woff2"
        probe.write_bytes(b"wOF2")
        try:
            with patch("mimetypes.guess_type", return_value=(None, None)):
                cases = {
                    "/static/fonts/x.woff2": "font/woff2",
                    "/static/site.webmanifest": "application/manifest+json",
                    "/static/app.css": "text/css; charset=utf-8",
                    "/static/tokens.css": "text/css; charset=utf-8",
                    "/static/js/main.js": "text/javascript; charset=utf-8",
                    "/static/favicon.svg": "image/svg+xml",
                }
                for path, ctype in cases.items():
                    code, got, _ = self.get(path)
                    self.assertEqual((code, got), (200, ctype), path)
        finally:
            probe.unlink()
            if made:
                fonts.rmdir()

    def test_unknown_extension_is_octet_stream(self):
        from orchestral.web import server
        odd = server.UI_DIR / "zz.unknownext"
        odd.write_bytes(b"x")
        try:
            with patch("mimetypes.guess_type", return_value=(None, None)):
                self.assertEqual(self.get("/static/zz.unknownext")[1], "application/octet-stream")
        finally:
            odd.unlink()

    def test_shell_versions_assets_with_a_stable_token(self):
        code, _ctype, body = self.get("/")
        html = body.decode()
        self.assertEqual(code, 200)
        self.assertNotIn("__V__", html)
        versions = set(re.findall(r"/static/[\w.]+\?v=([0-9a-f]+)", html))
        self.assertEqual(len(versions), 1, html)
        self.assertEqual(versions, set(re.findall(r"\?v=([0-9a-f]+)", self.get("/")[2].decode())))

    def test_versioned_asset_url_is_served(self):
        code, ctype, _ = self.get("/static/tokens.css?v=abc123")
        self.assertEqual((code, ctype), (200, "text/css; charset=utf-8"))

    def test_traversal_still_blocked(self):
        self.assertEqual(self.get("/static/../orchestral/config.py")[0], 404)


FONTS = UI / "fonts"
FONT_FACE = re.compile(r"@font-face\s*{(.*?)}", re.S)


def _font_faces() -> list[dict[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", (UI / "tokens.css").read_text(), flags=re.S)
    return [dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", body)) for body in FONT_FACE.findall(css)]


class TestTypefaceFiles(unittest.TestCase):
    """U2: self-hosted Instrument Sans and IBM Plex Mono, DESIGN.md 6.2."""

    def test_font_budget_is_110kb_and_four_files(self):
        files = sorted(FONTS.glob("*.woff2"))
        self.assertLessEqual(len(files), 4, files)
        total = sum(f.stat().st_size for f in files)
        self.assertLessEqual(total, 110 * 1024, f"{total} bytes")

    def test_files_are_woff2(self):
        for f in FONTS.glob("*.woff2"):
            self.assertEqual(f.read_bytes()[:4], b"wOF2", f.name)

    def test_every_real_face_swaps_and_resolves_to_a_file(self):
        real = [f for f in _font_faces() if "url(" in f.get("src", "")]
        self.assertGreaterEqual(len(real), 4)
        for face in real:
            self.assertEqual(face.get("font-display"), "swap", face)
            urls = re.findall(r'url\("?([^")]+)"?\)', face["src"])
            self.assertEqual(len(urls), 1, face)
            self.assertTrue(urls[0].startswith("fonts/"), urls[0])
            self.assertTrue((UI / urls[0]).is_file(), urls[0])
        families = {f["font-family"].strip("\"'") for f in real}
        self.assertEqual(families, {"Instrument Sans", "IBM Plex Mono"})

    def test_every_woff2_is_declared(self):
        declared = {Path(u).name for f in _font_faces()
                    for u in re.findall(r'url\("?([^")]+)"?\)', f.get("src", ""))}
        self.assertEqual({f.name for f in FONTS.glob("*.woff2")}, declared)

    def test_fallback_faces_are_size_adjusted_local_aliases(self):
        fallbacks = [f for f in _font_faces() if f["font-family"].strip("\"'").endswith("Fallback")]
        self.assertEqual(len(fallbacks), 2)
        for face in fallbacks:
            self.assertIn("local(", face["src"])
            self.assertNotIn("url(", face["src"])
            for prop in ("size-adjust", "ascent-override", "descent-override", "line-gap-override"):
                self.assertIn(prop, face, face)

    def test_font_stack_names_the_fallback_faces(self):
        css = (UI / "tokens.css").read_text()
        self.assertRegex(css, r'--font-sans:\s*"Instrument Sans",\s*"Instrument Sans Fallback"')
        self.assertRegex(css, r'--font-mono:\s*"IBM Plex Mono",\s*"Plex Mono Fallback"')

    def test_license_files_exist_for_both_families(self):
        for name, marker in (("OFL-InstrumentSans.txt", "Instrument Sans"),
                             ("OFL-IBMPlexMono.txt", "Plex")):
            text = (FONTS / name).read_text()
            self.assertIn("SIL OPEN FONT LICENSE Version 1.1", text, name)
            self.assertIn(marker, text, name)

    def test_provenance_is_recorded(self):
        readme = (FONTS / "README.md").read_text()
        for needle in ("Instrument/instrument-sans", "@ibm/plex-mono", "2.5.0", "subset-fonts.sh"):
            self.assertIn(needle, readme)
        self.assertTrue((ROOT / "scripts" / "subset-fonts.sh").is_file())

    def test_type_scale_tokens_follow_design_md(self):
        css = (UI / "tokens.css").read_text()
        for tok, size, line in (("display-l", 56, 60), ("display-m", 36, 40), ("title-l", 22, 28),
                                ("title-m", 17, 24), ("title-s", 14, 20), ("body", 13, 20),
                                ("body-l", 15, 24), ("label", 12, 16), ("col-head", 11, 14),
                                ("data", 12.5, 18), ("data-strong", 12.5, 18),
                                ("metric", 28, 32), ("micro", 11, 14)):
            self.assertRegex(css, rf"--fs-{tok}:\s*{size}px;", tok)
            self.assertRegex(css, rf"--lh-{tok}:\s*{line}px;", tok)
        self.assertRegex(css, r"html\s*{\s*font-variant-numeric:\s*tabular-nums;")
        self.assertRegex(css, r'font-feature-settings:\s*"zero"')

    def test_nothing_under_11px_in_scale(self):
        css = (UI / "tokens.css").read_text()
        for px in re.findall(r"--fs-[\w-]+:\s*([\d.]+)px", css):
            self.assertGreaterEqual(float(px), 11.0)

    def test_no_cross_origin_font_or_stylesheet_references(self):
        remote = re.compile(r"(?:url\(\s*[\"']?|@import\s+[\"']?|href=[\"']|src=[\"'])(?:https?:)?//", re.I)
        for f in (UI / "app.css", UI / "tokens.css", UI / "app.html"):
            self.assertIsNone(remote.search(f.read_text()), f.name)

    def test_only_instrument_sans_regular_is_preloaded(self):
        html = (UI / "app.html").read_text()
        pre = re.findall(r'<link rel="preload"[^>]*>', html)
        self.assertEqual(len(pre), 1, pre)
        self.assertIn("InstrumentSans-latin-var.woff2", pre[0])
        self.assertIn('as="font"', pre[0])
        self.assertIn("crossorigin", pre[0])


class TestTypefaceServing(_Served):
    def test_every_declared_font_is_served_as_woff2(self):
        faces = [u for f in _font_faces() for u in re.findall(r'url\("?([^")]+)"?\)', f.get("src", ""))]
        self.assertTrue(faces)
        with patch("mimetypes.guess_type", return_value=(None, None)):
            for rel in faces:
                code, ctype, body = self.get(f"/static/{rel}")
                self.assertEqual((code, ctype), (200, "font/woff2"), rel)
                self.assertEqual(body, (UI / rel).read_bytes(), rel)

    def test_preload_url_is_served(self):
        html = self.get("/")[2].decode()
        href = re.search(r'<link rel="preload" href="([^"]+)"', html).group(1)
        self.assertEqual(self.get(href)[:2], (200, "font/woff2"))


try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestThemeBrowser(_Served):
    @staticmethod
    def _page(pw, **ctx):
        browser = pw.chromium.launch()
        return browser, browser.new_context(**ctx).new_page()

    def test_stage_choice_persists_and_flips_color_scheme(self):
        with sync_playwright() as pw:
            browser, pg = self._page(pw, color_scheme="light")
            pg.goto(f"http://127.0.0.1:{self.port}/")
            pg.wait_for_selector("#theme-toggle")
            self.assertEqual(pg.evaluate("getComputedStyle(document.documentElement).colorScheme"), "light")
            pg.evaluate("localStorage.setItem('orchestral.theme','stage')")
            pg.reload()
            pg.wait_for_selector("#theme-toggle")
            self.assertEqual(pg.evaluate("document.documentElement.dataset.theme"), "stage")
            self.assertEqual(pg.evaluate("getComputedStyle(document.documentElement).colorScheme"), "dark")
            bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
            self.assertEqual(bg, "rgb(14, 16, 18)")
            browser.close()

    def test_toggle_cycles_and_persists(self):
        with sync_playwright() as pw:
            browser, pg = self._page(pw, color_scheme="light")
            pg.goto(f"http://127.0.0.1:{self.port}/")
            pg.wait_for_selector("#theme-toggle")
            seen = []
            for _ in range(3):
                pg.click("#theme-toggle")
                seen.append(pg.evaluate("localStorage.getItem('orchestral.theme')"))
            self.assertEqual(sorted(seen), ["paper", "stage", "system"])
            pg.click("#theme-toggle")
            pg.reload()
            pg.wait_for_selector("#theme-toggle")
            self.assertEqual(
                pg.evaluate("document.documentElement.dataset.theme"),
                pg.evaluate("localStorage.getItem('orchestral.theme')"),
            )
            browser.close()

    def test_system_follows_dark_os_locally(self):
        with sync_playwright() as pw:
            browser, pg = self._page(pw, color_scheme="dark")
            pg.goto(f"http://127.0.0.1:{self.port}/")
            pg.wait_for_selector("#theme-toggle")
            self.assertEqual(pg.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(14, 16, 18)")
            browser.close()

    def test_paper_default_shell_wins_on_dark_os_with_no_stored_choice(self):
        # Hosted Worker rewrites data-default-theme to paper; emulate that here.
        with sync_playwright() as pw:
            browser, pg = self._page(pw, color_scheme="dark")
            html = (UI / "app.html").read_text().replace(
                'data-default-theme="system"', 'data-default-theme="paper"')
            pg.route(f"http://127.0.0.1:{self.port}/",
                     lambda r: r.fulfill(status=200, content_type="text/html", body=html.replace("__V__", "t")))
            # record every data-theme write and whether <body> existed yet
            pg.add_init_script(
                "window.__writes = [];"
                "new MutationObserver(ms => ms.forEach(m => window.__writes.push("
                "[m.target.getAttribute('data-theme'), !!document.body])))"
                ".observe(document, {attributes: true, subtree: true, attributeFilter: ['data-theme']});")
            pg.goto(f"http://127.0.0.1:{self.port}/")
            pg.wait_for_selector("#theme-toggle")
            self.assertEqual(pg.evaluate("window.__writes"), [["paper", False]])
            self.assertEqual(pg.evaluate("getComputedStyle(document.documentElement).colorScheme"), "light")
            self.assertEqual(pg.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(247, 248, 248)")
            browser.close()


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestTypefaceBrowser(_Served):
    def test_faces_apply_and_no_foreign_requests(self):
        origin = f"http://127.0.0.1:{self.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            pg = browser.new_context().new_page()
            urls: list[str] = []
            pg.on("request", lambda r: urls.append(r.url))
            pg.goto(origin + "/")
            pg.wait_for_selector("#theme-toggle")
            pg.evaluate("document.body.insertAdjacentHTML('beforeend', '<span id=\"probe\" class=\"mono\">0O 0x1f</span>')")
            pg.evaluate("document.fonts.ready.then(() => 0)")
            pg.evaluate("Promise.all([document.fonts.load('13px \"Instrument Sans\"'), document.fonts.load('13px \"IBM Plex Mono\"')])")
            self.assertTrue(pg.evaluate("document.fonts.check('13px \"Instrument Sans\"')"))
            self.assertTrue(pg.evaluate("document.fonts.check('13px \"IBM Plex Mono\"')"))
            self.assertTrue(pg.evaluate("getComputedStyle(document.body).fontFamily").startswith('"Instrument Sans"'))
            self.assertTrue(pg.evaluate("getComputedStyle(document.getElementById('probe')).fontFamily").startswith('"IBM Plex Mono"'))
            loaded = pg.evaluate("[...document.fonts].filter(f => f.status === 'loaded').map(f => f.family)")
            self.assertIn('"Instrument Sans"', [x if x.startswith('"') else f'"{x}"' for x in loaded])
            browser.close()
        foreign = [u for u in urls if not u.startswith(origin + "/") and not u.startswith("data:")]
        self.assertEqual(foreign, [])
        self.assertTrue(any(u.split("?")[0].endswith("/static/fonts/InstrumentSans-latin-var.woff2") for u in urls))


if __name__ == "__main__":
    unittest.main()
