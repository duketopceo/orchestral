"""The Score, U4: icon sprite, Rests and terminal glyph parity.

Pins ``ui/icons.svg`` (DESIGN.md A4, A7) and ``orchestral/glyphs.py`` (6.8):
the documented id set, one viewBox and stroke width, no colour literals, a
size budget, and a glyph row for every symbol. Nothing here reaches a provider.
"""

from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from orchestral import glyphs

ROOT = Path(__file__).resolve().parents[1]
SPRITE = ROOT / "ui" / "icons.svg"
SVG = "{http://www.w3.org/2000/svg}"

# DESIGN.md A4 required glyphs, in order, plus stalled (KTD8).
ICON_NAMES = (
    "overview",
    "runs",
    "leaderboard",
    "compare",
    "cards",
    "models",
    "help",
    "new-run",
    "live",
    "pass",
    "fail",
    "cancelled",
    "inconclusive",
    "not-judged",
    "judge",
    "cost",
    "latency",
    "tokens",
    "low-n",
    "frontier",
    "flag-interesting",
    "flag-dismiss",
    "artifact",
    "transcript",
    "plan",
    "manifest",
    "external",
    "download",
    "copy",
    "filter",
    "search",
    "sort",
    "chevron",
    "close",
    "keyboard",
    "theme",
    "stalled",
)
ICON_IDS = frozenset("i-" + n for n in ICON_NAMES)
REST_IDS = frozenset(f"r-{n}" for n in ("empty", "nomatch", "starting", "missing", "error"))

COLOUR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|oklch|color-mix)\(|"
    r"\b(?:fill|stroke|color|stop-color)\s*[=:]\s*[\"']?(?!none|currentColor|url\(|transparent)[a-zA-Z]+",
)


def _root() -> ET.Element:
    return ET.parse(SPRITE).getroot()


def _symbols() -> list[ET.Element]:
    return list(_root().iter(f"{SVG}symbol"))


class SpriteTests(unittest.TestCase):
    def test_parses_and_ids_match_documented_set(self) -> None:
        ids = [s.get("id") for s in _symbols()]
        self.assertEqual(len(ids), len(set(ids)), "duplicate symbol ids")
        self.assertEqual(set(ids), ICON_IDS | REST_IDS)

    def test_icons_share_one_16px_grid_and_stroke(self) -> None:
        for s in _symbols():
            sid = s.get("id") or ""
            want = "0 0 16 16" if sid.startswith("i-") else "0 0 64 64"
            self.assertEqual(s.get("viewBox"), want, sid)
            self.assertEqual(s.get("stroke-width"), "1.5", sid)
            self.assertEqual(s.get("stroke"), "currentColor", sid)
            self.assertEqual(s.get("stroke-linecap"), "square", sid)
            self.assertEqual(s.get("stroke-linejoin"), "miter", sid)

    def test_no_child_overrides_stroke_width(self) -> None:
        for s in _symbols():
            for el in list(s.iter())[1:]:
                self.assertIsNone(el.get("stroke-width"), s.get("id"))

    def test_no_colour_literals(self) -> None:
        text = SPRITE.read_text(encoding="utf-8")
        body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        self.assertIsNone(COLOUR_LITERAL.search(body), COLOUR_LITERAL.search(body))
        for el in _root().iter():
            for attr in ("fill", "stroke"):
                self.assertIn(el.get(attr), (None, "none", "currentColor"), el.tag)

    def test_hatch_pattern_present(self) -> None:
        pats = [p for p in _root().iter(f"{SVG}pattern") if p.get("id") == "hatch"]
        self.assertEqual(len(pats), 1)

    def test_no_raster_gradient_or_script(self) -> None:
        text = SPRITE.read_text(encoding="utf-8")
        for bad in ("<image", "Gradient", "<script", "<style", "<filter"):
            self.assertNotIn(bad, text)

    def test_size_budget(self) -> None:
        self.assertLessEqual(SPRITE.stat().st_size, 20 * 1024)

    def test_verdict_symbols_use_fill_only_on_state_glyphs(self) -> None:
        state = {"i-" + n for n in glyphs.STATE_NAMES} | {"i-flag-dismiss", "i-theme"}
        for s in _symbols():
            filled = any(el.get("fill") == "currentColor" for el in s.iter())
            if filled:
                self.assertIn(s.get("id"), state | REST_IDS, "fill outside state glyphs")


class GlyphParityTests(unittest.TestCase):
    def test_every_symbol_has_a_glyph_and_vice_versa(self) -> None:
        sprite = {(s.get("id") or "")[2:] for s in _symbols() if (s.get("id") or "").startswith("i-")}
        self.assertEqual(sprite, set(glyphs.GLYPHS))

    def test_rows_complete(self) -> None:
        for g in glyphs.GLYPHS.values():
            self.assertTrue(g.unicode and g.ascii and g.word and g.meaning, g.name)
            self.assertTrue(g.ascii.isascii(), g.name)
            self.assertIn(g.role, {"pass", "fail", "live", "judge", "ink", "ink-2", "ink-3"})

    def test_states_cover_design_table_and_stalled(self) -> None:
        for n in ("pass", "fail", "cancelled", "live", "inconclusive", "not-judged", "judge", "low-n", "flag-interesting", "stalled"):
            self.assertIn(n, glyphs.STATE_NAMES)

    def test_no_two_verdicts_share_a_glyph(self) -> None:
        uni = [glyphs.GLYPHS[n].unicode for n in glyphs.STATE_NAMES]
        asc = [glyphs.GLYPHS[n].ascii for n in glyphs.STATE_NAMES]
        self.assertEqual(len(uni), len(set(uni)))
        self.assertEqual(len(asc), len(set(asc)))
        self.assertNotEqual(glyphs.GLYPHS["stalled"].unicode, glyphs.GLYPHS["live"].unicode)

    def test_all_unicode_glyphs_distinct(self) -> None:
        uni = [g.unicode for g in glyphs.GLYPHS.values()]
        self.assertEqual(len(uni), len(set(uni)))

    def test_no_emoji_code_points(self) -> None:
        for g in glyphs.GLYPHS.values():
            self.assertEqual(len(g.unicode), 1, g.name)
            cp = ord(g.unicode)
            self.assertFalse(0x1F000 <= cp <= 0x1FAFF, g.name)
            self.assertFalse(0x2300 <= cp <= 0x23FF, g.name)
            self.assertFalse(0x2600 <= cp <= 0x27BF, g.name)
            self.assertFalse(0x2B00 <= cp <= 0x2BFF, g.name)
            self.assertNotIn(0xFE0F, [ord(c) for c in g.unicode], g.name)

    def test_label_keeps_the_word(self) -> None:
        self.assertEqual(glyphs.label("pass", ascii_only=False), "■ pass")
        self.assertEqual(glyphs.label("fail", ascii_only=True), "[x] fail")


if __name__ == "__main__":
    unittest.main()
