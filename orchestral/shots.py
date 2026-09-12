"""Playwright screenshot capture for HTML artifacts.

Optional: playwright is only imported inside the capture call so the base
install never needs it. Missing package or browser binaries raise
ScreenshotUnavailable, which callers are expected to degrade on.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class ScreenshotUnavailable(Exception):
    """Raised when Playwright or its browser binaries are not installed."""


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
