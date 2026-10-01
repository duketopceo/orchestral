"""Provider model-catalog sync — the full list a provider exposes, cached
next to the user's own ``models/*.yaml`` so the observatory can answer
"what could run" alongside "what has run".

Scope and honesty rules:

- The source is the provider's public model list (OpenRouter
  ``GET /api/v1/models`` — unauthenticated). It says what *exists* and
  what it costs; it does not say what the account can reach. Provider
  blocks, region gates, and moderation flags live in the account, not
  this payload — a blocked model is discovered when a call against it
  errors, which is why the catalog view pairs this list with per-model
  call error counts.
- "Qualified" for a provider-only model is inferred from declared
  capabilities (output modalities, ``supported_parameters``) — not a
  claim it will plan well. Declared roles in ``models/*.yaml`` remain the
  authoritative signal for orchestration.
- ``:free`` suffixes and zero prices both mark free tiers; models with an
  ``expiration_date`` are flagged — preview/stealth entries vanish.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from orchestral.openrouter import DEFAULT_BASE_URL, _is_private_host

DEFAULT_MODELS_URL = f"{DEFAULT_BASE_URL}/models"
CATALOG_FILENAME = "provider-catalog.json"

# Structured-output support is the honest proxy for "can drive a JSON
# plan/verdict reliably" — the closest the model list gets to declaring
# orchestrator or judge suitability.
_STRUCTURED_PARAMS = {"tools", "structured_outputs", "response_format"}


def catalog_path(models_dir: Path | str) -> Path:
    return Path(models_dir) / CATALOG_FILENAME


def fetch_remote_catalog(
    url: str = DEFAULT_MODELS_URL, *, timeout: float = 30.0
) -> dict[str, Any]:
    """GET the provider model list and normalize it. Raises httpx errors
    and ValueError on a malformed body — callers surface both.

    The URL is operator-supplied (``models sync --url``) but validated the
    same way the client validates media URLs: https only, no loopback or
    private hosts, no redirects."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or _is_private_host(host):
        raise ValueError(f"refusing catalog fetch from untrusted URL: {parsed.scheme}://{host}")
    resp = httpx.get(url, timeout=timeout, follow_redirects=False)
    if resp.is_redirect:
        raise ValueError(f"refusing redirected catalog fetch from {parsed.scheme}://{host}")
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError(f"model catalog {url}: expected a 'data' list")

    models: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        arch = item.get("architecture") or {}
        pricing = item.get("pricing") or {}
        params = set(item.get("supported_parameters") or [])
        out_modalities = sorted(arch.get("output_modalities") or [])
        prompt_price = float(pricing.get("prompt") or 0.0)
        completion_price = float(pricing.get("completion") or 0.0)
        models.append({
            "slug": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "context": int(item.get("context_length") or 0),
            "input_price_per_mtok": prompt_price * 1_000_000,
            "output_price_per_mtok": completion_price * 1_000_000,
            "input_modalities": sorted(arch.get("input_modalities") or []),
            "output_modalities": out_modalities,
            "vision": "image" in (arch.get("input_modalities") or []),
            "structured": bool(params & _STRUCTURED_PARAMS),
            "free": prompt_price == 0.0 and completion_price == 0.0,
            "expires": item.get("expiration_date"),
        })
    models.sort(key=lambda m: m["slug"])
    return {
        "source": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "models": models,
    }


_catalog_cache: dict[Path, tuple[tuple[int, int], dict[str, Any] | None]] = {}


def write_catalog(models_dir: Path | str, catalog: dict[str, Any]) -> Path:
    out = catalog_path(models_dir)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)
    _catalog_cache.pop(out, None)
    return out


def load_catalog(models_dir: Path | str) -> dict[str, Any] | None:
    """The cached snapshot, or None when it has never been fetched or is
    unreadable — the catalog view degrades to configured models only.
    Memoized on (size, mtime_ns): the file only changes via
    ``models sync``, so the stat is the whole invalidation."""
    p = catalog_path(models_dir)
    try:
        stat_key = (p.stat().st_size, p.stat().st_mtime_ns)
    except OSError:
        stat_key = (0, -1)
    if _catalog_cache.get(p, (None,))[0] == stat_key:
        return _catalog_cache[p][1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        data = None
    _catalog_cache[p] = (stat_key, data)
    return data
