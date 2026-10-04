"""Playwright screenshot capture for HTML artifacts.

Optional: playwright is only imported inside the capture call so the base
install never needs it. Missing package or browser binaries raise
ScreenshotUnavailable, which callers are expected to degrade on.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs


class ScreenshotUnavailable(Exception):
    """Raised when Playwright or its browser binaries are not installed.

    ``code`` tells a caller which fix to offer: ``playwright_missing``,
    ``chromium_missing`` or the generic ``unavailable``."""

    def __init__(self, message: str, *, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


class CaptureError(Exception):
    """The page loaded but could not become a card image: it timed out or the
    view reported ``data-ready="error"``. Never answered with image bytes."""

    def __init__(self, message: str, *, code: str = "capture_error") -> None:
        super().__init__(message)
        self.code = code


INSTALL_COMMAND = {
    "playwright_missing": "pip install 'orchestral[shots]' && playwright install chromium",
    "chromium_missing": "playwright install chromium",
}

# One fixed capture contract: the share card is 1200x675 CSS px rendered at 2x.
CARD_VIEWPORT = (1200, 675)
CARD_SCALE = 2
OG_VIEWPORT = (1200, 630)


def shot_name(route: str, *, stamp: str | None = None) -> str:
    """Deterministic download filename for a captured app route —
    ``orchestral-pairing-x-ai-grok-4-7-z-ai-glm-5-3-flash-20260930.png``.
    Shared by the server's Content-Disposition header and the ``cards``
    batch export so the same view downloads under the same name either way."""
    path, _, raw_q = route.partition("?")
    params = parse_qs(raw_q)
    params.pop("capture", None)  # the capture flag never renames a download
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
        raise ScreenshotUnavailable(
            "playwright is not installed (pip install 'orchestral[shots]')",
            code="playwright_missing") from exc
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
        code = "chromium_missing" if "Executable doesn't exist" in str(exc) else "unavailable"
        raise ScreenshotUnavailable(f"could not launch browser: {exc}", code=code) from exc


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
    scale: int = 1,
    browser: Any = None,
    timeout_ms: int = 20000,
) -> bytes:
    """Load a live page, wait for it to settle, return PNG bytes.

    ``wait_for`` is the readiness selector (the SPA sets
    ``#view[data-ready]`` after each render). ``element`` narrows the
    shot to one node, e.g. ``.xcard`` for just the share card. A view that
    settles as ``data-ready="error"`` raises :class:`CaptureError` instead of
    returning the error page as an image. The viewport, device scale, colour
    scheme and motion are fixed so the same data renders the same pixels.
    """
    view = wait_for.split("[")[0]

    def _grab(b: Any) -> bytes:
        page = b.new_page(
            viewport={"width": width, "height": height}, device_scale_factor=scale,
            color_scheme="light", reduced_motion="reduce", locale="en-US",
            timezone_id="UTC")
        try:
            page.goto(url)
            page.wait_for_selector(wait_for, state="visible", timeout=timeout_ms)
            if page.get_attribute(view, "data-ready") == "error":
                raise CaptureError(
                    "the view reported an error, so no image was made", code="view_error")
            if element:
                page.wait_for_selector(element, state="visible", timeout=timeout_ms)
                return page.locator(element).first.screenshot(type="png")
            return page.locator(view).first.screenshot(type="png")
        finally:
            page.close()

    try:
        if browser is not None:
            return _grab(browser)
        with browser_session() as shared:
            return _grab(shared)
    except (ScreenshotUnavailable, CaptureError):
        raise
    except Exception as exc:
        if type(exc).__name__ == "TimeoutError":
            raise CaptureError(
                f"the page did not settle within {timeout_ms // 1000}s", code="timeout") from exc
        raise ScreenshotUnavailable(f"page capture failed: {exc}") from exc


def optimize_png(png: bytes, *, timeout_s: int = 60) -> bytes:
    """Run ``oxipng`` over a capture when it is on PATH; otherwise, or when it
    fails, hand back the original bytes. Never a Python dependency."""
    exe = shutil.which("oxipng")
    if not exe:
        return png
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.png"
            path.write_bytes(png)
            done = subprocess.run(
                [exe, "-o", "4", "--strip", "safe", "--quiet", str(path)],
                capture_output=True, timeout=timeout_s, check=False)
            out = path.read_bytes()
            if done.returncode == 0 and 0 < len(out) < len(png):
                return out
    except (OSError, subprocess.SubprocessError):
        pass
    return png


def render_og(out_png: str | Path, *, template: str | Path | None = None) -> Path:
    """Render ``ui/og/default.html`` to the 1200x630 OG image (1x, so it stays
    inside the 150KB budget). The template is static: no data, no network."""
    template = Path(template) if template else Path(__file__).resolve().parents[1] / "ui" / "og" / "default.html"
    out_png = Path(out_png)
    width, height = OG_VIEWPORT

    def _grab(b: Any) -> bytes:
        page = b.new_page(
            viewport={"width": width, "height": height}, device_scale_factor=1,
            color_scheme="light", reduced_motion="reduce", locale="en-US",
            timezone_id="UTC")
        try:
            page.route("**/*", lambda route: (
                route.abort() if route.request.url.startswith(("http://", "https://"))
                else route.continue_()))
            page.goto(template.resolve().as_uri())
            page.evaluate("document.fonts.ready")
            return page.screenshot(type="png", clip={"x": 0, "y": 0, "width": width, "height": height})
        finally:
            page.close()

    try:
        with browser_session() as shared:
            png = _grab(shared)
    except ScreenshotUnavailable:
        raise
    except Exception as exc:
        raise ScreenshotUnavailable(f"og render failed: {exc}") from exc
    out_png.write_bytes(optimize_png(png))
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
