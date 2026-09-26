"""Failure taxonomy: stable error categories for analysis.

`classify_exception` maps an exception to one of CATEGORIES so events, the
calls table, and run metadata all speak the same vocabulary. Project
exception types are matched by class name rather than isinstance so this
module stays import-cycle-free (runner.py imports taxonomy; taxonomy must
not import runner).
"""

from __future__ import annotations

import json

import httpx

CATEGORIES = (
    "rate_limit",
    "auth",
    "timeout",
    "transport",
    "provider_error",
    "submitted_job",
    "malformed_output",
    "validation",
    "empty_output",
    "config",
    "unknown",
)


def classify_exception(exc: BaseException) -> str:
    """Map an exception to a stable category label.

    Ordering matters: `OpenRouterVideoSubmittedError` must win over generic
    HTTP/transport checks (it wraps post-submission failures that must never
    be retried), and `HTTPStatusError` must precede `TimeoutException`/
    `TransportError` since httpx's hierarchy is not a chain.

    Provider wrappers (`OpenRouterError`) re-raise httpx failures via
    `raise ... from exc`, so the real cause lives on `__cause__` — the
    unwrap walk below keeps rate_limit/auth/timeout reachable on the
    primary chat path, where everything is wrapped.
    """
    name = type(exc).__name__

    # Post-submission video failures — never retryable (each retry bills a
    # new generation job). Checked before httpx so the label survives
    # whatever it wraps.
    if name in ("OpenRouterVideoSubmittedError", "OpenRouterVideoJobError"):
        return "submitted_job"
    if name == "ProviderConfigError":
        return "config"

    http_err = _unwrap_httpx(exc)
    if http_err is not None:
        if isinstance(http_err, httpx.HTTPStatusError):
            status = http_err.response.status_code
            if status == 429:
                return "rate_limit"
            if status in (401, 403):
                return "auth"
            return "provider_error"
        if isinstance(http_err, httpx.TimeoutException):
            return "timeout"
        if isinstance(http_err, httpx.TransportError):
            return "transport"
        return "provider_error"
    if name == "OpenRouterError" or isinstance(exc, TimeoutError):
        # OpenRouterError without an httpx cause is still an API failure;
        # bare TimeoutError is a local timeout
        return "provider_error" if name == "OpenRouterError" else "timeout"

    # Runner ValidationError covers two distinct cases: "no output" (the
    # worker produced nothing) vs. a check failure. Split on the message.
    if name == "ValidationError":
        msg = str(exc).lower()
        if "no output" in msg or "no files" in msg:
            return "empty_output"
        return "validation"
    if name in ("FilesetError", "PlanError") or isinstance(exc, json.JSONDecodeError):
        return "malformed_output"
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return "config"
    return "unknown"


def _unwrap_httpx(exc: BaseException, depth: int = 6) -> httpx.HTTPError | None:
    """Return the first httpx error in the cause/context chain, or None."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and depth > 0 and id(cur) not in seen:
        if isinstance(cur, httpx.HTTPError):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
        depth -= 1
    return None
