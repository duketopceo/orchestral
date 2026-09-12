"""Provider seam: resolve which API endpoint serves a given model config.

A model entry may carry `metadata.provider`, `metadata.base_url`, and
`metadata.api_key_env` to point at any OpenAI-compatible chat endpoint
(Together, Groq, vLLM, ollama, ...). OpenRouter remains the default.

Generic providers are chat-only: image generation is OpenRouter-specific and
`images()` on a non-OpenRouter provider fails fast with a clear error.
"""

from __future__ import annotations

import sys
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from orchestral.config import ModelConfig
from orchestral.openrouter import DEFAULT_BASE_URL, OpenRouterClient, _is_private_host

SUPPORTED_PROVIDERS = ("openrouter", "openai-compatible")
_DEFAULT_ENV = {"openrouter": "OPENROUTER_API_KEY", "openai-compatible": "OPENAI_API_KEY"}


@runtime_checkable
class Provider(Protocol):
    """Structural interface every provider client must satisfy."""

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
        temperature: float = 0.4,
    ) -> dict[str, Any]: ...

    def images(
        self,
        *,
        model: str,
        prompt: str,
        aspect_ratio: str | None = None,
        output_format: str = "png",
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


def provider_key(model: ModelConfig) -> tuple[str, str, str]:
    """Identity tuple for client caching: (provider, base_url, api_key_env)."""
    meta = model.metadata or {}
    provider = meta.get("provider", "openrouter")
    base_url = meta.get("base_url") or (DEFAULT_BASE_URL if provider == "openrouter" else "")
    api_key_env = meta.get("api_key_env") or _DEFAULT_ENV.get(provider, "")
    return (provider, base_url, api_key_env)


def provider_for(model: ModelConfig) -> Provider:
    """Build a client for the model's configured provider.

    Raises ValueError naming the problem (unknown provider, missing base_url,
    or missing API-key env var) rather than failing deep in a run.
    """
    provider, base_url, api_key_env = provider_key(model)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unknown provider {provider!r} for model {model.slug}; "
            f"supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    if not base_url:
        raise ValueError(
            f"Model {model.slug} uses provider {provider!r} but has no metadata.base_url"
        )
    if not api_key_env:
        raise ValueError(
            f"Model {model.slug} uses provider {provider!r} but has no metadata.api_key_env"
        )
    _warn_http_base_url(base_url, model.slug)
    return OpenRouterClient(base_url=base_url, api_key_env=api_key_env, provider=provider)


def _warn_http_base_url(base_url: str, slug: str) -> None:
    """Plain HTTP is fine for loopback/local providers; warn otherwise."""
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if parsed.scheme == "http" and not _is_private_host(host):
        print(
            f"warning: model {slug} uses plaintext http base_url ({host}); "
            "API keys will be sent unencrypted",
            file=sys.stderr,
        )
