"""Tests for the OpenRouter async Videos API (submit -> poll -> download)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from orchestral.openrouter import (
    OpenRouterClient,
    OpenRouterError,
    OpenRouterVideoJobError,
    OpenRouterVideoSubmittedError,
    _is_private_host,
)

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32


def _client(provider: str = "openrouter") -> OpenRouterClient:
    return OpenRouterClient(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        provider=provider,
    )


def _response(status: int, payload: dict | None = None, method: str = "POST",
              url: str = "https://openrouter.ai/api/v1/videos") -> httpx.Response:
    req = httpx.Request(method, url)
    return httpx.Response(status, json=payload or {}, request=req)


class _FakeStream:
    """Stand-in for the httpx.Client.stream context manager."""

    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def is_redirect(self) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield self._data


class _FailingStream:
    """Stream stub whose raise_for_status() fails with an HTTP error."""

    def __init__(self, status: int):
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def is_redirect(self) -> bool:
        return False

    def raise_for_status(self) -> None:
        request = httpx.Request("GET", "https://openrouter.ai/api/v1/videos/x/content")
        raise httpx.HTTPStatusError(
            f"error for url '{request.url}'", request=request,
            response=httpx.Response(self._status, request=request),
        )


def _no_poll_delay():
    return patch("orchestral.openrouter.VIDEO_POLL_INTERVAL_SECONDS", 0)


class TestVideosEndpoint(unittest.TestCase):
    def test_submit_poll_download_happy_path(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-1",
            "polling_url": "https://openrouter.ai/api/v1/videos/job-1",
            "status": "pending",
        }))
        client.client.get = MagicMock(side_effect=[
            _response(200, {"status": "in_progress"}, method="GET",
                      url="https://openrouter.ai/api/v1/videos/job-1"),
            _response(200, {
                "status": "completed",
                "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-1/content?index=0"],
                "usage": {"cost": 0.42},
            }, method="GET", url="https://openrouter.ai/api/v1/videos/job-1"),
        ])
        client.client.stream = MagicMock(return_value=_FakeStream(MP4_BYTES))

        with _no_poll_delay():
            out = client.videos(model="vid/model", prompt="a red panda", duration=4)

        self.assertEqual(out["video_bytes"], MP4_BYTES)
        self.assertEqual(out["id"], "job-1")
        self.assertEqual(out["usage"]["cost"], 0.42)
        self.assertGreaterEqual(out["latency_ms"], 0)
        self.assertEqual(client.client.post.call_count, 1)
        self.assertEqual(client.client.get.call_count, 2)

    def test_failed_status_raises_terminal_error(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-2", "polling_url": "/videos/job-2", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(200, {
            "status": "failed", "error": {"message": "Content policy violation"},
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-2"))

        with _no_poll_delay(), self.assertRaises(OpenRouterVideoJobError) as ctx:
            client.videos(model="vid/model", prompt="x")

        self.assertEqual(ctx.exception.status, "failed")
        self.assertIn("Content policy violation", str(ctx.exception))
        self.assertEqual(client.client.post.call_count, 1)

    def test_poll_exhaustion_raises_submitted_error(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-3b", "polling_url": "/videos/job-3b", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(
            500, {"error": "boom"}, method="GET",
            url="https://openrouter.ai/api/v1/videos/job-3b"))

        with _no_poll_delay(), self.assertRaises(OpenRouterVideoSubmittedError) as ctx:
            client.videos(model="vid/model", prompt="x")

        self.assertEqual(client.client.post.call_count, 1)
        # job id is safe to log; the poll URL is not
        self.assertIn("job-3b", str(ctx.exception))
        self.assertNotIn("http", str(ctx.exception))

    def test_poll_timeout_raises_retryable_error(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-3", "polling_url": "/videos/job-3", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(200, {
            "status": "in_progress",
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-3"))

        with _no_poll_delay(), \
                patch("orchestral.openrouter.VIDEO_MAX_WAIT_SECONDS", 0), \
                self.assertRaises(OpenRouterError) as ctx:
            client.videos(model="vid/model", prompt="x")

        self.assertNotIsInstance(ctx.exception, OpenRouterVideoJobError)
        # but still post-submission: the runner must not resubmit a paid job
        self.assertIsInstance(ctx.exception, OpenRouterVideoSubmittedError)

    def test_missing_unsigned_urls_falls_back_to_content_endpoint(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-4", "polling_url": "/videos/job-4", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(200, {
            "status": "completed", "usage": {"cost": 0.1},
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-4"))
        client.client.stream = MagicMock(return_value=_FakeStream(MP4_BYTES))

        with _no_poll_delay():
            out = client.videos(model="vid/model", prompt="x")

        self.assertEqual(out["video_bytes"], MP4_BYTES)
        stream_url = client.client.stream.call_args[0][1]
        # full equality: urljoin would drop the /api/v1 prefix
        self.assertEqual(stream_url, "https://openrouter.ai/api/v1/videos/job-4/content?index=0")

    def test_relative_polling_url_resolves_under_api_prefix(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-4b", "polling_url": "/videos/job-4b", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(200, {
            "status": "completed",
            "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-4b/content?index=0"],
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-4b"))
        client.client.stream = MagicMock(return_value=_FakeStream(MP4_BYTES))

        with _no_poll_delay():
            client.videos(model="vid/model", prompt="x")

        self.assertEqual(
            client.client.get.call_args[0][0],
            "https://openrouter.ai/api/v1/videos/job-4b",
        )

    def test_download_auth_only_on_api_host(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-5", "polling_url": "/videos/job-5", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(200, {
            "status": "completed",
            "unsigned_urls": ["https://cdn.example.com/video.mp4"],
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-5"))
        client.client.stream = MagicMock(return_value=_FakeStream(MP4_BYTES))

        with _no_poll_delay():
            client.videos(model="vid/model", prompt="x")

        foreign_headers = client.client.stream.call_args.kwargs.get("headers") or {}
        self.assertNotIn("Authorization", foreign_headers)

        client.client.get = MagicMock(return_value=_response(200, {
            "status": "completed",
            "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-5/content?index=0"],
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-5"))
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-5", "polling_url": "/videos/job-5", "status": "pending",
        }))
        with _no_poll_delay():
            client.videos(model="vid/model", prompt="x")

        same_host_headers = client.client.stream.call_args.kwargs.get("headers") or {}
        self.assertEqual(same_host_headers.get("Authorization"), "Bearer test-key")

    def test_non_openrouter_provider_rejected(self):
        client = _client(provider="openai-compatible")
        client.client.post = MagicMock()
        with self.assertRaises(OpenRouterError):
            client.videos(model="vid/model", prompt="x")
        client.client.post.assert_not_called()

    def test_refuses_insecure_or_private_download_url(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-6", "polling_url": "/videos/job-6", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(200, {
            "status": "completed",
            "unsigned_urls": ["http://openrouter.ai/api/v1/videos/job-6/content"],
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-6"))

        with _no_poll_delay(), self.assertRaises(OpenRouterError):
            client.videos(model="vid/model", prompt="x")

    def test_submit_400_raises_immediately(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(
            400, {"error": {"message": "bad request"}}))
        with self.assertRaises(OpenRouterError):
            client.videos(model="vid/model", prompt="x")
        self.assertEqual(client.client.post.call_count, 1)

    def test_transient_poll_error_retries_in_loop(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-7", "polling_url": "/videos/job-7", "status": "pending",
        }))
        client.client.get = MagicMock(side_effect=[
            _response(500, {"error": "boom"}, method="GET",
                      url="https://openrouter.ai/api/v1/videos/job-7"),
            _response(200, {
                "status": "completed",
                "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-7/content?index=0"],
            }, method="GET", url="https://openrouter.ai/api/v1/videos/job-7"),
        ])
        client.client.stream = MagicMock(return_value=_FakeStream(MP4_BYTES))

        with _no_poll_delay():
            out = client.videos(model="vid/model", prompt="x")

        self.assertEqual(out["video_bytes"], MP4_BYTES)
        self.assertEqual(client.client.post.call_count, 1)
        self.assertEqual(client.client.get.call_count, 2)

    def test_redirect_on_poll_never_resubmits_job(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-8", "polling_url": "/videos/job-8", "status": "pending",
        }))
        redirect = _response(302, {}, method="GET",
                             url="https://openrouter.ai/api/v1/videos/job-8")
        redirect.headers["location"] = "https://evil.example.com/steal"
        client.client.get = MagicMock(return_value=redirect)

        with _no_poll_delay(), self.assertRaises(OpenRouterError):
            client.videos(model="vid/model", prompt="x")

        # bounded in-loop retries, never a second paid job, never a follow
        self.assertEqual(client.client.post.call_count, 1)
        self.assertTrue(all(
            call.kwargs.get("follow_redirects") is False
            for call in client.client.get.call_args_list
        ))

    def test_unsafe_polling_url_rejected_before_get(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-9", "polling_url": "http://169.254.169.254/job-9",
            "status": "pending",
        }))
        client.client.get = MagicMock()

        with _no_poll_delay(), self.assertRaises(OpenRouterVideoSubmittedError) as ctx:
            client.videos(model="vid/model", prompt="x")

        client.client.get.assert_not_called()
        # scheme://host only — never the full path/query
        self.assertIn("169.254.169.254", str(ctx.exception))
        self.assertNotIn("169.254.169.254/job-9", str(ctx.exception))

    def test_download_http_error_message_omits_url(self):
        client = _client()
        client.client.post = MagicMock(return_value=_response(202, {
            "id": "job-10", "polling_url": "/videos/job-10", "status": "pending",
        }))
        client.client.get = MagicMock(return_value=_response(200, {
            "status": "completed",
            "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-10/content?index=0&sig=SECRET"],
        }, method="GET", url="https://openrouter.ai/api/v1/videos/job-10"))
        client.client.stream = MagicMock(return_value=_FailingStream(403))

        with _no_poll_delay(), self.assertRaises(OpenRouterVideoSubmittedError) as ctx:
            client.videos(model="vid/model", prompt="x")

        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertNotIn("sig=", str(ctx.exception))


class TestPrivateHostCheck(unittest.TestCase):
    def test_canonical_and_non_canonical_literals(self):
        for host in (
            "localhost", "localhost.", "127.0.0.1", "0.0.0.0", "10.1.2.3",
            "192.168.1.1", "169.254.169.254", "::1",
            "2130706433",      # decimal 127.0.0.1
            "0x7f000001",      # hex 127.0.0.1
            "0177.0.0.1",      # octal 127.0.0.1
            "127.1",           # short form 127.0.0.1
            "100.101.102.103", # CGNAT / tailnet space
            "metadata.google.internal",
        ):
            self.assertTrue(_is_private_host(host), host)

    def test_public_hosts_allowed(self):
        for host in ("openrouter.ai", "cdn.example.com", "8.8.8.8"):
            self.assertFalse(_is_private_host(host), host)


if __name__ == "__main__":
    unittest.main()
