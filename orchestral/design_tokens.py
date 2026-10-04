"""Design tokens for Python surfaces (The Score, KTD2).

``ui/tokens.css`` is the only token source. This module parses its paper and
stage custom-property blocks so ``render.py``, ``reporter.py`` and the Textual
themes never carry a palette of their own. If ``ui/`` is absent (installed
wheel, odd checkout) a minimal embedded fallback keeps callers working; a test
pins the fallback to the CSS so the two cannot drift.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1] / "ui"

THEMES = ("paper", "stage")

# Subset of ui/tokens.css; keep in sync (tests enforce it).
FALLBACK: dict[str, dict[str, str]] = {
    "paper": {
        "--canvas": "#F7F8F8", "--surface": "#FFFFFF", "--rule": "#E2E5E8",
        "--ink": "#121417", "--ink-2": "#4A5159", "--ink-3": "#69717A",
        "--pass-text": "#00775A", "--fail-text": "#B04A00",
        "--judge-text": "#005A8F", "--live-text": "#8A5F00",
    },
    "stage": {
        "--canvas": "#0E1012", "--surface": "#15181B", "--rule": "#262B30",
        "--ink": "#ECEEF0", "--ink-2": "#A9B0B8", "--ink-3": "#838B94",
        "--pass-text": "#2EC495", "--fail-text": "#F2813F",
        "--judge-text": "#6CC0EE", "--live-text": "#F0B429",
    },
}

_PAPER_SEL = ":root"
_STAGE_SEL = ':root[data-theme="stage"]'
_DARK_MEDIA = "@media (prefers-color-scheme: dark)"
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def _decls(body: str) -> dict[str, str]:
    return {k: " ".join(v.split()) for k, v in _DECL.findall(body)}


def parse_blocks(css: str) -> dict[str, dict[str, str]]:
    """Map selector -> custom properties. Rules inside the dark
    ``prefers-color-scheme`` media query are keyed ``@dark <selector>``."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: dict[str, dict[str, str]] = {}
    pos = 0
    while True:
        m = css.find(_DARK_MEDIA, pos)
        if m < 0:
            break
        start = css.index("{", m)
        depth, i = 0, start
        while i < len(css):
            depth += css[i] == "{"
            depth -= css[i] == "}"
            i += 1
            if depth == 0:
                break
        for sel, body in _RULE.findall(css[start + 1:i - 1]):
            out[f"@dark {sel.strip()}"] = _decls(body)
        css = css[:m] + css[i:]
        pos = m
    for sel, body in _RULE.findall(css):
        out.setdefault(sel.strip(), {}).update(_decls(body))
    return out


@lru_cache(maxsize=8)
def _load(path: str) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    blocks = parse_blocks(Path(path).read_text(encoding="utf-8"))
    paper, stage = blocks.get(_PAPER_SEL), blocks.get(_STAGE_SEL)
    if not paper or not stage:
        raise ValueError("tokens.css is missing a paper or stage block")
    return (("paper", tuple(paper.items())), ("stage", tuple(stage.items())))


def load(ui_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Both themes' tokens as ``{"paper": {"--canvas": ...}, "stage": {...}}``.

    Never raises: a missing or unparseable ``tokens.css`` yields the embedded
    fallback."""
    path = (ui_dir or UI_DIR) / "tokens.css"
    try:
        return {name: dict(items) for name, items in _load(str(path))}
    except (OSError, ValueError):
        return {name: dict(vals) for name, vals in FALLBACK.items()}


def theme(name: str, ui_dir: Path | None = None) -> dict[str, str]:
    return load(ui_dir)[name]


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio between two ``#rrggbb`` colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# DESIGN.md 6.9 item 9: categorical series, Okabe-Ito order, at most 7 hues.
# An eighth series is labelled without a new hue.
CATEGORICAL = ("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442")

# Font stacks and spacing from ui/tokens.css; self-hosted faces are not
# inlined (single-file pages), so they fall back to system stacks.
_FONTS = (
    '  --font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;\n'
    '  --font-mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;\n'
    "  --s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-5: 24px;\n"
    "  --r-control: 2px;\n"
)

TOKEN_START = "/* tokens:start */"
TOKEN_END = "/* tokens:end */"
_TOKEN_BLOCK = re.compile(re.escape(TOKEN_START) + r".*?" + re.escape(TOKEN_END), re.S)


def _decl_text(vals: dict[str, str], indent: str) -> str:
    return "".join(f"{indent}{k}: {v};\n" for k, v in vals.items())


def css_block(themes: tuple[str, ...] = THEMES, categorical: bool = False,
              ui_dir: Path | None = None) -> str:
    """Self-contained custom-property CSS for single-file pages.

    Paper is ``:root``. Stage, when requested, applies for the OS dark
    preference (unless ``data-theme="paper"``) and for an explicit
    ``data-theme="stage"``, so it works with JavaScript off. The block is
    fenced by ``TOKEN_START``/``TOKEN_END`` so tests can strip it, and it is
    the only place a page may carry colour literals."""
    tokens = load(ui_dir)
    parts = [TOKEN_START, ":root {\n  color-scheme: light;\n" + _FONTS + _decl_text(tokens["paper"], "  ")]
    if categorical:
        parts[-1] += "".join(f"  --cat-{i}: {c};\n" for i, c in enumerate(CATEGORICAL, 1))
    parts[-1] += "}"
    if "stage" in themes:
        stage = _decl_text(tokens["stage"], "    ")
        parts.append(
            f'{_DARK_MEDIA} {{\n  :root:not([data-theme="paper"]) {{\n    color-scheme: dark;\n{stage}  }}\n}}'
        )
        parts.append(
            ':root[data-theme="stage"] {\n  color-scheme: dark;\n'
            + _decl_text(tokens["stage"], "  ") + "}"
        )
    parts.append(TOKEN_END)
    return "\n".join(parts)


def strip_token_block(text: str) -> str:
    """``text`` without the inlined token block (for colour-literal tests)."""
    return _TOKEN_BLOCK.sub("", text)


_SYMBOL = re.compile(r'<symbol\b[^>]*\bid="([^"]+)".*?</symbol>', re.S)


def sprite(ids: tuple[str, ...], ui_dir: Path | None = None) -> str:
    """Hidden inline ``<svg>`` holding only the named ``ui/icons.svg`` symbols.

    A missing sprite or id yields an empty string so a page still renders."""
    try:
        text = ((ui_dir or UI_DIR) / "icons.svg").read_text(encoding="utf-8")
    except OSError:
        return ""
    found = {m.group(1): m.group(0) for m in _SYMBOL.finditer(text)}
    body = "".join(found[i] for i in ids if i in found)
    if not body:
        return ""
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute" aria-hidden="true">{body}</svg>'
