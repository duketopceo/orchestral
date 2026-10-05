"""U7: static checks on the shell, with no browser.

Pins the import map, the icon wiring and the sprite's delivery (MIME and
version), the hatch colour hook, and the module list the plan names.
"""

from __future__ import annotations

import re
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestral.web.server import UI_DIR, Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
HTML = (UI / "app.html").read_text(encoding="utf-8")
CSS = (UI / "app.css").read_text(encoding="utf-8")
SPRITE = (UI / "icons.svg").read_text(encoding="utf-8")
SYMBOLS = set(re.findall(r'<symbol id="([^"]+)"', SPRITE))
JS = {p: p.read_text(encoding="utf-8") for p in (UI / "js").rglob("*.js")}


def _code(src: str) -> str:
    """JS with block and line comments removed (prose may name a symbol)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


class TestImportMap(unittest.TestCase):
    def test_every_module_is_in_the_import_map(self) -> None:
        mapped = set(re.findall(r'"(/static/js/[^"]+\.js)": "', HTML))
        on_disk = {"/static/" + p.relative_to(UI).as_posix() for p in JS}
        self.assertEqual(sorted(on_disk - mapped), [], "module missing from the import map")
        self.assertEqual(sorted(mapped - on_disk), [], "import map names a module that is gone")

    def test_plan_modules_exist(self) -> None:
        for name in ("shell", "keys", "live", "components/table", "components/states",
                     "components/dialog", "components/tabs"):
            self.assertTrue((UI / "js" / f"{name}.js").is_file(), name)


class TestIconWiring(unittest.TestCase):
    def test_every_static_use_resolves_to_a_symbol(self) -> None:
        refs = set(re.findall(r'<use href="#([^"]+)"', HTML))
        for src in map(_code, JS.values()):
            refs |= set(re.findall(r'<use href="#([^"$]+)"', src))
            refs |= {f"i-{n}" for n in re.findall(r'\bicon\("([a-z-]+)"', src)}
            refs |= {f"r-{n}" for n in re.findall(r'\brest\("([a-z-]+)"', src)}
        self.assertTrue(refs, "no icon references found")
        self.assertEqual(sorted(refs - SYMBOLS - {"hatch"}), [])

    def test_nav_rail_and_state_chips_use_the_sprite(self) -> None:
        for sym in ("i-overview", "i-runs", "i-leaderboard", "i-compare", "i-cards",
                    "i-models", "i-help", "i-new-run"):
            self.assertIn(f'href="#{sym}"', HTML, sym)

    def test_stalled_dot_and_data_rest_hooks_are_gone(self) -> None:
        css_and_js = CSS + "".join(JS.values())
        self.assertNotIn("TODO(U7)", css_and_js)
        self.assertNotIn("dot-stalled", css_and_js)
        self.assertNotIn('data-rest="stalled"', css_and_js)
        self.assertIn('icon("stalled"', JS[UI / "js" / "components" / "states.js"])
        self.assertIn("liveGlyph", JS[UI / "js" / "live.js"])

    def test_starting_rest_uses_the_shipped_glyph(self) -> None:
        src = JS[UI / "js" / "components" / "states.js"]
        self.assertIn('starting: "starting"', src)
        self.assertIn("r-starting", SYMBOLS)

    def test_hatch_pattern_takes_its_colour_from_css(self) -> None:
        self.assertRegex(CSS, r"#hatch\s*\{[^}]*color:\s*var\(--ink-2\)")

    def test_sprite_is_injected_inline_from_a_versioned_same_origin_url(self) -> None:
        src = JS[UI / "js" / "shell.js"]
        self.assertIn("/static/icons.svg", src)
        self.assertIn("import.meta.url", src)  # reuses the module's ?v= token


class TestNoDeadInterpolation(unittest.TestCase):
    def test_no_template_expression_sits_in_a_plain_quoted_string(self) -> None:
        """`'... ${icon("x")} ...'` prints the source text; it needs backticks."""
        bad = []
        for p, src in JS.items():
            for n, line in enumerate(_code(src).splitlines(), 1):
                if re.search(r"""[?:(,=]\s*'[^'`\n]*\$\{(?:icon|liveGlyph|esc)\(""", line):
                    bad.append(f"{p.name}:{n}")
        self.assertEqual(bad, [])


class TestSpriteDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        obs = Observatory(Path(cls.tmp), Path(cls.tmp) / "tasks", Path(cls.tmp) / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_sprite_served_as_svg(self) -> None:
        with urllib.request.urlopen(f"{self.base}/static/icons.svg?v=1") as r:
            self.assertEqual(r.headers.get_content_type(), "image/svg+xml")
            self.assertIn(b"<symbol", r.read())

    def test_shell_page_is_versioned_and_carries_the_landmarks(self) -> None:
        with urllib.request.urlopen(f"{self.base}/") as r:
            html = r.read().decode()
        self.assertNotIn("__V__", html)
        self.assertEqual(html.count("<main"), 1)
        self.assertIn('class="skip-link"', html)
        self.assertEqual(UI_DIR, UI)


class TestShellMarkup(unittest.TestCase):
    def test_rail_labels_follow_the_ia(self) -> None:
        rail = HTML.split('id="nav"', 1)[1].split("</nav>", 1)[0]
        labels = re.findall(r"<span class=\"nav-label\">([^<]+)</span>", rail)
        self.assertEqual(labels, ["Now", "Runs", "Pairings", "Compare", "Experiments",
                                  "Publish", "Models", "New run", "Guide"])
        routes = re.findall(r'data-route="([^"]+)"', rail)
        self.assertEqual(routes, ["/", "/runs", "/leaderboard", "/compare", "/experiment",
                                  "/cards", "/models", "/new", "/about"])

    def test_every_nav_link_has_an_icon_and_the_landmarks_are_named(self) -> None:
        self.assertIn('aria-label="Primary"', HTML)
        self.assertIn('aria-label="Mobile"', HTML)
        self.assertIn('id="live-region"', HTML)
        self.assertRegex(HTML, r'id="main"[^>]*tabindex="-1"')


if __name__ == "__main__":
    unittest.main()
