"""Embedding-model registry + plan/artifact clustering.

Analysis-only surface: embed run plans or artifacts through a small model
registry, cache vectors per model (sha1-of-text keyed, model-marked files
under ``runs/_embeddings/``), and cluster or compare across models. Every
output names the embedding model that produced it.

Tier 1 (httpx-only, zero new deps): local Ollama models.
Tier 2 (optional, degrade to ``skipped: <reason>``):
``pplx/embed-v2-late-0.6b`` needs ``sentence-transformers`` — a multi-vector
late-interaction model, so its similarity is token-level MaxSim, not cosine —
and hosted ``pplx/embed-v1-4b`` needs ``PPLX_API_KEY``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

OLLAMA_URL = os.environ.get("ORCHESTRAL_OLLAMA_URL", "http://127.0.0.1:11434")

Doc = dict[str, Any]  # {"id", "task", "orch", "worker", "text"}


# ---------------------------------------------------------------- models

def _ollama_embedder(model: str) -> Embedder:
    def embed(texts: list[str], cache: dict[str, Any], base_url: str) -> list[Any]:
        todo = list(dict.fromkeys(t for t in texts if _sha(t) not in cache))
        for k in range(0, len(todo), 16):
            part = todo[k:k + 16]
            r = httpx.post(f"{base_url}/api/embed",
                           json={"model": model, "input": part},
                           timeout=300)
            r.raise_for_status()
            for t, e in zip(part, r.json()["embeddings"], strict=True):
                cache[_sha(t)] = e
        return [cache[_sha(t)] for t in texts]

    return Embedder(slug=f"ollama/{model}", kind="cosine", embed=embed)


def _st_late_embedder(slug: str, model: str) -> Embedder:
    """Optional multi-vector model — one vector per token, MaxSim scoring."""
    def embed(texts: list[str], cache: dict[str, Any], base_url: str) -> list[Any]:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("needs sentence-transformers") from exc
        st = SentenceTransformer(model)
        for t in texts:
            if _sha(t) not in cache:
                cache[_sha(t)] = [v.tolist() for v in st.encode(t)]
        return [cache[_sha(t)] for t in texts]

    return Embedder(slug=slug, kind="maxsim", embed=embed)


def _pplx_api_embedder(slug: str, model: str) -> Embedder:
    def embed(texts: list[str], cache: dict[str, Any], base_url: str) -> list[Any]:
        key = os.environ.get("PPLX_API_KEY")
        if not key:
            raise RuntimeError("needs PPLX_API_KEY")
        todo = [t for t in texts if _sha(t) not in cache]
        for k in range(0, len(todo), 16):
            part = todo[k:k + 16]
            r = httpx.post("https://api.perplexity.ai/v1/embeddings",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": model, "input": part},
                           timeout=300)
            r.raise_for_status()
            for t, row in zip(part, r.json()["data"], strict=True):
                cache[_sha(t)] = row["embedding"]
        return [cache[_sha(t)] for t in texts]

    return Embedder(slug=slug, kind="cosine", embed=embed)


@dataclass
class Embedder:
    """A registered embedding model. ``kind`` picks the similarity fn —
    ``cosine`` for single-vector models, ``maxsim`` for late-interaction
    multi-vector models (the v2-late spike proved mean-pooling collapses)."""
    slug: str
    kind: str
    embed: Callable[[list[str], dict[str, Any], str], list[Any]]

    def sim(self, a: Any, b: Any) -> float:
        return _maxsim(a, b) if self.kind == "maxsim" else cosine(a, b)

    def unavailable_reason(self) -> str | None:
        if self.kind == "maxsim":
            try:
                import sentence_transformers  # noqa: F401
            except ImportError:
                return "needs sentence-transformers"
        if self.slug == "pplx/embed-v1-4b" and not os.environ.get("PPLX_API_KEY"):
            return "needs PPLX_API_KEY"
        return None


EMBED_MODELS: dict[str, Callable[[], Embedder]] = {
    "ollama/embeddinggemma": lambda: _ollama_embedder("embeddinggemma"),
    "ollama/bge-m3": lambda: _ollama_embedder("bge-m3"),
    "pplx/v2-late-0.6b": lambda: _st_late_embedder(
        "pplx/v2-late-0.6b", "perplexity/pplx-embed-v2-late-0.6b"),
    "pplx/embed-v1-4b": lambda: _pplx_api_embedder(
        "pplx/embed-v1-4b", "pplx-embed-v1-4b"),
}

DEFAULT_EMBED_MODEL = "ollama/embeddinggemma"


def embedder_for(slug: str) -> Embedder:
    if slug not in EMBED_MODELS:
        raise ValueError(
            f"unknown embedding model {slug!r}; registry: {', '.join(EMBED_MODELS)}")
    return EMBED_MODELS[slug]()


# ---------------------------------------------------------------- math

def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _maxsim(a: list[list[float]], b: list[list[float]]) -> float:
    """ColBERT-style late interaction: mean over query tokens of the max
    cosine against any document token."""
    if not a or not b:
        return 0.0
    return sum(max(cosine(q, d) for d in b) for q in a) / len(a)


def cluster_greedy(embedder: Embedder, vectors: list[Any], thresh: float) -> list[list[int]]:
    """Greedy single-linkage-by-centroid: join the first centroid within
    ``thresh``, else start a new cluster."""
    cents: list[Any] = []
    members: list[list[int]] = []
    for i, e in enumerate(vectors):
        best, bi = -1.0, -1
        for j, c in enumerate(cents):
            s = embedder.sim(e, c)
            if s > best:
                best, bi = s, j
        if bi >= 0 and best >= thresh:
            members[bi].append(i)
        else:
            cents.append(e)
            members.append([i])
    return members


# ------------------------------------------------------------- extraction

def _plan_text(run_dir: Path) -> str | None:
    try:
        d = json.loads((run_dir / "plan.json").read_text())
    except Exception:
        return None
    subs = "; ".join(
        s.get("description", "") for s in (d.get("subtasks") or [])
        if isinstance(s, dict))
    text = f"{d.get('reasoning', '')} || {subs}" if subs else None
    if text and text.startswith("Dry-run"):
        return None
    return text


def _artifact_text(run_dir: Path) -> str | None:
    path = run_dir / "artifact.zip"
    if not path.exists():
        art = next(
            (p for p in run_dir.glob("artifact.*")
             if p.suffix in {".html", ".txt", ".md", ".sql", ".py", ".json"}),
            None)
        if art is None:
            return None
        try:
            return art.read_bytes()[:8000].decode("utf-8", "replace")
        except Exception:
            return None
    try:
        with zipfile.ZipFile(path) as z:
            parts = [
                z.read(n)[:4000].decode("utf-8", "replace")
                for n in z.namelist()
                if n.endswith((".html", ".py", ".md", ".txt", ".sql"))
            ]
        return "\n".join(parts)[:8000] if parts else None
    except Exception:
        return None


def extract_docs(metas: list[Any], kind: str = "plans") -> list[Doc]:
    """One Doc per run carrying a usable plan/artifact text. ``kind`` is
    ``plans`` (orchestrator decomposition) or ``artifacts`` (output text)."""
    extract = _plan_text if kind == "plans" else _artifact_text
    docs = []
    for m in metas:
        if m.status != "finished" or m.dry_run or not m.run_dir:
            continue
        text = extract(Path(m.run_dir))
        if text:
            docs.append({"id": Path(m.run_dir).name, "task": m.task_id,
                         "orch": m.orchestrator, "worker": m.worker,
                         "text": text[:8000]})
    return docs


# ---------------------------------------------------------------- cache

def _cache_path(runs_dir: str | Path, model_slug: str) -> Path:
    safe = model_slug.replace("/", "_")
    return Path(runs_dir) / "_embeddings" / f"{safe}.json"


def embed_docs(embedder: Embedder, docs: list[Doc],
               runs_dir: str | Path, base_url: str = OLLAMA_URL) -> list[Any]:
    """Embed doc texts with a per-model sha1-keyed cache — re-runs are
    incremental and two models never share a vector file."""
    path = _cache_path(runs_dir, embedder.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache: dict[str, Any] = json.loads(path.read_text())
    except Exception:
        cache = {}
    try:
        vectors = embedder.embed([d["text"] for d in docs], cache, base_url)
    finally:
        # cache mutates in place as vectors land — persist even on a
        # mid-embed failure (e.g. ollama dying) so re-runs stay incremental
        if cache:
            path.write_text(json.dumps({"model": embedder.slug, "vectors": cache}))
    return vectors


# -------------------------------------------------------------- analysis

def cluster_report(embedder: Embedder, docs: list[Doc],
                   vectors: list[Any], thresh: float) -> dict[str, Any]:
    from collections import Counter
    members = cluster_greedy(embedder, vectors, thresh)
    clusters = []
    for m in sorted(members, key=len, reverse=True):
        clusters.append({
            "size": len(m),
            "tasks": dict(Counter(docs[i]["task"] for i in m).most_common(5)),
            "orchs": dict(Counter(docs[i]["orch"] for i in m).most_common(5)),
            "example": docs[m[0]]["text"][:150],
            "ids": [docs[i]["id"] for i in m],
        })
    return {"model": embedder.slug, "kind": embedder.kind, "docs": len(docs),
            "threshold": thresh, "clusters": len(members),
            "cluster_list": clusters}


def compare_models(embedders: list[Embedder], docs: list[Doc],
                   vectors_by_slug: dict[str, list[Any]],
                   thresholds: tuple[float, ...] = (0.86, 0.92)) -> dict[str, Any]:
    """Same corpus through N models: cluster counts at fixed thresholds
    plus same-task vs diff-task similarity means — the repeatable version
    of the parked embedding spike."""
    models_out: list[dict[str, Any]] = []
    out: dict[str, Any] = {"docs": len(docs), "models": models_out}
    for emb in embedders:
        vectors = vectors_by_slug[emb.slug]
        counts = {str(t): len(cluster_greedy(emb, vectors, t)) for t in thresholds}
        same: list[float] = []
        diff: list[float] = []
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                s = emb.sim(vectors[i], vectors[j])
                if docs[i]["task"] == docs[j]["task"]:
                    same.append(s)
                else:
                    diff.append(s)
        models_out.append({
            "model": emb.slug,
            "cluster_counts": counts,
            "same_task_mean": (sum(same) / len(same)) if same else None,
            "diff_task_mean": (sum(diff) / len(diff)) if diff else None,
            "pairs": len(same) + len(diff),
        })
    return out
