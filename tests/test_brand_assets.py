"""The Score, U3: Downbeat mark, wordmark, favicon set (DESIGN.md A1-A3).

Static checks on the committed brand files plus the served MIME types and the
shell's link tags. Nothing here reaches a provider or needs a rasteriser: the
PNG/ICO checks read the file headers directly.
"""

from __future__ import annotations

import json
import re
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestral.web.server import Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
BRAND = UI / "brand"
SVG_NS = "{http://www.w3.org/2000/svg}"

BRAND_SVGS = (
    BRAND / "mark.svg", BRAND / "mark-16.svg", BRAND / "mark-24.svg",
    BRAND / "wordmark.svg", BRAND / "lockup-horizontal.svg",
    BRAND / "lockup-stacked.svg", UI / "favicon.svg",
)
# ui/tokens.css --ink in paper and stage; the only literal inks allowed.
TOKEN_INKS = {"#121417", "#ECEEF0"}


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


def _ico_sizes(path: Path) -> list[tuple[int, int]]:
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "not an ICO"
    out = []
    for i in range(count):
        w, h = struct.unpack("BB", data[6 + 16 * i:8 + 16 * i])
        out.append((w or 256, h or 256))
    return out


class TestBrandSvgs(unittest.TestCase):
    def test_files_exist(self):
        for p in BRAND_SVGS:
            self.assertTrue(p.is_file(), p)

    def test_each_is_valid_svg_with_viewbox_and_title(self):
        for p in BRAND_SVGS:
            root = ET.parse(p).getroot()
            self.assertEqual(root.tag, SVG_NS + "svg", p)
            self.assertTrue(root.get("viewBox"), f"{p.name}: no viewBox")
            title = root.find(SVG_NS + "title")
            self.assertIsNotNone(title, f"{p.name}: no <title>")
            self.assertTrue((title.text or "").strip())

    def test_flat_single_ink_no_raster_no_gradient_no_text(self):
        for p in BRAND_SVGS:
            tags = {el.tag.removeprefix(SVG_NS) for el in ET.parse(p).iter()}
            self.assertFalse(tags & {"image", "linearGradient", "radialGradient",
                                     "pattern", "filter", "text", "foreignObject"},
                             f"{p.name}: {tags}")
            src = p.read_text()
            for colour in re.findall(r"#[0-9A-Fa-f]{3,8}\b", src):
                self.assertIn(colour.upper(), TOKEN_INKS, f"{p.name}: {colour}")
            self.assertNotRegex(src, r"url\(\s*['\"]?(?!#)")

    def test_no_external_references(self):
        for p in BRAND_SVGS:
            src = p.read_text()
            self.assertNotRegex(src, r"(?i)(href|src)\s*=\s*['\"](?!#)", p.name)
            self.assertNotIn("@import", src)
            urls = re.findall(r"https?://[^\s\"'<>)]+", src)
            self.assertEqual([u for u in urls if "www.w3.org/2000/svg" not in u], [], p.name)

    def test_mark_viewboxes_and_optical_variants(self):
        vb = lambda p: ET.parse(p).getroot().get("viewBox")  # noqa: E731
        self.assertEqual(vb(BRAND / "mark.svg"), "0 0 24 24")
        self.assertEqual(vb(BRAND / "mark-24.svg"), "0 0 24 24")
        self.assertEqual(vb(BRAND / "mark-16.svg"), "0 0 16 16")
        self.assertEqual(vb(UI / "favicon.svg"), "0 0 16 16")
        # Optical variants are drawn, not scaled: distinct geometry per size.
        geo = {n: re.findall(r' d="([^"]+)"', (BRAND / n).read_text())
               for n in ("mark.svg", "mark-24.svg", "mark-16.svg")}
        self.assertEqual(len({tuple(v) for v in geo.values()}), 3)
        # 16px = two staff lines + baton; 24px masters = three lines + baton.
        self.assertEqual(geo["mark-16.svg"][0].count("z"), 3)   # 1 line + 2 knockout halves
        self.assertEqual(geo["mark.svg"][0].count("z"), 4)      # 2 lines + 2 halves

    def test_marks_use_currentcolor_only(self):
        for n in ("mark.svg", "mark-16.svg", "mark-24.svg", "wordmark.svg",
                  "lockup-horizontal.svg", "lockup-stacked.svg"):
            src = (BRAND / n).read_text()
            self.assertIn("currentColor", src, n)
            self.assertNotRegex(src, r"#[0-9A-Fa-f]{3,8}\b", n)

    def test_wordmark_is_outlined_not_live_text(self):
        for n in ("wordmark.svg", "lockup-horizontal.svg", "lockup-stacked.svg"):
            root = ET.parse(BRAND / n).getroot()
            self.assertIsNone(root.find(f".//{SVG_NS}text"), n)
            self.assertNotIn("font-family", (BRAND / n).read_text(), n)
            self.assertGreater(len((BRAND / n).read_text()), 2000, n)

    def test_favicon_swaps_ink_with_color_scheme(self):
        src = (UI / "favicon.svg").read_text()
        self.assertIn("prefers-color-scheme:dark", src.replace(" ", ""))
        self.assertIn("#121417", src)
        self.assertIn("#ECEEF0", src)
        self.assertNotIn("<rect", src)  # one color, no tile


