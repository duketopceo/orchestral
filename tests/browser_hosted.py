"""Shared fixture for browser tests: the U23 corpus as a hosted read-only snapshot.

Not a test module. The snapshot is built once per process from the shared
corpus and served by a Worker-like handler (/api/<key> reads <key>.json)."""

from __future__ import annotations

from typing import Any

import browser_corpus as bc
from test_serve_browser import _serve, _WorkerSim

from orchestral.storage import RunStore
from orchestral.web import snapshot

_HOSTED: dict[str, Any] = {}


def hosted_base() -> str:
    if "base" not in _HOSTED:
        srv = bc.shared()
        snap = snapshot.build_snapshot(
            RunStore(srv.root),
            bc.FIXTURES / "tasks", bc.FIXTURES / "models",
            run_ids=[srv.manifest["failed_run_id"], srv.manifest["orphan_run_id"]],
            synced_at="2026-10-02T11:00:00+00:00", source_commit="abc1234")
        httpd, port = _serve(type("Sim", (_WorkerSim,), {"snap": snap}))
        _HOSTED["httpd"] = httpd
        _HOSTED["base"] = f"http://127.0.0.1:{port}"
    return _HOSTED["base"]
