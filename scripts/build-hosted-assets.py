#!/usr/bin/env python3
"""Stage ui/ for the hosted Worker with the asset version token resolved (U6).

    python3 scripts/build-hosted-assets.py [--out infra/cloudflare/observatory/.build/ui]

The local server substitutes ``__V__`` in app.html (the ``?v=`` on every module
URL, the import map, and through ``import.meta.url`` the sprite fetch) at
request time. The Worker serves static assets as stored, so this build step does
it once: it copies ``ui/`` and rewrites ``app.html`` with a content hash of the
tree, so a changed file is a changed URL. ``wrangler.toml`` runs it as its
``[build] command``, so ``wrangler deploy``, ``wrangler dev`` and a dry run all
stage it; there is no separate step to forget.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui"
DEFAULT_OUT = REPO / "infra" / "cloudflare" / "observatory" / ".build" / "ui"
TOKEN = "__V__"  # orchestral/web/server.py _VERSION_TOKEN


def content_version(ui: Path) -> str:
    """Ten hex chars of a hash over every file's path and bytes."""
    h = hashlib.sha256()
    for p in sorted(q for q in ui.rglob("*") if q.is_file()):
        h.update(p.relative_to(ui).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:10]


def build(ui: Path = UI, out: Path = DEFAULT_OUT) -> str:
    version = content_version(ui)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ui, out)
    app = out / "app.html"
    app.write_text(app.read_text(encoding="utf-8").replace(TOKEN, version), encoding="utf-8")
    return version


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    version = build(UI, args.out.resolve())
    print(f"hosted assets staged in {args.out} (version {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
