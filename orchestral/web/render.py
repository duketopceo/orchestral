"""Minimal HTML for the observatory's non-API error paths.

The UI itself is the static SPA in ``ui/`` (app.html + app.css + app.js);
this module only renders plain-text-safe error pages for non-API 404/500s.
"""

from __future__ import annotations


def esc(s: object) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>orchestral — {esc(title)}</title>
  <style>
    body {{ background:#0b0d10; color:#d7dee7; font:14px system-ui,sans-serif; padding:3rem; }}
    a {{ color:#22d3ee; }}
    .muted {{ color:#8b97a6; }}
    .err {{ color:#f87171; }}
  </style>
</head>
<body>{body}</body>
</html>"""


def render_not_found(what: str) -> str:
    return _page("not found", f"<h1>not found</h1><p class='muted'>{esc(what)}</p>")


def render_bad_request(msg: str) -> str:
    return _page("bad request", f"<h1>bad request</h1><p class='err'>{esc(msg)}</p>")
