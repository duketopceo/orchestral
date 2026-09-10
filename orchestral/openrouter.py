"""Minimal OpenRouter client for orchestral.

Tracks latency and returns usage so the runner can compute cost from the model
config. API keys are read from the environment only — never from disk or git.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx


MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 1.0
NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 422}


class OpenRouterClient:
    def __init__(self, api_key: str | None = None, base_url: str = "https://openrouter.ai/api/v1"):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        self.client = httpx.Client(base_url=base_url, timeout=120.0, follow_redirects=True)

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        start = time.time()
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        attempt = 0
        last_error: Exception | None = None
        while attempt <= MAX_RETRIES:
            attempt += 1
            try:
                response = self.client.post(
                    "/chat/completions",
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

            data = response.json()
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})

            return {
                "id": data.get("id"),
                "model": data.get("model", model),
                "content": content,
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "cached_tokens": usage.get("cached_tokens", 0),
                    "reasoning_tokens": usage.get("reasoning_tokens", 0),
                },
                "latency_ms": (time.time() - start) * 1000,
                "raw_response": data,
            }

        raise OpenRouterError("OpenRouter request failed after retries") from last_error

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