class TestRasterAssets(unittest.TestCase):
    def test_apple_touch_and_512(self):
        self.assertEqual(_png_size(BRAND / "apple-touch-icon.png"), (180, 180))
        self.assertEqual(_png_size(BRAND / "icon-512.png"), (512, 512))

    def test_ico_has_16_32_48(self):
        self.assertEqual(sorted(_ico_sizes(UI / "favicon.ico")), [(16, 16), (32, 32), (48, 48)])

    def test_build_script_present_and_executable(self):
        script = ROOT / "scripts" / "build-icons.sh"
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertIn("set -euo pipefail", script.read_text())


class TestManifest(unittest.TestCase):
    def test_parses_and_icon_paths_exist(self):
        m = json.loads((UI / "site.webmanifest").read_text())
        for key in ("name", "short_name", "start_url", "display", "theme_color",
                    "background_color", "icons"):
            self.assertIn(key, m)
        self.assertEqual(m["theme_color"].upper(), "#F7F8F8")
        sizes = set()
        for icon in m["icons"]:
            self.assertTrue(icon["src"].startswith("/static/"), icon)
            self.assertTrue((UI / icon["src"].removeprefix("/static/")).is_file(), icon)
            sizes.add(icon["sizes"])
        self.assertIn("512x512", sizes)


class TestShellAndServing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "tasks").mkdir()
        (tmp / "models").mkdir()
        obs = Observatory(tmp, tmp / "tasks", tmp / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._tmp.cleanup()

    def get(self, path: str) -> tuple[int, str, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type", ""), e.read()

    def test_served_types(self):
        cases = {
            "/favicon.ico": "image/x-icon",
            "/static/favicon.ico": "image/x-icon",
            "/static/favicon.svg": "image/svg+xml",
            "/static/brand/mark.svg": "image/svg+xml",
            "/static/brand/lockup-horizontal.svg": "image/svg+xml",
            "/static/brand/apple-touch-icon.png": "image/png",
            "/static/brand/icon-512.png": "image/png",
            "/static/site.webmanifest": "application/manifest+json",
        }
        for path, ctype in cases.items():
            code, got, body = self.get(path)
            self.assertEqual((code, got), (200, ctype), path)
            self.assertTrue(body, path)

    def test_favicon_ico_is_the_real_icon(self):
        _, _, body = self.get("/favicon.ico")
        self.assertEqual(body, (UI / "favicon.ico").read_bytes())

    def test_shell_links_icon_set_and_drops_text_glyph(self):
        _, _, body = self.get("/")
        html = body.decode()
        self.assertRegex(html, r'<link rel="icon" href="/static/favicon\.svg" type="image/svg\+xml">')
        self.assertIn('<link rel="icon" href="/favicon.ico"', html)
        self.assertIn('<link rel="apple-touch-icon" href="/static/brand/apple-touch-icon.png">', html)
        self.assertIn('<link rel="manifest" href="/static/site.webmanifest">', html)
        self.assertNotIn("◆", html)  # the old text-glyph brand
        self.assertIn('class="brand-mark"', html)

    def test_linked_hrefs_resolve(self):
        _, _, body = self.get("/")
        for href in re.findall(r'<link rel="(?:icon|apple-touch-icon|manifest)" href="([^"]+)"',
                               body.decode()):
            self.assertEqual(self.get(href)[0], 200, href)


if __name__ == "__main__":
    unittest.main()
