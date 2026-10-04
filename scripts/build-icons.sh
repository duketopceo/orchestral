#!/usr/bin/env bash
# Regenerate the derived brand assets (U3). Dev-time only; outputs are committed.
#
#   scripts/build-icons.sh
#
# Hand-authored inputs (never overwritten):
#   ui/brand/mark.svg, mark-24.svg, mark-16.svg, ui/favicon.svg
# Generated outputs:
#   ui/brand/wordmark.svg, lockup-horizontal.svg, lockup-stacked.svg
#       Instrument Sans (ui/fonts, wght 600, wdth 90, tracking -1%) shaped with
#       uharfbuzz and OUTLINED to paths with fontTools, so the SVGs need no font
#       at runtime. A throwaway venv supplies fonttools + brotli + uharfbuzz.
#   ui/brand/apple-touch-icon.png (180), icon-512.png, ui/favicon.ico (16/32/48)
#       Mark in --ink (#121417) on a square --canvas (#F7F8F8) tile, no rounding.
#       Rasterised by resvg (no timestamps, no system fonts), optimised with
#       oxipng --strip all, ICO packed by ImageMagick (-strip, -define
#       icon:auto-resize off; frames are explicit). Re-running is byte-identical.
#
# Needs: bash, python3, resvg, oxipng, magick (ImageMagick 7), network for pip
# (unless fontTools+uharfbuzz are importable already).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRAND="$REPO_ROOT/ui/brand"
FONT="$REPO_ROOT/ui/fonts/InstrumentSans-latin-var.woff2"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for t in resvg oxipng magick python3; do
  command -v "$t" >/dev/null 2>&1 || { echo "missing tool: $t" >&2; exit 1; }
done

PY=python3
if ! python3 -c 'import fontTools, uharfbuzz, brotli' >/dev/null 2>&1; then
  python3 -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" -q install fonttools brotli uharfbuzz
  PY="$WORK/venv/bin/python"
fi

BRAND="$BRAND" FONT="$FONT" WORK="$WORK" "$PY" - <<'PYEOF'
import os, re
from pathlib import Path
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen

brand, work = Path(os.environ["BRAND"]), Path(os.environ["WORK"])
font = TTFont(os.environ["FONT"])
font = instancer.instantiateVariableFont(font, {"wght": 600, "wdth": 90})
ttf = work / "is-600-90.ttf"
font.flavor = None
font.save(ttf)
font = TTFont(ttf)
gs = font.getGlyphSet()
order = font.getGlyphOrder()
cap = font["OS/2"].sCapHeight
upm = font["head"].unitsPerEm

blob = hb.Blob(ttf.read_bytes())
hbfont = hb.Font(hb.Face(blob))
buf = hb.Buffer(); buf.add_str("orchestral"); buf.guess_segment_properties()
hb.shape(hbfont, buf, {"kern": True, "liga": False})
TRACK = -0.01 * upm
S = 100 / upm  # em = 100 units; y is flipped (font y-up -> svg y-down)

