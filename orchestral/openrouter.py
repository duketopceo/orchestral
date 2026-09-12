"""Minimal OpenRouter client for orchestral.

Tracks latency and returns usage so the runner can compute cost from the model
config. API keys are read from the environment only — never from disk or git.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from orchestral.costs import token_usage_from_raw


MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 1.0
NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 422}
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

_PRIVATE_HOST_MARKERS = ("localhost", "127.", "0.", "10.", "192.168.", "169.254.", "::1")
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _is_private_host(hostname: str) -> bool:
    h = hostname.lower()
    if h.endswith(".local") or h.endswith(".internal"):
        return True
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (ValueError, IndexError):
            pass
    return any(h == m or h.startswith(m) for m in _PRIVATE_HOST_MARKERS)


class OpenRouterClient:
    """OpenAI-compatible chat client; OpenRouter is the default endpoint.

    `provider` names the configured backend ("openrouter" or
    "openai-compatible"); `api_key_env` names the env var the key is read from.
    The OpenRouter-only Images API is gated on provider == "openrouter".
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        api_key_env: str = "OPENROUTER_API_KEY",
        provider: str = "openrouter",
    ):
        self.provider = provider
        self.api_key = (api_key or os.environ.get(api_key_env) or "").strip()
        if not self.api_key:
            raise ValueError(f"{api_key_env} is not set")
        self.client = httpx.Client(base_url=base_url, timeout=120.0, follow_redirects=True)

    def _post_with_retry(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        attempt = 0
        last_error: Exception | None = None
        while attempt <= MAX_RETRIES:
            attempt += 1
            try:
                response = self.client.post(
                    path,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "HTTP-Referer": "https://github.com/duketopceo/orchestral",
                        "X-Title": "orchestral",
                    },
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in NON_RETRYABLE_STATUSES:
                    raise OpenRouterError(f"OpenRouter error {exc.response.status_code}: {exc.response.text}") from exc
                if attempt > MAX_RETRIES:
                    last_error = exc
                    break
                delay = _retry_after_seconds(exc) or (BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))
                time.sleep(delay)
                continue
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt > MAX_RETRIES:
                    raise OpenRouterError(f"OpenRouter request failed after {MAX_RETRIES} retries: {exc}") from exc
                delay = BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                time.sleep(delay)
                continue
            return response
        raise OpenRouterError(f"OpenRouter request to {path} failed after retries") from last_error

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        start = time.time()
        response = self._post_with_retry("/chat/completions", {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })

        data = response.json()
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content") or ""
        usage = data.get("usage", {})

        return {
            "id": data.get("id"),
            "model": data.get("model", model),
            "content": content,
            "usage": token_usage_from_raw(usage).to_dict(),
            "latency_ms": (time.time() - start) * 1000,
            "raw_response": data,
        }

    def images(
        self,
        *,
        model: str,
        prompt: str,
        aspect_ratio: str | None = None,
        output_format: str = "png",
    ) -> dict[str, Any]:
        """Generate an image via the dedicated OpenRouter Images API.

        Returns a dict with `image_bytes` (decoded from b64_json), `usage`,
        and `latency_ms`, mirroring the `chat` return shape.
        """
        if self.provider != "openrouter":
            raise OpenRouterError(
                f"image generation is only supported for provider 'openrouter'; "
                f"provider {self.provider!r} is chat-only"
            )
        start = time.time()
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "output_format": output_format,
        }
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio

        response = self._post_with_retry("/images", payload)
        data = response.json()
        items = data.get("data", [])
        if not isinstance(items, list):
            raise OpenRouterError(f"Malformed images response: data is {type(items).__name__}")
        first = items[0] if items and isinstance(items[0], dict) else {}
        image_bytes = b""
        if first.get("b64_json"):
            image_bytes = base64.b64decode(first["b64_json"])
        elif first.get("url"):
            image_bytes = self._download(first["url"])

        return {
            "id": data.get("id"),
            "model": data.get("model", model),
            "image_bytes": image_bytes,
            "output_format": output_format,
            "usage": token_usage_from_raw(data.get("usage", {})).to_dict(),
            "latency_ms": (time.time() - start) * 1000,
            "raw_response": data,
        }

    def _download(self, url: str) -> bytes:
        """Fetch an image returned as a URL instead of b64_json.

        The URL comes from the API response, so validate before fetching:
        https only, no loopback/private hosts, no redirects to off-host.
        """
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or _is_private_host(host):
            raise OpenRouterError(f"Refusing to download image from untrusted URL: {parsed.scheme}://{host}")
        with self.client.stream("GET", url, follow_redirects=False) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > _MAX_IMAGE_BYTES:
                    raise OpenRouterError(f"Image download exceeded {_MAX_IMAGE_BYTES} bytes")
                chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "OpenRouterClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class OpenRouterError(Exception):
    """Raised when the OpenRouter API returns an error."""


def _retry_after_seconds(exc: httpx.HTTPStatusError) -> float | None:
    value = _find_retry_after(exc.response.headers.get("retry-after"))
    if value is not None:
        return value
    return _find_retry_after(getattr(exc, "body", None))


def _find_retry_after(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return _coerce_retry_after(value)
    if isinstance(value, str):
        return _coerce_retry_after(value)
    if isinstance(value, dict):
        for child in value.values():
            found = _find_retry_after(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_retry_after(child)
            if found is not None:
                return found
    return None


def _coerce_retry_after(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
