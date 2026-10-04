"""Shared fixture for browser tests: the key-free U23 corpus served on loopback.

Not a test module (no ``test_`` prefix). Building the corpus takes a few
seconds, so a process builds it once and every browser test class reuses it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestral.storage import RunMeta, RunStore
from orchestral.web.server import Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"

_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus)

_SHARED: dict[str, CorpusServer] = {}


class CorpusServer:
    """A loopback observatory over a freshly built corpus."""

    def __init__(self, shape: str = "full") -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "runs"
        self._stop = threading.Event()
        with patch.dict(os.environ, {}, clear=True):
            self.manifest = corpus.build_corpus(self.root, shape)
        if shape == "full":
            self._add_live_cli_run()
        obs = Observatory(self.root, FIXTURES / "tasks", FIXTURES / "models")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _add_live_cli_run(self) -> None:
        """One CLI-launched run that stays live for the server's lifetime.

        A fixed "20s ago" timestamp drifts past STALL_AFTER_S while the shared
        corpus is reused across test classes, flipping the row to `stalled`;
        a heartbeat thread appends a fresh event so idle_s stays small."""
        now = datetime.now(UTC)
        run_dir = self.root / "cli-live0001"
        run_dir.mkdir()
        events = run_dir / "events.jsonl"
        events.write_text(json.dumps({
            "type": "run.started", "timestamp": (now - timedelta(seconds=20)).isoformat()}) + "\n")
        RunStore(self.root).index_meta(RunMeta(
            run_id="cli-live0001", orchestrator="corpus/orch-a", task_id="corpus-landing-page",
            worker="corpus/worker-cheap", status="running",
            started_at=(now - timedelta(minutes=5)).isoformat(), run_dir=str(run_dir)))

        def beat() -> None:
            while not self._stop.wait(30):
                with events.open("a") as fh:
                    fh.write(json.dumps({"type": "run.heartbeat",
                                         "timestamp": datetime.now(UTC).isoformat()}) + "\n")

        threading.Thread(target=beat, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def api(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return json.loads(r.read())

    def close(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def shared(shape: str = "full") -> CorpusServer:
    if shape not in _SHARED:
        _SHARED[shape] = CorpusServer(shape)
    return _SHARED[shape]


def routes(srv: CorpusServer) -> list[str]:
    """Every SPA route, with detail routes bound to real corpus rows."""
    runs = srv.api("/api/runs?limit=5")
    rows = runs.get("runs") if isinstance(runs, dict) else runs
    run_id = rows[0]["run_id"]
    return [
        "/", "/runs", f"/run/{run_id}", "/leaderboard", "/compare",
        f"/compare?group={srv.manifest['long_group']}", "/experiment",
        "/cards", "/card?kind=group&target=corpus-main:r0", "/models", "/new", "/about",
    ]


def at_exit() -> None:
    for s in _SHARED.values():
        s.close()
    _SHARED.clear()


import atexit  # noqa: E402

atexit.register(at_exit)