def fmt(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s

x, parts, bp = 0.0, [], BoundsPen(gs)
for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
    name = order[info.codepoint]
    t = (S, 0, 0, -S, (x + pos.x_offset) * S, 0)
    pen = SVGPathPen(gs, ntos=fmt)
    gs[name].draw(TransformPen(pen, t))
    gs[name].draw(TransformPen(bp, t))
    parts.append(pen.getCommands())
    x += pos.x_advance + TRACK
d = "".join(parts)
x0, y0, x1, y1 = bp.bounds  # svg space; baseline is y=0
cap_h = cap * S
pad = 0
W, H = x1 - x0, y1 - y0

def svg(vb, body, title="orchestral"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">\n'
            f'  <title>{title}</title>\n{body}\n</svg>\n')

word = f'<path fill="currentColor" d="{d}"/>'
(brand / "wordmark.svg").write_text(svg(
    f"{fmt(x0)} {fmt(y0)} {fmt(W)} {fmt(H)}",
    "  <!-- Instrument Sans SemiBold, wdth 90, tracking -1%, outlined by "
    "scripts/build-icons.sh. Plain t: the custom baton t is deferred. -->\n  " + word))

# Mark inner geometry, taken from the hand-authored master.
m = (brand / "mark.svg").read_text()
inner = re.sub(r"<!--.*?-->", "", re.search(r"</title>(.*)</svg>", m, re.S).group(1), flags=re.S)
inner = "\n".join("    " + l.strip() for l in inner.strip().splitlines())
MB = 1.9 * (-y0)           # mark box edge: 1.9 x ascender height
ms = MB / 24
gap = 0.5 * cap_h
# horizontal: mark box, gap, wordmark; vertically the box is centred on the
# wordmark's x-height-to-ascender block (its full ink height).
cy = y0 + H / 2
mx = 0.0
my = cy - MB / 2
wx = mx + MB + gap - x0
mark_g = f'  <g transform="translate({fmt(mx)} {fmt(my)}) scale({fmt(ms)})">\n{inner}\n  </g>'
word_g = f'  <g transform="translate({fmt(wx)} 0)">\n    {word}\n  </g>'
tot_w = wx + x1
vy0 = min(my, y0); vy1 = max(my + MB, y1)
(brand / "lockup-horizontal.svg").write_text(svg(
    f"0 {fmt(vy0)} {fmt(tot_w)} {fmt(vy1 - vy0)}", mark_g + "\n" + word_g))

# Stacked: mark centred above the wordmark, gap = 0.5 x cap height.
sw = W
mx2 = (sw - MB) / 2
my2 = y0 - gap - MB
mark2 = f'  <g transform="translate({fmt(mx2)} {fmt(my2)}) scale({fmt(ms)})">\n{inner}\n  </g>'
word2 = f'  <g transform="translate({fmt(-x0)} 0)">\n    {word}\n  </g>'
(brand / "lockup-stacked.svg").write_text(svg(
    f"0 {fmt(my2)} {fmt(sw)} {fmt(y1 - my2)}", mark2 + "\n" + word2))

# Tiles for rasterising: mark in --ink on a square --canvas, no rounding.
INK, PAPER = "#121417", "#F7F8F8"
def tile(src, size, frac):
    t = (brand / src if src != "favicon" else brand.parent / "favicon.svg").read_text()
    vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', t).group(1).split()]
    body = re.search(r"</title>(.*)</svg>", t, re.S).group(1)
    body = re.sub(r"<!--.*?-->|<style>.*?</style>", "", body, flags=re.S)
    body = body.replace("currentColor", INK).replace('class="i"', f'fill="{INK}" stroke="{INK}"')
    k = size * frac / vb[2]
    o = (size - vb[2] * k) / 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="{size}" height="{size}"><rect width="{size}" height="{size}" fill="{PAPER}"/>'
            f'<g transform="translate({fmt(o)} {fmt(o)}) scale({fmt(k)})">{body}</g></svg>')

tiles = {"t180": ("mark.svg", 180, 0.62), "t512": ("mark.svg", 512, 0.62),
         "t16": ("mark-16.svg", 16, 1.0), "t32": ("mark-24.svg", 32, 0.9),
         "t48": ("mark.svg", 48, 0.8)}
for k, (src, size, frac) in tiles.items():
    (work / f"{k}.svg").write_text(tile(src, size, frac))
PYEOF

for k in t180 t512 t16 t32 t48; do
  resvg "$WORK/$k.svg" "$WORK/$k.png"
  oxipng -q --strip all -o 4 "$WORK/$k.png"
done
cp "$WORK/t180.png" "$BRAND/apple-touch-icon.png"
cp "$WORK/t512.png" "$BRAND/icon-512.png"
magick "$WORK/t16.png" "$WORK/t32.png" "$WORK/t48.png" -strip "$REPO_ROOT/ui/favicon.ico"
echo "built: wordmark, lockups, apple-touch-icon.png, icon-512.png, favicon.ico"
