"""Tests for orchestral/remotecatalog.py — fetch guards, normalization, cache."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestral import remotecatalog


class _Resp:
    """Minimal httpx.Response stand-in for the fetch path."""

    def __init__(self, body, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.is_redirect = 300 <= status_code < 400
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._body


class TestFetchRemoteCatalog(unittest.TestCase):
    def test_normalizes_provider_rows(self):
        body = {"data": [
            {"id": "acme/cheap", "name": "Cheap",
             "architecture": {"input_modalities": ["text"],
                              "output_modalities": ["text"]},
             "pricing": {"prompt": "0.000001", "completion": "0.000002"},
             "supported_parameters": ["tools"], "context_length": 1000,
             "expiration_date": "2026-12-01"},
            {"id": "acme/vision", "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"]},
             "pricing": {"prompt": "0", "completion": "0"}},
            "not-a-dict",
            {"name": "no-id-entry"},
        ]}
        with patch.object(remotecatalog.httpx, "get", return_value=_Resp(body)):
            cat = remotecatalog.fetch_remote_catalog("https://provider.example/v1/models")
        slugs = [m["slug"] for m in cat["models"]]
        self.assertEqual(slugs, ["acme/cheap", "acme/vision"])
        cheap = cat["models"][0]
        self.assertAlmostEqual(cheap["input_price_per_mtok"], 1.0)
        self.assertAlmostEqual(cheap["output_price_per_mtok"], 2.0)
        self.assertTrue(cheap["structured"])
        self.assertFalse(cheap["vision"])
        self.assertFalse(cheap["free"])
        self.assertEqual(cheap["expires"], "2026-12-01")
        vision = cat["models"][1]
        self.assertTrue(vision["vision"])
        self.assertTrue(vision["free"])
        self.assertEqual(cat["source"], "https://provider.example/v1/models")

    def test_rejects_non_https_and_private_hosts_without_fetching(self):
        with patch.object(remotecatalog.httpx, "get") as get:
            for bad in ("http://provider.example/models",
                        "https://127.0.0.1/models",
                        "https://localhost/models",
                        "https://192.168.1.1/models"):
                with self.assertRaises(ValueError, msg=bad):
                    remotecatalog.fetch_remote_catalog(bad)
            get.assert_not_called()

    def test_refuses_redirects(self):
        with patch.object(remotecatalog.httpx, "get",
                          return_value=_Resp({}, 302)), self.assertRaises(ValueError):
            remotecatalog.fetch_remote_catalog("https://provider.example/models")

    def test_malformed_body_raises(self):
        with patch.object(remotecatalog.httpx, "get",
                          return_value=_Resp({"data": "nope"})), self.assertRaises(ValueError):
            remotecatalog.fetch_remote_catalog("https://provider.example/models")


class TestLoadCatalog(unittest.TestCase):
    def test_valid_json_non_dict_degrades_to_none(self):
        with tempfile.TemporaryDirectory() as td:
            remotecatalog.catalog_path(td).write_text("[]", encoding="utf-8")
            self.assertIsNone(remotecatalog.load_catalog(td))
            remotecatalog.catalog_path(td).write_text(
                '"a string"', encoding="utf-8")
            remotecatalog._catalog_cache.clear()
            self.assertIsNone(remotecatalog.load_catalog(td))

    def test_write_read_roundtrip_and_cache_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            remotecatalog.write_catalog(td, {"source": "s", "models": []})
            first = remotecatalog.load_catalog(td)
            self.assertEqual(first, {"source": "s", "models": []})
            Path(remotecatalog.catalog_path(td)).write_text(
                json.dumps({"source": "s2", "models": [{"slug": "x"}]}),
                encoding="utf-8")
            again = remotecatalog.load_catalog(td)
            self.assertEqual(again["models"], [{"slug": "x"}])
            # equal serialized length, different content — the cache must
            # still notice via mtime, since size alone is ambiguous
            initial = {"source": "s", "models": [{"slug": "y"}]}
            updated = {"source": "s", "models": [{"slug": "x"}]}
            remotecatalog.write_catalog(td, initial)
            self.assertEqual(remotecatalog.load_catalog(td), initial)
            Path(remotecatalog.catalog_path(td)).write_text(
                json.dumps(updated, indent=2) + "\n", encoding="utf-8")
            self.assertEqual(remotecatalog.load_catalog(td), updated)


if __name__ == "__main__":
    unittest.main()
