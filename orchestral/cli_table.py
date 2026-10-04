"""The Score, U18: CLI tables that fit, degrade cleanly and share the vocabulary.

One helper builds every table the CLI prints. It owns three decisions so no
command has to:

* **Style.** Colour only when stdout is a TTY, ``NO_COLOR`` is unset and
  ``TERM`` is not ``dumb``. Unicode glyphs and rules only on a TTY whose
  encoding is UTF-8 and where ``TERM`` is not ``dumb`` and ``ORCH_ASCII`` is
  unset; everything else (pipes, files, CI logs) gets the ASCII fallbacks from
  ``orchestral/glyphs.py``. ``NO_COLOR`` alone keeps the glyphs, because shape
  and word carry the verdict, not hue (DESIGN.md 6.8).
* **Width.** Columns carry a priority. When the table is wider than the
  terminal the highest priority number is dropped first, so the verdict, task,
  pairing, cost and n columns survive and latency and tokens go. A piped table
  has no width unless ``COLUMNS`` is set, so scripts never lose a column.
* **Cells.** Money, percent, token and duration text comes from
  ``orchestral/format.py``. The compact duration here has no internal space
  (``66.8m``, not ``1h 07m``) so piped columns stay space-separated.

Display only: nothing here touches stored or exported numbers. The plan names
``rich.table.Table``; the rendering here is line-based instead because the
golden tests need output that does not move with a Rich release. Rich still
draws the colour (``rich.console.Console`` and ``rich.text.Text``).
"""

from __future__ import annotations

import codecs
import contextlib
import io
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from orchestral.format import NULL_GLYPH, _fixed, fmt_duration_ms
from orchestral.glyphs import GLYPHS, glyph, label

GUTTER = 2
MIN_SHRINK = 8

# Colour roles from ui/tokens.css mapped to the terminal's own palette.
_ROLE_STYLE = {
    "pass": "green", "fail": "red", "live": "yellow", "judge": "blue",
    "ink-2": "dim", "ink-3": "dim", "ink": "",
}


@dataclass(frozen=True)
class Style:
    unicode: bool
    color: bool
    width: int | None  # None: never drop or shrink (piped output)


def detect_style(stream: IO[str] | None = None, environ: Mapping[str, str] | None = None) -> Style:
    """Pick glyph set, colour and width for ``stream`` (default stdout)."""
    stream = stream if stream is not None else sys.stdout
    env = os.environ if environ is None else environ
    tty = bool(getattr(stream, "isatty", lambda: False)())
    dumb = env.get("TERM") == "dumb"
    color = tty and not env.get("NO_COLOR") and not dumb
    utf8 = False
    with contextlib.suppress(LookupError):
        utf8 = codecs.lookup(getattr(stream, "encoding", None) or "ascii").name == "utf-8"
    uni = tty and utf8 and not dumb and not env.get("ORCH_ASCII")
    width: int | None = None
    cols = env.get("COLUMNS", "")
    if cols.isdigit() and int(cols) > 0:
        width = int(cols)
    elif tty:
        width = shutil.get_terminal_size((80, 24)).columns
    return Style(unicode=bool(uni), color=bool(color), width=width)


def fmt_duration_compact(ms: float | None) -> str:
    """612ms, 12.3s, 7.4m, 66.8m. No internal space, so columns stay splittable."""
    if ms is None or ms < 0:
        return NULL_GLYPH
    if ms < 59_950:
        return fmt_duration_ms(ms)
    return f"{_fixed(ms / 60_000, 1)}m"


Cell = str | tuple[str, str] | None


def verdict_cell(passes: bool | None, style: Style) -> tuple[str, str]:
    """``■ pass`` / ``□ fail`` (``[+] pass`` / ``[x] fail`` in ASCII); null when unjudged."""
    if passes is None:
        return NULL_GLYPH, "ink-3"
    name = "pass" if passes else "fail"
    return label(name, ascii_only=not style.unicode), GLYPHS[name].role


def n_cell(n: int, low: bool, style: Style) -> tuple[str, str]:
    """The sample count, with the ``low n`` glyph and word when it is thin."""
    if not low:
        return str(n), "ink"
    return f"{n} {label('low-n', ascii_only=not style.unicode)}", GLYPHS["low-n"].role


