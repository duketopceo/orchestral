"""U23: the observatory fixture corpus (KTD14).

Deterministic, key-free data every cost, liveness, browser and capture test
renders. Nothing here may reach a provider or a non-loopback host.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestral.web.server import Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert SPEC and SPEC.loader
corpus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus)

FIXTURES = ROOT / "tests" / "fixtures" / "observatory"


def _digest(root: Path) -> tuple[str, str]:
    """(logical index dump, hash of every file under root) for byte comparison."""
    with closing(sqlite3.connect(root / "index.db")) as conn:
        dump = "\n".join(conn.iterdump())
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "index.db" and not p.name.startswith("index.db-"):
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return dump, h.hexdigest()


class _Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.root, "full")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql: str, *params):
        with closing(sqlite3.connect(self.root / "index.db")) as conn:
            return conn.execute(sql, params).fetchall()


class TestDeterminism(unittest.TestCase):
    def test_building_twice_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs"
            corpus.build_corpus(root, "full")
            first = _digest(root)
            shutil.rmtree(root)
            corpus.build_corpus(root, "full")
            self.assertEqual(first, _digest(root))

    def test_small_shapes_are_deterministic_too(self):
        for shape in ("empty", "single"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "runs"
                corpus.build_corpus(root, shape)
                first = _digest(root)
                shutil.rmtree(root)
                corpus.build_corpus(root, shape)
                self.assertEqual(first, _digest(root), shape)


class TestShapes(_Corpus):
    def test_empty_and_single(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus.build_corpus(Path(tmp) / "e", "empty")
            corpus.build_corpus(Path(tmp) / "s", "single")
            with closing(sqlite3.connect(Path(tmp) / "e" / "index.db")) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            with closing(sqlite3.connect(Path(tmp) / "s" / "index.db")) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)

    def test_thousand_plus_runs(self):
        self.assertGreaterEqual(self.q("SELECT COUNT(*) FROM runs")[0][0], 1000)

    def test_ninety_two_character_group_key(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM runs WHERE LENGTH(run_group) = 92")[0][0] > 0, True)

    def test_orphaned_running_row(self):
        rows = self.q("SELECT run_id, started_at, run_dir FROM runs WHERE status = 'running'")
        self.assertEqual(len(rows), 1)
        events = Path(rows[0][2]) / "events.jsonl"
        last = json.loads(events.read_text().strip().splitlines()[-1])
        self.assertTrue(last["timestamp"].startswith("2026-09-30"))
        self.assertNotIn("run.completed", events.read_text())

    def test_failed_run_with_spend_bills_more_than_it_recorded(self):
        (_rid, total, billed), = self.q(
            "SELECT r.run_id, r.total_cost_usd, SUM(c.api_cost_usd) FROM runs r "
            "JOIN calls c ON c.run_id = r.run_id WHERE r.status = 'failed' "
            "AND r.run_id = ? GROUP BY r.run_id", self.manifest["failed_run_id"])
        self.assertAlmostEqual(total, 0.11)
        self.assertAlmostEqual(billed, 0.74)

    def test_dry_and_holdout_runs(self):
        self.assertGreater(self.q("SELECT COUNT(*) FROM runs WHERE dry_run = 1")[0][0], 0)
        self.assertGreater(self.q("SELECT COUNT(*) FROM calls WHERE dry_run = 1")[0][0], 0)
        self.assertGreater(self.q(
            "SELECT COUNT(*) FROM runs WHERE json_extract(config, '$.holdout') = 1")[0][0], 0)

    def test_one_pairing_group_and_all_low_n_group(self):
        solo = self.q("SELECT COUNT(DISTINCT orchestrator || '|' || worker) FROM runs "
                      "WHERE run_group = ?", self.manifest["solo_group"])
        self.assertEqual(solo[0][0], 1)
        thin = self.q("SELECT orchestrator, worker, COUNT(*) FROM runs WHERE run_group = ? "
                      "AND status = 'finished' GROUP BY 1, 2", self.manifest["thin_group"])
        self.assertGreaterEqual(len(thin), 2)
        self.assertTrue(all(n < 3 for _, _, n in thin))

    def test_priced_history_spans_three_models_with_differing_ratios(self):
        ratios = {m: api / cost for m, api, cost in self.q(
            "SELECT model, SUM(api_cost_usd), SUM(cost_usd) FROM calls "
            "WHERE dry_run = 0 AND api_cost_usd IS NOT NULL GROUP BY model HAVING SUM(cost_usd) > 0")}
        self.assertGreaterEqual(len(ratios), 3)
        self.assertLess(min(ratios.values()), 1.1)
        self.assertGreater(max(ratios.values()), 10.0)
        priced_counts = dict(self.q(
            "SELECT model, COUNT(*) FROM calls WHERE dry_run = 0 AND api_cost_usd IS NOT NULL "
            "GROUP BY model"))
        self.assertTrue(any(n >= 20 for n in priced_counts.values()))
        self.assertTrue(any(n < 20 for n in priced_counts.values()))

    def test_unpriced_calls_exist_including_a_model_with_none_priced(self):
        self.assertGreater(self.q(
            "SELECT COUNT(*) FROM calls WHERE dry_run = 0 AND api_cost_usd IS NULL")[0][0], 0)
        none_priced = self.q(
            "SELECT model FROM calls WHERE dry_run = 0 GROUP BY model "
            "HAVING SUM(api_cost_usd IS NOT NULL) = 0")
        self.assertEqual(len(none_priced), 1)

    def test_every_run_has_directory_events_and_report(self):
        for (run_dir,) in self.q("SELECT run_dir FROM runs ORDER BY run_id LIMIT 50"):
            d = Path(run_dir)
            self.assertTrue((d / "events.jsonl").is_file(), d)
            self.assertTrue((d / "run.json").is_file(), d)
        finished = self.q("SELECT run_dir FROM runs WHERE status = 'finished' LIMIT 5")
        for (run_dir,) in finished:
            json.loads((Path(run_dir) / "report.json").read_text())

    def test_sync_journal_is_clean(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM sync_dirty")[0][0], 0)


class TestServesWithoutNetwork(_Corpus):
    ROUTES = ("/api/overview", "/api/runs", "/api/groups", "/api/matrix", "/api/pairings",
              "/api/leaderboard", "/api/cards", "/api/tasks", "/api/models", "/api/flags",
              "/api/models-catalog", "/api/compare?a=corpus-main:r0&b=corpus-main:r1",
              "/api/card?kind=group&target=corpus-main:r0",
              "/api/estimate?task=corpus-landing-page&orchestrator=corpus/orch-a&worker=corpus/worker-cheap",
              "/api/run/corpfail0001", "/api/run/corporph0930")

    def test_serving_with_empty_environment_reaches_no_network_host(self):
        attempts: list[object] = []
        real_connect = socket.socket.connect

        def guarded(sock, address):
            host = address[0] if isinstance(address, tuple) else address
            if host not in ("127.0.0.1", "::1", "localhost"):
                attempts.append(address)
                raise OSError(f"network blocked in test: {address!r}")
            return real_connect(sock, address)

        with patch.dict(os.environ, {}, clear=True), patch.object(socket.socket, "connect", guarded):
            obs = Observatory(self.root, FIXTURES / "tasks", FIXTURES / "models")
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                port = httpd.server_address[1]
                for route in ("/", *self.ROUTES):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}") as r:
                        self.assertEqual(r.status, 200, route)
                        r.read()
            finally:
                httpd.shutdown()
                httpd.server_close()
        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
