#!/usr/bin/env bash
# Regenerate ui/fonts/*.woff2 (KTD12). Dev-time only: fonttools is not a
# runtime or dev-extra dependency. Downloads pinned upstream sources into a
# temp dir, subsets with pyftsubset, writes woff2 + the OFL texts into ui/fonts/.
#
#   scripts/subset-fonts.sh
#
# Needs: bash, curl, tar, npm, python3 (a throwaway venv supplies fonttools +
# brotli unless pyftsubset is already on PATH).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/ui/fonts"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

IS_COMMIT=7fa22308a3d0c94ee2b3cd537a1196b65db34a3e   # Instrument/instrument-sans master, 2026-10-03
IS_RAW="https://raw.githubusercontent.com/Instrument/instrument-sans/$IS_COMMIT"
PLEX_VERSION=2.5.0                                    # npm @ibm/plex-mono

if ! command -v pyftsubset >/dev/null 2>&1; then
  python3 -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" -q install fonttools brotli
  export PATH="$WORK/venv/bin:$PATH"
fi

# Latin, Latin-1, general punctuation, arrows, and the math/symbols the UI
# prints (minus, <=, >=, ~=, !=, delta, check). Glyphs a family lacks are
# skipped silently and fall through to the next family in the stack.
UNICODES="U+0020-007E,U+00A0-00FF,U+2010-2015,U+2018-201F,U+2022,U+2026,U+2030,U+2032-2033,U+20AC,U+2122,U+2190-2199,U+2206,U+2212,U+2215,U+2248,U+2260,U+2264-2265,U+2713"
FEATURES="kern,liga,calt,ccmp,locl,mark,mkmk,tnum,zero,case,ss01"

curl -fsSL "$IS_RAW/fonts/variable/InstrumentSans%5Bwdth,wght%5D.ttf" -o "$WORK/is.ttf"
curl -fsSL "$IS_RAW/OFL.txt" -o "$WORK/OFL-instrument-sans.txt"
(cd "$WORK" && npm pack "@ibm/plex-mono@$PLEX_VERSION" >/dev/null && mkdir plex && tar xzf ibm-plex-mono-*.tgz -C plex)

mkdir -p "$OUT"
# Limit the variable axes to what DESIGN.md 6.2 uses (wght 400-600, wdth 80-100).
fonttools varLib.instancer "$WORK/is.ttf" wght=400:600 wdth=80:100 -o "$WORK/is-lim.ttf" -q
pyftsubset "$WORK/is-lim.ttf" --unicodes="$UNICODES" --layout-features="$FEATURES" \
  --flavor=woff2 --no-hinting --desubroutinize --output-file="$OUT/InstrumentSans-latin-var.woff2"

for w in Regular Medium SemiBold; do
  pyftsubset "$WORK/plex/package/fonts/complete/woff2/IBMPlexMono-$w.woff2" \
    --unicodes="$UNICODES" --layout-features="$FEATURES" \
    --flavor=woff2 --no-hinting --desubroutinize --output-file="$OUT/IBMPlexMono-$w-latin.woff2"
done

cp "$WORK/OFL-instrument-sans.txt" "$OUT/OFL-InstrumentSans.txt"
cp "$WORK/plex/package/LICENSE.txt" "$OUT/OFL-IBMPlexMono.txt"
ls -l "$OUT"
