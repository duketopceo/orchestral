#!/usr/bin/env python3
"""Regenerate the README visuals from the key-free fixture corpus.

Writes ``social.png`` (1280x640, from ``ui/og/social.html``) and
``observatory-paper.png`` (the Pairings view at 1440 in the paper theme).
Both are deterministic captures of the real UI: the corpus is built locally,
served on loopback, and nothing reaches a provider. Needs the ``[shots]``
extra and Chromium; ``oxipng`` is used when it is on PATH.

    python scripts/render-readme-assets.py            # writes docs/assets/
    python scripts/render-readme-assets.py --out /tmp/preview
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
SOCIAL = (1280, 640)
SHOT = (1440, 1000)


def _corpus_module():
    spec = importlib.util.spec_from_file_location(
        "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render(out: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright

    from orchestral.shots import optimize_png
    from orchestral.web.server import Observatory, make_handler

    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    written: list[Path] = []
    try:
        with patch.dict(os.environ, {}, clear=True):
            _corpus_module().build_corpus(tmp / "runs", "full")
        obs = Observatory(tmp / "runs", FIXTURES / "tasks", FIXTURES / "models")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                common = {"device_scale_factor": 1, "color_scheme": "light", "reduced_motion": "reduce",
                          "locale": "en-US", "timezone_id": "UTC"}

                ctx = browser.new_context(viewport={"width": SOCIAL[0], "height": SOCIAL[1]}, **common)
                pg = ctx.new_page()
                pg.goto(f"{base}/static/og/social.html")
                pg.wait_for_selector("html[data-ready]", timeout=30000)
                pg.evaluate("document.fonts.ready")
                dest = out / "social.png"
                dest.write_bytes(optimize_png(pg.screenshot(
                    type="png", clip={"x": 0, "y": 0, "width": SOCIAL[0], "height": SOCIAL[1]})))
                written.append(dest)
                ctx.close()

                ctx = browser.new_context(viewport={"width": SHOT[0], "height": SHOT[1]}, **common)
                ctx.add_init_script("try{localStorage.setItem('orchestral.theme','paper')}catch(e){}")
                pg = ctx.new_page()
                pg.goto(f"{base}/#/leaderboard")
                pg.wait_for_selector("#view[data-ready]", timeout=30000)
                if pg.get_attribute("#view", "data-ready") == "error":
                    raise SystemExit("leaderboard failed to load")
                pg.evaluate("document.fonts.ready")
                pg.wait_for_selector("#lb-strip svg", timeout=30000)
                dest = out / "observatory-paper.png"
                dest.write_bytes(optimize_png(pg.screenshot(
                    type="png", clip={"x": 0, "y": 0, "width": SHOT[0], "height": SHOT[1]})))
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
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "assets", help="output directory")
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    try:
        files = render(args.out)
    except ImportError as exc:
        print(f"render-readme-assets: {exc}. Install the extra: pip install -e '.[shots]' "
              "and run: python -m playwright install chromium", file=sys.stderr)
        return 2
    for f in files:
        print(f"{f} {f.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
