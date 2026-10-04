"""The Score, U4: terminal glyph parity table (DESIGN.md 6.8).

Every symbol in ``ui/icons.svg`` ("Engraved 16", ``#i-<name>``) has one row
here, so the TUI, the CLI and plain-text reports speak the same vocabulary as
the web. Verdicts and states are encoded by shape and fill, never by colour
alone: each row carries a Unicode glyph, a pure-ASCII fallback (for ``TERM=dumb``,
CI logs and non-UTF-8 locales) and a word that always travels with the glyph.

``role`` names a colour role from ``ui/tokens.css`` (``pass``, ``fail``,
``live``, ``judge``, ``ink``, ``ink-2``, ``ink-3``). Callers apply colour only
when ``isatty`` and ``NO_COLOR`` is unset; the glyph and word carry the meaning
without it. No glyph here is an emoji code point.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Glyph:
    name: str  # sprite id without the ``i-`` prefix
    meaning: str
    unicode: str
    ascii: str
    word: str
    role: str

    @property
    def symbol_id(self) -> str:
        return f"i-{self.name}"


def _g(name: str, meaning: str, uni: str, asc: str, word: str, role: str) -> Glyph:
    return Glyph(name, meaning, uni, asc, word, role)


# Verdicts and states (DESIGN.md 6.8 table, plus ``stalled`` from KTD8).
# Shapes differ pairwise; ``stalled`` (diamond) differs from ``live`` (ring).
_STATES: tuple[Glyph, ...] = (
    _g("pass", "mechanical check passed", "■", "[+]", "pass", "pass"),
    _g("fail", "mechanical check failed", "□", "[x]", "fail", "fail"),
    _g("cancelled", "run cancelled", "┄", "[-]", "cancelled", "ink-2"),
    _g("live", "run in progress", "◔", "(*)", "live", "live"),
    _g("stalled", "no event for 10 min, no live owner", "◇", "<!>", "stalled", "live"),
    _g("inconclusive", "evidence does not decide", "◧", "[~]", "inconclusive", "ink-2"),
    _g("not-judged", "no judge score", "○", "( )", "not judged", "ink-3"),
    _g("judge", "judge score (gauge, then number)", "◖", "g", "judge", "judge"),
    _g("low-n", "too few samples (hatch)", "░", "[.]", "low n", "ink-2"),
    _g("flag-interesting", "flagged: hold here (fermata)", "※", "*", "flagged", "ink"),
)

STATE_NAMES: tuple[str, ...] = tuple(g.name for g in _STATES)

# Remaining icons: plain instrument iconography with terminal stand-ins.
_ICONS: tuple[Glyph, ...] = (
    _g("overview", "overview", "▦", "#", "overview", "ink"),
    _g("runs", "runs", "≡", "=", "runs", "ink"),
    _g("leaderboard", "leaderboard", "▆", "|", "leaderboard", "ink"),
    _g("compare", "compare", "⇄", "<>", "compare", "ink"),
    _g("cards", "cards", "▤", "[=]", "cards", "ink"),
    _g("models", "models", "◈", "<#>", "models", "ink"),
    _g("help", "about / help", "⁇", "?", "help", "ink"),
    _g("new-run", "new run", "⊕", "+", "new run", "ink"),
    _g("cost", "cost", "¤", "$", "cost", "ink-2"),
    _g("latency", "latency", "◷", "t", "latency", "ink-2"),
    _g("tokens", "tokens", "≫", ">>", "tokens", "ink-2"),
    _g("flag-dismiss", "dismissed (rest)", "▬", "-", "dismissed", "ink-3"),
    _g("artifact", "artifact", "▯", "[]", "artifact", "ink"),
    _g("transcript", "transcript", "≣", "==", "transcript", "ink"),
    _g("plan", "plan (staff with ticks)", "╪", "+-", "plan", "ink"),
    _g("manifest", "manifest / hash", "‡", "##", "manifest", "ink"),
    _g("external", "external link", "↗", "->", "open", "ink"),
    _g("download", "download", "↓", "v", "download", "ink"),
    _g("copy", "copy", "⧉", "cp", "copy", "ink"),
    _g("filter", "filter", "▽", "Y", "filter", "ink"),
    _g("search", "search", "◍", "/", "search", "ink"),
    _g("sort", "sort", "⇅", "^v", "sort", "ink"),
    _g("chevron", "expand / collapse", "▾", "v", "expand", "ink"),
    _g("close", "close", "×", "x", "close", "ink"),
    _g("keyboard", "keyboard shortcuts", "▭", "kbd", "keys", "ink"),
    _g("theme", "theme toggle", "◐", "o|", "theme", "ink"),
)

GLYPHS: dict[str, Glyph] = {g.name: g for g in _STATES + _ICONS}


def glyph(name: str, *, ascii_only: bool | None = None) -> str:
    """The glyph for ``name``; ASCII when asked or when ``ORCH_ASCII`` is set."""
    row = GLYPHS[name]
    if ascii_only is None:
        ascii_only = bool(os.environ.get("ORCH_ASCII"))
    return row.ascii if ascii_only else row.unicode


def label(name: str, *, ascii_only: bool | None = None) -> str:
    """Glyph plus word, e.g. ``"■ pass"``; the word is never dropped."""
    return f"{glyph(name, ascii_only=ascii_only)} {GLYPHS[name].word}"
