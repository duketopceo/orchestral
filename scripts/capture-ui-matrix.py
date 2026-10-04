#!/usr/bin/env python3
"""Capture every observatory route at 1440 and 390 in both themes (PR evidence).

Builds the key-free fixture corpus in a temp directory, serves it on loopback
and screenshots each route with Playwright. No network, no provider key, no
spend. Needs the ``[shots]`` extra and Chromium.

    python scripts/capture-ui-matrix.py --out /path/to/evidence
    python scripts/capture-ui-matrix.py --out /tmp/shots --route /runs --width 390

Files are named ``<route-slug>-<width>-<theme>.png``. The script writes only
under ``--out``; keep that outside the repository.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
WIDTHS = {1440: 900, 390: 844}
MAX_HEIGHT = 3000
THEMES = ("paper", "stage")


def _corpus_module():
    spec = importlib.util.spec_from_file_location(
        "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def route_list(base: str, manifest: dict) -> list[str]:
    """Every SPA route, detail routes bound to real corpus rows."""
    with urllib.request.urlopen(f"{base}/api/runs?limit=5", timeout=30) as r:
        payload = json.loads(r.read())
    rows = payload.get("runs") if isinstance(payload, dict) else payload
    return [
        "/", "/runs", f"/run/{manifest['failed_run_id']}", f"/run/{rows[0]['run_id']}",
        "/leaderboard", "/compare", "/experiment", "/cards",
        "/card?kind=group&target=corpus-main:r0", "/models", "/new", "/about",
    ]


def slug(route: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-") or "now"


def capture(out: Path, routes: list[str] | None = None, widths: list[int] | None = None,
            themes: tuple[str, ...] = THEMES) -> list[Path]:
    from playwright.sync_api import sync_playwright

    from orchestral.web.server import Observatory, make_handler

    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    written: list[Path] = []
    try:
        with patch.dict(os.environ, {}, clear=True):
            manifest = _corpus_module().build_corpus(tmp / "runs", "full")
        obs = Observatory(tmp / "runs", FIXTURES / "tasks", FIXTURES / "models")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            todo = routes or route_list(base, manifest)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                for width in widths or list(WIDTHS):
                    for theme in themes:
                        ctx = browser.new_context(viewport={"width": width, "height": WIDTHS.get(width, 900)},
                                                  has_touch=width <= 640)
                        ctx.add_init_script(
                            f"try{{localStorage.setItem('orchestral.theme','{theme}')}}catch(e){{}}")
                        pg = ctx.new_page()
                        for route in todo:
                            pg.goto(f"{base}/#{route}")
                            pg.wait_for_selector("#view[data-ready]", timeout=30000)
                            pg.wait_for_timeout(250)
                            if pg.get_attribute("#view", "data-ready") == "error":
                                raise SystemExit(f"{route} at {width} {theme}: view failed to load")
                            dest = out / f"{slug(route)}-{width}-{theme}.png"
                            # a long list (1,000+ runs) would be an 85,000px image: keep the top
                            height = pg.evaluate("document.documentElement.scrollHeight")
                            if height > MAX_HEIGHT:
                                pg.screenshot(path=str(dest), full_page=True,
                                              clip={"x": 0, "y": 0, "width": width, "height": MAX_HEIGHT})
                            else:
                                pg.screenshot(path=str(dest), full_page=True)
                            written.append(dest)
                        ctx.close()
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="directory for the PNGs (outside the repo)")
    ap.add_argument("--route", action="append", help="limit to a route (repeatable)")
    ap.add_argument("--width", action="append", type=int, choices=sorted(WIDTHS), help="limit to a width")
    ap.add_argument("--theme", action="append", choices=THEMES, help="limit to a theme")
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    try:
        files = capture(args.out, args.route, args.width, tuple(args.theme or THEMES))
    except ImportError as exc:
        print(f"capture-ui-matrix: {exc}. Install the extra: pip install -e '.[shots]' "
              "and run: python -m playwright install chromium", file=sys.stderr)
        return 2
    print(f"wrote {len(files)} screenshot(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
