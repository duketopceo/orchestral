"""Textual themes for the Score (U17): paper and stage, from ``ui/tokens.css``.

Colours are read through ``orchestral.design_tokens`` and never written here, so
the TUI cannot drift from the web observatory. Role mapping (the web token it
reads, the Textual slot it fills):

    --canvas -> background     --surface -> surface    --sunken -> panel
    --ink -> foreground        --ink-2 -> secondary / $text-muted
    --judge-text -> primary and accent
    --pass-text -> success     --fail-text -> error    --live-text -> warning
"""

from __future__ import annotations

from textual.theme import Theme

from orchestral import design_tokens

THEME_NAMES = ("orchestral-paper", "orchestral-stage")
DEFAULT_THEME = "orchestral-stage"


def build_theme(name: str) -> Theme:
    key = name.removeprefix("orchestral-")
    if key not in design_tokens.THEMES:
        raise ValueError(f"unknown theme {name!r}")
    t = design_tokens.theme(key)
    return Theme(
        name=name,
        primary=t["--judge-text"],
        secondary=t["--ink-2"],
        accent=t["--judge-text"],
        success=t["--pass-text"],
        error=t["--fail-text"],
        warning=t["--live-text"],
        foreground=t["--ink"],
        background=t["--canvas"],
        surface=t["--surface"],
        panel=t.get("--sunken", t["--surface"]),
        dark=key == "stage",
        variables={
            "text-muted": t["--ink-2"],
            "text-disabled": t["--ink-3"],
            "border": t.get("--rule-strong", t["--rule"]),
            "border-blurred": t["--rule"],
            "footer-background": t.get("--sunken", t["--surface"]),
            "footer-key-foreground": t["--ink"],
            "footer-description-foreground": t["--ink-2"],
            "block-cursor-background": t["--ink"],
            "block-cursor-foreground": t.get("--on-ink", t["--canvas"]),
        },
    )


def other(name: str) -> str:
    """The theme the toggle binding switches to."""
    return THEME_NAMES[0] if name == THEME_NAMES[1] else THEME_NAMES[1]


def tokens_for(name: str) -> dict[str, str]:
    """The web tokens behind a registered theme (for Rich cell styles)."""
    key = name.removeprefix("orchestral-")
    return design_tokens.theme(key if key in design_tokens.THEMES else "stage")
