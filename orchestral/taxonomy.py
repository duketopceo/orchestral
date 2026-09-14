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
    """
    name = type(exc).__name__

    # Post-submission video failures — never retryable (each retry bills a
    # new generation job). Checked before httpx so the label survives
    # whatever it wraps.
    if name in ("OpenRouterVideoSubmittedError", "OpenRouterVideoJobError"):
        return "submitted_job"
    if name == "ProviderConfigError":
        return "config"

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limit"
        if status in (401, 403):
            return "auth"
        return "provider_error"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "transport"
    if isinstance(exc, httpx.HTTPError):
        return "provider_error"

    # Runner ValidationError covers two distinct cases: "no output" (the
    # worker produced nothing) vs. a check failure. Split on the message.
    if name == "ValidationError":
        msg = str(exc).lower()
        if "no output" in msg or "no files" in msg:
            return "empty_output"
        return "validation"
    if name == "FilesetError" or isinstance(exc, json.JSONDecodeError):
        return "malformed_output"
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return "config"
    return "unknown"
