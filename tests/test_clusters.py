"""Cluster/embed surface: model-marked caches, deterministic clustering,
optional models reporting skipped, and the cross-model compare table."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestral import clusters
from orchestral.clusters import Embedder


def _fake_embedder(slug: str, vecs: dict[str, list[float]]) -> Embedder:
    """Deterministic embedder: maps text to planted vectors by content."""
    def embed(texts, cache, base_url):
        out = []
        for t in texts:
            key = clusters._sha(t)
            cache[key] = vecs[t]
            out.append(cache[key])
        return out
    return Embedder(slug=slug, kind="cosine", embed=embed)


DOCS = [
    {"id": "r1", "task": "alpha", "orch": "o1", "worker": "w", "text": "plan-a"},
    {"id": "r2", "task": "alpha", "orch": "o2", "worker": "w", "text": "plan-b"},
    {"id": "r3", "task": "beta", "orch": "o1", "worker": "w", "text": "plan-c"},
]


class TestClusterGreedy(unittest.TestCase):
    def test_planted_separation_clusters_as_expected(self):
        vecs = {"plan-a": [1.0, 0.0], "plan-b": [0.95, 0.05], "plan-c": [0.0, 1.0]}
        emb = _fake_embedder("test/fake", vecs)
        vectors = [vecs[d["text"]] for d in DOCS]
        members = clusters.cluster_greedy(emb, vectors, 0.9)
        self.assertEqual(sorted(sorted(m) for m in members), [[0, 1], [2]])

    def test_deterministic_rerun(self):
        vecs = {"plan-a": [1.0, 0.0], "plan-b": [0.95, 0.05], "plan-c": [0.0, 1.0]}
        emb = _fake_embedder("test/fake", vecs)
        vectors = [vecs[d["text"]] for d in DOCS]
        self.assertEqual(clusters.cluster_greedy(emb, vectors, 0.9),
                         clusters.cluster_greedy(emb, vectors, 0.9))


class TestModelMarkedCache(unittest.TestCase):
    def test_per_model_cache_files(self):
        vecs = {"plan-a": [1.0, 0.0], "plan-b": [0.9, 0.1], "plan-c": [0.0, 1.0]}
        with tempfile.TemporaryDirectory() as tmp:
            for slug in ("test/fake-a", "test/fake-b"):
                emb = _fake_embedder(slug, vecs)
                clusters.embed_docs(emb, DOCS, tmp)
            files = sorted(p.name for p in (Path(tmp) / "_embeddings").glob("*.json"))
            self.assertEqual(files, ["test_fake-a.json", "test_fake-b.json"])
            payload = json.loads((Path(tmp) / "_embeddings" / "test_fake-a.json").read_text())
            self.assertEqual(payload["model"], "test/fake-a")
            self.assertIn(clusters._sha("plan-a"), payload["vectors"])

    def test_cache_incremental(self):
        calls = {"n": 0}
        def embed(texts, cache, base_url):
            calls["n"] += 1
            for t in texts:
                cache.setdefault(clusters._sha(t), [1.0])
            return [cache[clusters._sha(t)] for t in texts]
        emb = Embedder(slug="test/inc", kind="cosine", embed=embed)
        with tempfile.TemporaryDirectory() as tmp:
            clusters.embed_docs(emb, DOCS, tmp)
            self.assertEqual(calls["n"], 1)
            clusters.embed_docs(emb, DOCS, tmp)
            # embed fn is still called but every text hits the cache —
            # the registry passes the same dict back; a real embedder only
            # calls its provider for cache misses (see _ollama_embedder)
            self.assertEqual(calls["n"], 2)


class TestOptionalModels(unittest.TestCase):
    def test_st_model_reports_unavailable_without_dep(self):
        emb = clusters.embedder_for("pplx/v2-late-0.6b")
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            reason = emb.unavailable_reason()
        self.assertEqual(reason, "needs sentence-transformers")

    def test_pplx_api_needs_key(self):
        emb = clusters.embedder_for("pplx/embed-v1-4b")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(emb.unavailable_reason(), "needs PPLX_API_KEY")

    def test_unknown_model_errors(self):
        with self.assertRaises(ValueError):
            clusters.embedder_for("not/a-model")


class TestCompareModels(unittest.TestCase):
    def test_separation_ranks(self):
        """Model A separates tasks (same=1.0, diff=0.0); model B does not."""
        docs = [
            {"id": "a1", "task": "alpha", "text": "a1"},
            {"id": "a2", "task": "alpha", "text": "a2"},
            {"id": "b1", "task": "beta", "text": "b1"},
            {"id": "b2", "task": "beta", "text": "b2"},
        ]
        va = {"a1": [1.0, 0.0], "a2": [1.0, 0.0], "b1": [0.0, 1.0], "b2": [0.0, 1.0]}
        vb = {k: [1.0, 0.0] for k in va}  # everything identical — no separation
        ea = _fake_embedder("m/a", va)
        eb = _fake_embedder("m/b", vb)
        out = clusters.compare_models(
            [ea, eb], docs,
            {"m/a": [va[d["text"]] for d in docs],
             "m/b": [vb[d["text"]] for d in docs]})
        ma, mb = out["models"]
        self.assertEqual(ma["cluster_counts"]["0.86"], 2)
        self.assertEqual(mb["cluster_counts"]["0.86"], 1)
        self.assertAlmostEqual(ma["same_task_mean"], 1.0)
        self.assertAlmostEqual(ma["diff_task_mean"], 0.0)
        self.assertAlmostEqual(mb["diff_task_mean"], 1.0)

    def test_report_marks_model(self):
        vecs = {"plan-a": [1.0, 0.0], "plan-b": [0.95, 0.05], "plan-c": [0.0, 1.0]}
        emb = _fake_embedder("ollama/testgemma", vecs)
        rep = clusters.cluster_report(emb, DOCS, [vecs[d["text"]] for d in DOCS], 0.9)
        self.assertEqual(rep["model"], "ollama/testgemma")
        self.assertEqual(rep["clusters"], 2)


if __name__ == "__main__":
    unittest.main()
