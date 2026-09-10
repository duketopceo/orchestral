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
        response = self.client.post(
            "/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/duketopceo/orchestral",
                "X-Title": "orchestral",
            },
            json=payload,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            raise OpenRouterError(f"OpenRouter error {exc.response.status_code}: {body}") from exc

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
            },
            "latency_ms": (time.time() - start) * 1000,
            "raw_response": data,
        }

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "OpenRouterClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class OpenRouterError(Exception):
    """Raised when the OpenRouter API returns an error."""
