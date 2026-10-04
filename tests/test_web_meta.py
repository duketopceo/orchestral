"""U6: the capabilities document (`/api/meta`, KTD5)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestral import format as fmt
from orchestral.web import state
from orchestral.web.server import Observatory, make_handler

CAPABILITIES = {"launch", "cancel", "flag_write", "thread", "png_capture", "live_stream"}


class TestMetaPayload(unittest.TestCase):
    def test_local_default(self):
        meta = state.meta_payload()
        self.assertEqual(meta["mode"], "local")
        self.assertEqual(set(meta["capabilities"]), CAPABILITIES)
        self.assertTrue(all(meta["capabilities"].values()))
        self.assertIsNone(meta["synced_at"])
        self.assertEqual(meta["low_n"], {"cell": fmt.LOW_N_CELL, "best": fmt.LOW_N_BEST})
        self.assertIn("source_commit", meta)

    def test_hosted_has_no_local_capabilities(self):
        meta = state.meta_payload(mode="hosted", synced_at="2026-10-02T12:00:00+00:00",
                                  source_commit="abc1234")
        self.assertEqual(meta["mode"], "hosted")
        self.assertEqual(set(meta["capabilities"]), CAPABILITIES)
        self.assertFalse(any(meta["capabilities"].values()))
        self.assertEqual(meta["synced_at"], "2026-10-02T12:00:00+00:00")
        self.assertEqual(meta["source_commit"], "abc1234")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            state.meta_payload(mode="cloud")


class TestMetaRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "tasks").mkdir()
        (cls.tmp / "models").mkdir()
        obs = Observatory(cls.tmp, cls.tmp / "tasks", cls.tmp / "models")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_api_meta_is_local_with_every_capability(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/meta") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("application/json", r.headers["Content-Type"])
            meta = json.loads(r.read())
        self.assertEqual(meta["mode"], "local")
        self.assertEqual(set(meta["capabilities"]), CAPABILITIES)
        self.assertTrue(all(meta["capabilities"].values()))
        self.assertEqual(meta["low_n"]["cell"], fmt.LOW_N_CELL)
        self.assertEqual(meta["low_n"]["best"], fmt.LOW_N_BEST)


if __name__ == "__main__":
    unittest.main()