def format_error(summary: str, cause: str, fix: str) -> str:
    """Summary, cause and the fix command, each on its own line."""
    return f"error: {summary}\ncause: {cause}\nfix: {fix}"


@dataclass(frozen=True)
class Column:
    header: str
    align: str = "l"          # "l" or "r"
    priority: int = 0         # 0 is never dropped; higher numbers drop first
    max_width: int | None = None


@dataclass(frozen=True)
class Note:
    """A full-width line between rows (for example the unranked divider)."""
    text: str


def _truncate(text: str, width: int, style: Style) -> str:
    if cell_len(text) <= width:
        return text
    mark = "…" if style.unicode else "..."
    keep = max(width - cell_len(mark), 0)
    out = ""
    for ch in text:
        if cell_len(out + ch) > keep:
            break
        out += ch
    return out + mark


def _pad(text: str, width: int, align: str) -> str:
    gap = max(width - cell_len(text), 0)
    return " " * gap + text if align == "r" else text + " " * gap


def _fit(columns: Sequence[Column], widths: list[int], limit: int | None) -> list[int]:
    """Indexes of the columns kept, dropping highest priority first, then shrinking."""
    keep = list(range(len(columns)))
    if limit is None:
        return keep

    def total() -> int:
        return sum(widths[i] for i in keep) + GUTTER * (len(keep) - 1)

    while total() > limit:
        droppable = [i for i in keep if columns[i].priority > 0]
        if not droppable:
            break
        keep.remove(max(droppable, key=lambda i: (columns[i].priority, i)))
    while total() > limit:
        shrinkable = [i for i in keep if widths[i] > MIN_SHRINK and columns[i].max_width]
        if not shrinkable:
            break
        widest = max(shrinkable, key=lambda i: widths[i])
        widths[widest] -= 1
    return keep


def render_table(columns: Sequence[Column], rows: Sequence[Sequence[Cell] | Note], style: Style) -> str:
    """Render to text. Rows hold ``str``, ``None`` (null glyph) or ``(text, role)``."""
    def text_of(c: Cell) -> str:
        if c is None:
            return NULL_GLYPH
        return c[0] if isinstance(c, tuple) else c

    data = [r for r in rows if not isinstance(r, Note)]
    widths = []
    for i, col in enumerate(columns):
        w = max([cell_len(col.header)] + [cell_len(text_of(r[i])) for r in data])
        widths.append(min(w, col.max_width) if col.max_width else w)
    keep = _fit(columns, widths, style.width)
    total = sum(widths[i] for i in keep) + GUTTER * (len(keep) - 1)
    rule_ch = "─" if style.unicode else "-"
    lines: list[list[tuple[str, str]]] = []

    def line(cells: list[tuple[str, str]]) -> None:
        lines.append(cells)

    line([(_pad(columns[i].header, widths[i], columns[i].align), "bold") for i in keep])
    line([(rule_ch * total, "dim")])
    for r in rows:
        if isinstance(r, Note):
            line([(_truncate(r.text, style.width or total, style), "dim")])
            continue
        cells = []
        for i in keep:
            c = r[i]
            text = _truncate(text_of(c), widths[i], style)
            role = c[1] if isinstance(c, tuple) else ("ink-3" if c is None else "ink")
            cells.append((_pad(text, widths[i], columns[i].align), _ROLE_STYLE.get(role, role)))
        line(cells)

    out = []
    for cells in lines:
        if style.color:
            out.append(_paint(cells))
        else:
            out.append(("  ".join(t for t, _ in cells)).rstrip())
    return "\n".join(out)


def _paint(cells: list[tuple[str, str]]) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", width=10_000,
                      legacy_windows=False, highlight=False)
    text = Text()
    for n, (t, st) in enumerate(cells):
        if n:
            text.append(" " * GUTTER)
        text.append(t, style=st or None)
    text.rstrip()
    console.print(text, end="", soft_wrap=True, markup=False)
    return buf.getvalue()


def print_table(columns: Sequence[Column], rows: Sequence[Sequence[Cell] | Note],
                style: Style | None = None) -> None:
    print(render_table(columns, rows, style or detect_style()))


__all__ = [
    "Cell", "Column", "Note", "Style", "detect_style", "fmt_duration_compact", "format_error",
    "glyph", "n_cell", "print_table", "render_table", "verdict_cell",
]
