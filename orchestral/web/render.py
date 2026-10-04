"""Server-rendered error pages for the observatory's non-API paths.

The UI itself is the static SPA in ``ui/`` (app.html + app.css + app.js).
These pages are single-file and need nothing else: the design tokens
(``ui/tokens.css`` via ``design_tokens.py``) and one Rest drawing from
``ui/icons.svg`` are inlined, nothing is fetched, and no JavaScript runs.
Paper is the default; the stage theme follows the OS preference, so both
themes work with scripting off (DESIGN.md 7.11, A7).
"""

from __future__ import annotations

from orchestral import design_tokens


def esc(s: object) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_CSS = """
  *, *::before, *::after { box-sizing: border-box; }
  html { background: var(--canvas); color: var(--ink); }
  body {
    margin: 0; min-height: 100vh; background: var(--canvas); color: var(--ink);
    font: 16px/1.5 var(--font-sans, ui-sans-serif, system-ui, sans-serif);
    display: grid; place-items: center; padding: var(--s-5, 24px) var(--s-4, 16px);
  }
  main { width: 100%; max-width: 32rem; }
  .rest { width: 64px; height: 64px; color: var(--ink-3); display: block; margin-bottom: var(--s-4, 16px); }
  .status { margin: 0; font: 500 13px/1.4 var(--font-mono, ui-monospace, monospace); color: var(--ink-3); letter-spacing: .04em; }
  h1 { margin: var(--s-1, 4px) 0 var(--s-3, 12px); font-size: 28px; line-height: 1.2; font-weight: 600; }
  p { margin: 0 0 var(--s-3, 12px); color: var(--ink-2); }
  code {
    font-family: var(--font-mono, ui-monospace, monospace); font-size: 14px; color: var(--ink);
    background: var(--sunken); border: 1px solid var(--rule); padding: 1px 6px; overflow-wrap: anywhere;
  }
  .detail { color: var(--fail-text); overflow-wrap: anywhere; }
  .actions { display: flex; flex-wrap: wrap; gap: var(--s-3, 12px); margin-top: var(--s-5, 24px); }
  .actions a {
    display: inline-block; padding: 8px 14px; min-height: 40px; line-height: 24px;
    border: 1px solid var(--control-border); border-radius: var(--r-control, 2px);
    color: var(--ink); text-decoration: none; background: var(--surface);
  }
  .actions a.primary { background: var(--ink); color: var(--on-ink); border-color: var(--ink); }
  .actions a:hover { border-color: var(--ink); }
  a:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; box-shadow: 0 0 0 4px var(--focus-halo); }
"""


def _page(status: int, title: str, rest: str, heading: str, body: str,
          actions: list[tuple[str, str]]) -> str:
    last = len(actions) - 1
    links = "".join(
        '<a href="{}"{}>{}</a>'.format(href, ' class="primary"' if i == last else "", esc(label))
        for i, (label, href) in enumerate(actions)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="color-scheme" content="light dark" />
  <title>{esc(title)} | orchestral</title>
  <style>
{design_tokens.css_block()}
{_CSS}
  </style>
</head>
<body>
{design_tokens.sprite((rest,))}
<main>
  <svg class="rest" viewBox="0 0 64 64" role="img" aria-label="{esc(heading)}"><use href="#{rest}"/></svg>
  <p class="status">{status}</p>
  <h1>{esc(heading)}</h1>
{body}
  <div class="actions">{links}</div>
</main>
</body>
</html>"""


def render_not_found(what: str) -> str:
    return _page(
        404, "Not found", "r-missing", "Not found",
        f"  <p>Nothing lives at <code>{esc(what)}</code>. The run may have been "
        "removed, or the link is mistyped.</p>",
        [("Search runs", "/#/runs"), ("Back to overview", "/")],
    )


def render_server_error(msg: str) -> str:
    return _page(
        500, "Server error", "r-error", "Something broke on our side",
        f"  <p class='detail'>{esc(msg)}</p>\n"
        "  <p>Nothing was changed. Reload the page, or check the terminal running "
        "<code>harness.py serve</code> for the cause.</p>",
        [("Search runs", "/#/runs"), ("Back to overview", "/")],
    )


def render_bad_request(msg: str) -> str:
    return _page(
        400, "Bad request", "r-error", "That request did not parse",
        f"  <p class='detail'>{esc(msg)}</p>",
        [("Back to overview", "/")],
    )
