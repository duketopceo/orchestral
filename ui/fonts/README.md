# Self-hosted typefaces

Both families are SIL Open Font License 1.1; licence texts sit beside the
fonts (`OFL-*.txt`). Regenerate everything with `scripts/subset-fonts.sh`.

| File | Family | Source | Version |
|---|---|---|---|
| `InstrumentSans-latin-var.woff2` | Instrument Sans, variable (wght 400-600, wdth 80-100) | https://github.com/Instrument/instrument-sans `fonts/variable/InstrumentSans[wdth,wght].ttf` | commit 7fa22308a3d0c94ee2b3cd537a1196b65db34a3e |
| `IBMPlexMono-{Regular,Medium,SemiBold}-latin.woff2` | IBM Plex Mono 400 / 500 / 600 | npm `@ibm/plex-mono` (IBM/plex release build) | 2.5.0 |

Subset (pyftsubset, woff2): Basic Latin, Latin-1, general punctuation, arrows
U+2190-2199, minus, delta, approx/not-equal/le/ge, check mark. Layout features
kept: kern, liga, calt, ccmp, locl, mark, mkmk, tnum, zero, case, ss01.
Instrument Sans has no <=, >=, approx, not-equal, +/-, delta or check glyphs;
those characters fall through to the system fallback in the font stack.
