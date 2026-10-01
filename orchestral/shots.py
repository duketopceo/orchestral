"""Playwright screenshot capture for HTML artifacts.

Optional: playwright is only imported inside the capture call so the base
install never needs it. Missing package or browser binaries raise
ScreenshotUnavailable, which callers are expected to degrade on.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs


class ScreenshotUnavailable(Exception):
    """Raised when Playwright or its browser binaries are not installed."""


def shot_name(route: str, *, stamp: str | None = None) -> str:
    """Deterministic download filename for a captured app route —
    ``orchestral-pairing-x-ai-grok-4-7-z-ai-glm-5-3-flash-20260930.png``.
    Shared by the server's Content-Disposition header and the ``cards``
    batch export so the same view downloads under the same name either way."""
    path, _, raw_q = route.partition("?")
    params = parse_qs(raw_q)
    kind = params.get("kind", [""])[0]
    target = params.get("target", [""])[0]
    grp = params.get("group", [""])[0]

    def slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

    if path == "/card" and kind and target:
        base = f"{slug(kind)}-{slug(target)}"
        if grp:
            base += f"-{slug(grp)}"
        lens = params.get("lens", [""])[0]
        if lens and lens != "overall":
            base += f"-lens-{slug(lens)}"
    else:
        base = slug(path) or "overview"
        # every distinguishing param belongs in the name — two different
        # views (lens, group, sort…) must not download as the same file
        extra = sorted(
            f"{slug(k)}-{slug(vs[0])}" for k, vs in params.items() if vs[0]
        )
        if extra:
            base += "-" + "-".join(extra)
    # stay under the 255-byte filename ceiling for long model ids —
    # digest-suffixed so two truncated routes can never collide
    if len(base) > 200:
        digest = hashlib.sha1(route.encode()).hexdigest()[:6]
        base = base[:192].rstrip("-") + f"-{digest}"
    return f"orchestral-{base}-{stamp or time.strftime('%Y%m%d')}.png"


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScreenshotUnavailable("playwright is not installed (pip install 'orchestral[shots]')") from exc
    return sync_playwright


@contextmanager
def browser_session() -> Iterator[Any]:
    """Yield one shared Chromium browser for a batch of captures."""
    sync_playwright = _import_playwright()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                yield browser
            finally:
                browser.close()
    except ScreenshotUnavailable:
        raise
    except Exception as exc:
        raise ScreenshotUnavailable(f"could not launch browser: {exc}") from exc


def capture_html(
    html_path: str | Path,
    out_png: str | Path,
    *,
    width: int = 1280,
    height: int = 900,
    browser: Any = None,
) -> Path:
    """Render an HTML file and write a PNG screenshot. Returns out_png.

    Pass `browser` from browser_session() to reuse one launch across captures.
    """
    html_path = Path(html_path)
    out_png = Path(out_png)

    def _capture(b: Any) -> None:
        page = b.new_page(viewport={"width": width, "height": height})
        try:
            # artifacts are model-generated HTML: abort all remote requests
            page.route("**/*", lambda route: (
                route.abort()
                if route.request.url.startswith(("http://", "https://"))
                else route.continue_()
            ))
            page.goto(html_path.resolve().as_uri())
            page.screenshot(path=str(out_png), full_page=True)
        finally:
            page.close()

    try:
        if browser is not None:
            _capture(browser)
        else:
            with browser_session() as shared:
                _capture(shared)
    except ScreenshotUnavailable:
        raise
    except Exception as exc:
        raise ScreenshotUnavailable(f"screenshot capture failed: {exc}") from exc
    return out_png


def capture_page(
    url: str,
    *,
    wait_for: str = "#view[data-ready]",
    element: str | None = None,
    width: int = 1280,
    height: int = 800,
    browser: Any = None,
    timeout_ms: int = 20000,
) -> bytes:
    """Load a live page, wait for it to settle, return PNG bytes.

    ``wait_for`` is the readiness selector (the SPA sets
    ``#view[data-ready]`` after each render). ``element`` narrows the
    shot to one node — e.g. ``.xcard`` for just the share card.
    """

    def _grab(b: Any) -> bytes:
        page = b.new_page(viewport={"width": width, "height": height})
        try:
            page.goto(url)
            page.wait_for_selector(wait_for, state="visible", timeout=timeout_ms)
            if element:
                page.wait_for_selector(element, state="visible", timeout=timeout_ms)
                return page.locator(element).first.screenshot(type="png")
            return page.locator(wait_for.split("[")[0]).first.screenshot(type="png")
        finally:
            page.close()

    try:
        if browser is not None:
            return _grab(browser)
        with browser_session() as shared:
            return _grab(shared)
    except ScreenshotUnavailable:
        raise
    except Exception as exc:
        raise ScreenshotUnavailable(f"page capture failed: {exc}") from exc


def capture_run(run_dir: str | Path, *, force: bool = False, browser: Any = None) -> tuple[Path | None, str]:
    """Screenshot a run's artifact.html into screenshot.png.

    Returns (path_or_none, status) where status is "captured", "current"
    (screenshot already up to date), or "no_artifact".
    """
    run_dir = Path(run_dir)
    artifact = run_dir / "artifact.html"
    out = run_dir / "screenshot.png"
    if not artifact.exists():
        return None, "no_artifact"
    if out.exists() and not force and out.stat().st_mtime >= artifact.stat().st_mtime:
        return out, "current"
    return capture_html(artifact, out, browser=browser), "captured"
