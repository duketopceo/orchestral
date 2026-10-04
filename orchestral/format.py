"""One formatting contract for every surface (KTD3).

``ui/js/format.js`` implements the same functions; both are driven by
``tests/fixtures/format_cases.json``, so the web UI, the TUI, the CLI and
payload copy cannot drift. Display only: stored and exported numbers keep their
full precision.

Rounding is half-up on the exact binary value, which is what JavaScript's
``toFixed`` does, so the two implementations agree on exact ties.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

# Plain-text null marker. The web UI draws the designed null glyph (a short rule
# with aria-label "no data"); everywhere else this is the text stand-in. It is
# never an em dash.
NULL_GLYPH = "-"

# Sample-size thresholds, published so the SPA cannot drift from them.
LOW_N_CELL = 3    # a matrix/heatmap cell with fewer runs is flagged "Low n"
LOW_N_BEST = 10   # a pairing needs this many runs before it may be called "best"

SLUG_MAX = 36
_SLUG_TAIL = 14


def _fixed(v: float, places: int) -> str:
    """``toFixed`` semantics: round half up on the exact value, sign preserved."""
    d = Decimal(abs(v)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{'-' if v < 0 and d != 0 else ''}{d:f}"


def _round_half_up(v: float) -> int:
    return math.floor(v + 0.5)


def fmt_money(v: float | None) -> str:
    """$0.0072 below $0.01, $0.187 below $1, $12.40 above; negatives for deltas."""
    if v is None:
        return NULL_GLYPH
    if v < 0:
        return "-" + fmt_money(-v)
    if v == 0:
        return "$0.00"
    if v < 0.00005:
        return "<$0.0001"
    if v < 0.01:
        s = _fixed(v, 4)
        if float(s) < 0.01:
            return f"${s}"
    if v < 1:
        s = _fixed(v, 3)
        if float(s) < 1:
            return f"${s}"
    whole, frac = _fixed(v, 2).split(".")
    return f"${int(whole):,}.{frac}"


def fmt_percent(v: float | None) -> str:
    """A rate in 0..1 as an integer percent."""
    return NULL_GLYPH if v is None else f"{_round_half_up(v * 100)}%"


def fmt_score(v: float | None) -> str:
    """A 0..1 score with two decimals."""
    return NULL_GLYPH if v is None else _fixed(v, 2)


def fmt_range_pct(lo: float | None, hi: float | None) -> str:
    """An interval as '49-94%' (ranges use a hyphen)."""
    if lo is None or hi is None:
        return NULL_GLYPH
    return f"{_round_half_up(lo * 100)}-{_round_half_up(hi * 100)}%"


def fmt_duration_ms(ms: float | None) -> str:
    """450ms, 12.3s, 4m 05s, 2h 03m. Negative or missing is the null glyph."""
    if ms is None or ms < 0:
        return NULL_GLYPH
    if ms < 1000:
        return f"{_round_half_up(ms)}ms"
    if ms < 60_000 and float(_fixed(ms / 1000, 1)) < 60:
        return f"{_fixed(ms / 1000, 1)}s"
    secs = _round_half_up(ms / 1000)
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {secs % 3600 // 60:02d}m"


def fmt_tokens(n: float | None) -> str:
    """999, 12.3k, 1.5M."""
    if n is None:
        return NULL_GLYPH
    if n < 1000:
        return str(_round_half_up(n))
    if n < 999_950:
        return f"{_fixed(n / 1000, 1)}k"
    return f"{_fixed(n / 1_000_000, 1)}M"


def fmt_delta(v: float | None, unit: str) -> str:
    """A signed difference. unit: 'pp' (rate difference), 'score', or 'money'."""
    if v is None:
        return NULL_GLYPH
    if unit == "pp":
        n = _round_half_up(v * 100)
        return f"{'+' if n > 0 else ''}{n}pp"
    if unit == "score":
        s = _fixed(v, 2)
        return f"+{s}" if float(s) > 0 else s
    if unit == "money":
        s = fmt_money(v)
        return f"+{s}" if v > 0 else s
    raise ValueError(f"unknown delta unit: {unit!r}")


def short_slug(slug: str | None, max_len: int = SLUG_MAX) -> str:
    """Drop the vendor prefix; keep the model and version. Long names are cut in
    the middle so the tail (usually the version) survives."""
    if not slug:
        return NULL_GLYPH
    name = slug.rsplit("/", 1)[-1]
    if len(name) <= max_len:
        return name
    return name[: max_len - 1 - _SLUG_TAIL] + "…" + name[-_SLUG_TAIL:]


def is_low_n_cell(n: int | None) -> bool:
    return n is None or n < LOW_N_CELL


def is_low_n_best(n: int | None) -> bool:
    return n is None or n < LOW_N_BEST
