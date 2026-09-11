"""Tests for OpenRouter Images API support and per-image cost accounting."""

from __future__ import annotations

import base64
import unittest
from unittest.mock import MagicMock

import httpx

from orchestral.config import ModelConfig
from orchestral.costs import compute_image_cost, token_usage_from_raw
from orchestral.openrouter import OpenRouterClient, OpenRouterError
from orchestral.planners import TINY_PNG as PNG_BYTES


def _client() -> OpenRouterClient:
    return OpenRouterClient(api_key="test-key", base_url="https://openrouter.ai/api/v1")


def _response(status: int, payload: dict | None = None, headers: dict | None = None) -> httpx.Response:
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/images")
    return httpx.Response(status, json=payload or {}, headers=headers or {}, request=req)


class TestImagesEndpoint(unittest.TestCase):
    def test_decodes_b64_image(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(200, {
            "id": "gen-1",
            "model": "img/model",
            "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}],
            "usage": {"total_tokens": 12, "prompt_tokens": 12},
        }))
        out = client.images(model="img/model", prompt="a red panda")
        self.assertEqual(out["image_bytes"], PNG_BYTES)
        self.assertEqual(out["usage"]["prompt_tokens"], 12)
        self.assertGreaterEqual(out["latency_ms"], 0)

    def test_retries_on_429_with_retry_after(self):
        client = _client()
        ok = _response(200, {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]})
        rate_limited = _response(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "0"})
        client.client.post = MagicMock(side_effect=[rate_limited, ok])
        out = client.images(model="img/model", prompt="x")
        self.assertEqual(out["image_bytes"], PNG_BYTES)
        self.assertEqual(client.client.post.call_count, 2)

    def test_400_raises_immediately(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(400, {"error": {"message": "bad"}}))
        with self.assertRaises(OpenRouterError):
            client.images(model="img/model", prompt="x")
        self.assertEqual(client.client.post.call_count, 1)

    def test_hits_images_endpoint_with_payload(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(200, {
            "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}],
        }))
        client.images(model="img/model", prompt="x", aspect_ratio="16:9", output_format="webp")
        args, kwargs = client.client.post.call_args
        self.assertEqual(args[0], "/images")
        self.assertEqual(kwargs["json"]["prompt"], "x")
        self.assertEqual(kwargs["json"]["aspect_ratio"], "16:9")
        self.assertEqual(kwargs["json"]["output_format"], "webp")

    def test_url_download_path(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(200, {
            "data": [{"url": "https://cdn.example.com/img.png"}],
        }))
        stream_resp = MagicMock()
        stream_resp.raise_for_status = MagicMock()
        stream_resp.iter_bytes = MagicMock(return_value=iter([PNG_BYTES]))
        client.client.stream = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=stream_resp),
            __exit__=MagicMock(return_value=False),
        ))
        out = client.images(model="img/model", prompt="x")
        self.assertEqual(out["image_bytes"], PNG_BYTES)

    def test_download_rejects_private_host(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(200, {
            "data": [{"url": "http://169.254.169.254/latest/meta-data"}],
        }))
        with self.assertRaises(OpenRouterError):
            client.images(model="img/model", prompt="x")

    def test_download_rejects_non_https(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(200, {
            "data": [{"url": "http://images.example.com/x.png"}],
        }))
        with self.assertRaises(OpenRouterError):
            client.images(model="img/model", prompt="x")

    def test_malformed_data_shape(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(200, {"data": "oops"}))
        with self.assertRaises(OpenRouterError):
            client.images(model="img/model", prompt="x")


class TestChatNullContent(unittest.TestCase):
    def test_null_content_becomes_empty_string(self):
        # regression: CI eval run got "content": null and crashed on .strip()
        client = _client()
        client.client.post = MagicMock(return_value=_response(200, {
            "id": "x", "model": "m",
            "choices": [{"message": {"content": None}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }))
        out = client.chat(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(out["content"], "")


class TestImageCost(unittest.TestCase):
    def _cfg(self, per_image: float = 0.03) -> ModelConfig:
        return ModelConfig(
            slug="img/model", name="IMG", role="worker",
            input_price_per_mtok=0.5, output_price_per_mtok=2.0,
            price_per_image=per_image,
        )

    def test_fallback_to_per_image_price(self):
        self.assertAlmostEqual(compute_image_cost(self._cfg(), None, 2), 0.06)

    def test_token_usage_wins_when_present(self):
        usage = token_usage_from_raw({"prompt_tokens": 1000, "completion_tokens": 0})
        cost = compute_image_cost(self._cfg(0.03), usage, 1)
        self.assertAlmostEqual(cost, 0.0005)


if __name__ == "__main__":
    unittest.main()
