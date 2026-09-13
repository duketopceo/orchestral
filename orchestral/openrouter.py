"""Minimal OpenRouter client for orchestral.

Tracks latency and returns usage so the runner can compute cost from the model
config. API keys are read from the environment only — never from disk or git.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import socket
import time
from typing import Any, Self
from urllib.parse import urlparse

import httpx

from orchestral.costs import token_usage_from_raw

MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 1.0
NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 422}
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

VIDEO_POLL_INTERVAL_SECONDS = 30.0
VIDEO_MAX_WAIT_SECONDS = 600.0
VIDEO_MAX_POLL_ERRORS = 3
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 300.0
_VIDEO_TERMINAL_STATUSES = {"failed", "cancelled", "expired"}

_PRIVATE_HOST_MARKERS = ("localhost", "127.", "0.", "10.", "192.168.", "169.254.", "::1")
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_VIDEO_BYTES = 100 * 1024 * 1024


def _is_private_host(hostname: str) -> bool:
    h = hostname.lower().rstrip(".")
    if h.endswith((".local", ".internal")):
        return True
    ip = _parse_ip_literal(h)
    if ip is not None:
        # is_global also rejects loopback, link-local, CGNAT (100.64/10),
        # documentation, and benchmarking ranges — not just RFC1918
        return not ip.is_global
    return any(h == m or h.startswith(m) for m in _PRIVATE_HOST_MARKERS)


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse canonical and non-canonical IP literals without doing DNS.

    `ipaddress` only accepts canonical forms; `socket.inet_aton` (pure parse,
    no lookup) additionally catches decimal, hex, octal, and short dotted
    IPv4 spellings that still resolve to loopback/private space.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


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

    def _headers(self, url: str | None = None) -> dict[str, str]:
        """Auth + attribution headers for API calls.

        When `url` is given (an API-returned URL rather than a known endpoint),
        the Authorization header is only sent when the URL's host matches the
        configured API origin — the bearer key must never leak to a
        provider-controlled or off-origin host.
        """
        if url is not None:
            parsed = urlparse(url)
            api_host = urlparse(str(self.client.base_url)).hostname
            if parsed.scheme != "https" or parsed.hostname != api_host:
                return {}
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/duketopceo/orchestral",
            "X-Title": "orchestral",
        }

    def _post_with_retry(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        attempt = 0
        last_error: Exception | None = None
        while attempt <= MAX_RETRIES:
            attempt += 1
            try:
                response = self.client.post(
                    path,
                    headers=self._headers(),
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

    def videos(
        self,
        *,
        model: str,
        prompt: str,
        duration: float | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        generate_audio: bool | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Generate a video via the asynchronous OpenRouter Videos API.

        Submits a job, polls its status to completion, then downloads the
        resulting bytes. Callers see one synchronous call shaped like
        `images`. Terminal job statuses raise `OpenRouterVideoJobError`,
        which must never be retried — a retry resubmits a billable job.
        """
        if self.provider != "openrouter":
            raise OpenRouterError(
                f"video generation is only supported for provider 'openrouter'; "
                f"provider {self.provider!r} is chat-only"
            )
        start = time.time()
        payload: dict[str, Any] = {"model": model, "prompt": prompt}
        for key, value in (
            ("duration", duration),
            ("resolution", resolution),
            ("aspect_ratio", aspect_ratio),
            ("generate_audio", generate_audio),
            ("seed", seed),
        ):
            if value is not None:
                payload[key] = value

        data = self._post_with_retry("/videos", payload).json()
        job_id = data.get("id")
        if not job_id:
            raise OpenRouterError("Malformed videos response: missing job id")

        # Everything below this point happens with a billable job already
        # submitted — failures raise OpenRouterVideoSubmittedError so the
        # runner's retry loop never resubmits a fresh paid generation.
        polling_url = self._api_url(data.get("polling_url") or f"/videos/{job_id}")
        parsed_poll = urlparse(polling_url)
        if parsed_poll.scheme != "https" or _is_private_host(parsed_poll.hostname or ""):
            raise OpenRouterVideoSubmittedError(
                f"video job {job_id}: refusing unsafe polling URL "
                f"{parsed_poll.scheme}://{parsed_poll.hostname}"
            )

        deadline = start + VIDEO_MAX_WAIT_SECONDS
        poll_errors = 0
        status: str | None = None
        while True:
            time.sleep(VIDEO_POLL_INTERVAL_SECONDS)
            try:
                # no redirects: a redirect could carry the Authorization header
                # to a foreign host, and the polling endpoint has no reason to
                # issue one
                poll = self.client.get(
                    polling_url,
                    headers=self._headers(polling_url),
                    follow_redirects=False,
                )
                if poll.is_redirect:
                    raise _PollingRedirectError()
                poll.raise_for_status()
                data = poll.json()
                poll_errors = 0
            except (ValueError, _PollingRedirectError) as exc:
                poll_errors += 1
                if poll_errors > VIDEO_MAX_POLL_ERRORS:
                    raise OpenRouterVideoSubmittedError(
                        f"video job {job_id} polling failed after "
                        f"{poll_errors} consecutive errors ({type(exc).__name__})"
                    ) from exc
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in NON_RETRYABLE_STATUSES:
                    raise OpenRouterVideoSubmittedError(
                        f"video job {job_id} polling rejected with HTTP "
                        f"{exc.response.status_code}"
                    ) from exc
                poll_errors += 1
                if poll_errors > VIDEO_MAX_POLL_ERRORS:
                    raise OpenRouterVideoSubmittedError(
                        f"video job {job_id} polling failed after "
                        f"{poll_errors} consecutive errors ({type(exc).__name__})"
                    ) from exc
            else:
                status = data.get("status") if isinstance(data, dict) else None
                if status in _VIDEO_TERMINAL_STATUSES:
                    error = data.get("error") or {}
                    detail = error.get("message") if isinstance(error, dict) else str(error or status)
                    raise OpenRouterVideoJobError(
                        f"video job {job_id} {status}: {detail}", status=status
                    )
                if status == "completed":
                    break
            if time.time() >= deadline:
                raise OpenRouterVideoSubmittedError(
                    f"video job {job_id} did not complete within "
                    f"{VIDEO_MAX_WAIT_SECONDS}s (last status: {status!r})"
                )

        content_urls = data.get("unsigned_urls")
        if not isinstance(content_urls, list):
            content_urls = []
        ref = str(content_urls[0]) if content_urls else f"/videos/{job_id}/content?index=0"
        url = self._api_url(ref)
        try:
            video_bytes = self._download(
                url,
                max_bytes=_MAX_VIDEO_BYTES,
                headers=self._headers(url),
                timeout=VIDEO_DOWNLOAD_TIMEOUT_SECONDS,
                kind="video",
            )
        except OpenRouterError as exc:
            raise OpenRouterVideoSubmittedError(
                f"video job {job_id} download failed: {exc}"
            ) from exc

        return {
            "id": job_id,
            "model": data.get("model", model),
            "video_bytes": video_bytes,
            "usage": data.get("usage", {}),
            "latency_ms": (time.time() - start) * 1000,
            "raw_response": data,
        }

    def _api_url(self, ref: str) -> str:
        """Resolve an API-returned URL reference against the configured base.

        Absolute URLs are used as-is. Relative refs join onto the full base
        path — `urljoin` can't be used because it drops the `/api/v1` prefix
        for root-relative refs like `/videos/x`.
        """
        if ref.startswith(("https://", "http://")):
            return ref
        return f"{str(self.client.base_url).rstrip('/')}/{ref.lstrip('/')}"

    def _download(
        self,
        url: str,
        *,
        max_bytes: int = _MAX_IMAGE_BYTES,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
        kind: str = "image",
    ) -> bytes:
        """Fetch a media artifact returned as a URL.

        The URL comes from the API response, so validate before fetching:
        https only, no loopback/private hosts, no redirects to off-host.
        """
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or _is_private_host(host):
            raise OpenRouterError(f"Refusing to download {kind} from untrusted URL: {parsed.scheme}://{host}")
        with self.client.stream(
            "GET", url, headers=headers or {}, follow_redirects=False, timeout=timeout
        ) as response:
            if response.is_redirect:
                raise OpenRouterError(
                    f"Refusing redirected {kind} download from {parsed.scheme}://{host}"
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # httpx's str(exc) embeds the request URL, which for media
                # downloads may be a credentialed CDN URL — keep it out
                raise OpenRouterError(
                    f"{kind.capitalize()} download failed: HTTP {exc.response.status_code}"
                ) from exc
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise OpenRouterError(f"{kind.capitalize()} download exceeded {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class OpenRouterError(Exception):
    """Raised when the OpenRouter API returns an error."""


class _PollingRedirectError(Exception):
    """The video polling endpoint returned a redirect — counted as a
    transient poll error so it never triggers a fresh billable job."""


class OpenRouterVideoSubmittedError(OpenRouterError):
    """A video job failed after submission — the job was already billable.

    The runner's retry loop must never retry this error: each retry would
    submit a new billable generation job. Raised for every post-submit
    failure (poll exhaustion, timeout, unsafe URLs, download errors).
    """


class OpenRouterVideoJobError(OpenRouterVideoSubmittedError):
    """A video generation job reached a terminal failure status.

    The runner's retry loop must never retry this error: each retry would
    submit a new billable generation job for a deterministically-failed one.
    """

    def __init__(self, message: str, *, status: str):
        super().__init__(message)
        self.status = status


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
