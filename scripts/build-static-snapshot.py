#!/usr/bin/env python3
"""Render the hosted observatory's key tree to a directory (U6).

    python3 scripts/build-static-snapshot.py --runs runs --out /tmp/snap

Writes ``<out>/api/<key>`` for every key ``orchestral.web.snapshot`` defines.
Reads the local index only: no network, no provider keys.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from orchestral.storage import RunStore  # noqa: E402
from orchestral.web import snapshot  # noqa: E402


def _commit() -> str:
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                         capture_output=True, text=True, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=Path, default=Path("runs"), help="runs directory (index.db)")
    ap.add_argument("--tasks", type=Path, default=REPO / "tasks")
    ap.add_argument("--models", type=Path, default=REPO / "models")
    ap.add_argument("--groups", type=Path, default=None, help="groups.yaml (default: next to --tasks)")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    groups = args.groups or args.tasks.parent / "groups.yaml"
    snap = snapshot.build_snapshot(
        RunStore(args.runs), args.tasks, args.models, groups,
        synced_at=datetime.now(UTC).isoformat(timespec="seconds"), source_commit=_commit())
    n = snapshot.write_snapshot(snap, args.out)
    print(f"wrote {n} files under {args.out / 'api'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
