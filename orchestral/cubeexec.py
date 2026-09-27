"""Isolated unittest execution through an E2B-compatible sandbox endpoint.

Selected by ``ORCHESTRAL_CODE_RUNTIME=isolated``. The endpoint is any
E2B-compatible API, addressed through the SDK's own env contract:

- ``E2B_DOMAIN`` — API base. Self-hosted CubeSandbox (a local node or a
  remote box such as a headless server) sets this to its gateway; unset, the
  SDK targets hosted E2B.
- ``E2B_API_KEY`` — control-plane credential. It is read by the SDK for the
  API handshake only; it is never written into the sandbox environment and is
  scrubbed from any persisted error string.
- ``ORCHESTRAL_CUBE_TEMPLATE`` — sandbox template/image ID (default
  ``code-interpreter``; self-hosted nodes create it with
  ``cubemastercli tpl create-from-image``).

Confidentiality: the worker fileset *and* the hidden verifier source are sent
to the endpoint. Self-hosted CubeSandbox keeps both on owned infrastructure;
hosted E2B discloses evaluation oracles to a third party — do not point
``E2B_DOMAIN`` at a host you do not control when grading holdout or
oracle-bearing tasks.

Verdict channel: graded code runs inside the verifier process, so unittest's
stdout is forgeable in principle. The verdict instead comes from a JSON
payload the verifier-authored runner derives from the unittest result object
and writes to a nonce-named file, read back through the SDK file API. The
runner exits via ``os._exit`` to deny atexit handlers a rewrite window; a
determined artifact could still parse the runner source and race a background
writer — the remaining bar is deliberate sabotage, not incidental output.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Any

from orchestral.codeexec import (
    shadowing_members,
    suite_result_report,
    suite_runner_source,
)
from orchestral.fileset import FilesetError, sanitize_path

E2B_TEMPLATE_ENV = "ORCHESTRAL_CUBE_TEMPLATE"
DEFAULT_TEMPLATE = "code-interpreter"
_RUNTIME_NAME = "e2b"
# Guest-side working directory. The suite runs under the verifier runner
# (not `python -m unittest`), which derives its verdict from the result
# object rather than forgeable stdout.
_WORKDIR = "/home/user"
_TEST_FILE = "task_tests.py"
_RUNNER_FILE = "_orch_runner.py"
# The guest env is template default plus this allowlist — os.environ is never
# forwarded, and E2B_API_KEY stays strictly control-plane.
_SANDBOX_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONHASHSEED": "0",
    "LANG": "C.UTF-8",
}
# Host-side wall-clock margin over the suite timeout for create/write/read/
# teardown, and the ceiling applied to each retained output stream. Graded
# code may write to stdout until timeout; the tail keeps only the trailing
# bytes the report actually needs.
_DEADLINE_MARGIN_SECONDS = 30.0
_REQUEST_TIMEOUT_CAP = 60.0
_STREAM_TAIL_BYTES = 64_000
_SECRET_ENV_KEYS = ("E2B_API_KEY", "E2B_ACCESS_TOKEN", "OPENROUTER_API_KEY")


def _base_report(timeout_seconds: float, template: str) -> dict[str, Any]:
    return {
        "executed": False,
        "tests_run": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "ok": False,
        "timed_out": False,
        "returncode": None,
        "output_tail": "",
        "runtime": _RUNTIME_NAME,
        "sandbox_image": template,
        "requested_timeout_seconds": timeout_seconds,
        "sandbox_cleanup": "none",
        "error": None,
    }


class _Tail:
    """Bounded rolling capture for streamed stdout/stderr chunks."""

    def __init__(self, limit: int = _STREAM_TAIL_BYTES) -> None:
        self._buf = ""
        self._limit = limit

    def feed(self, chunk: str) -> None:
        self._buf = (self._buf + chunk)[-self._limit:]

    @property
    def text(self) -> str:
        return self._buf


def _load_sdk() -> tuple[Any, type[BaseException], type[BaseException]] | None:
    """Return (Sandbox, TimeoutException, CommandExitException) or None."""
    try:
        from e2b import Sandbox  # type: ignore[import-not-found]
        from e2b.exceptions import (  # type: ignore[import-not-found]
            CommandExitException,
            TimeoutException,
        )
    except ImportError:
        return None
    return Sandbox, TimeoutException, CommandExitException


def _safe_error_message(exc: BaseException) -> str:
    """Bounded, credential-scrubbed error string for the persisted report."""
    msg = re.sub(r"\s+", " ", str(exc)).strip()
    for key in _SECRET_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            msg = msg.replace(value, "[redacted]")
    return f"{type(exc).__name__}: {msg[:200]}"


def _kill_sandbox(sandbox: Any, report: dict[str, Any]) -> None:
    """Destroy the sandbox and record the outcome honestly in the report."""
    for _ in range(2):
        try:
            if sandbox.kill(request_timeout=_REQUEST_TIMEOUT_CAP):
                report["sandbox_cleanup"] = "destroyed"
                return
            report["sandbox_cleanup"] = "not_found"
            return
        except Exception as exc:
            last = exc
    report["sandbox_cleanup"] = "kill_failed"
    if report["error"] is None:
        report["error"] = f"sandbox teardown failed: {_safe_error_message(last)}"


def run_unittest_suite(
    files: dict[str, str],
    tests_source: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Run the hidden suite inside a disposable E2B-compatible sandbox.

    Same report contract as the disabled posture plus ``sandbox_image`` and
    ``sandbox_cleanup`` provenance. The host enforces a wall-clock deadline
    across create/write/run/read/teardown — the suite timeout alone does not
    bound a stalled endpoint.
    """
    template = os.environ.get(E2B_TEMPLATE_ENV, DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
    report = _base_report(timeout_seconds, template)

    sdk = _load_sdk()
    if sdk is None:
        report["error"] = (
            "e2b SDK not installed — `pip install orchestral[e2b]` "
            "or unset ORCHESTRAL_CODE_RUNTIME"
        )
        return report

    try:
        members = [(sanitize_path(rel), body) for rel, body in files.items()]
    except FilesetError as exc:
        report["error"] = f"artifact path rejected before sandbox write: {exc}"
        return report
    shadowed = shadowing_members(rel for rel, _ in members)
    if shadowed:
        report["error"] = (
            "artifact members shadow the suite runtime: " + ", ".join(shadowed[:5])
        )
        return report

    sandbox_cls, timeout_exc, exit_exc = sdk
    deadline = time.monotonic() + timeout_seconds + _DEADLINE_MARGIN_SECONDS

    def remaining() -> float:
        return deadline - time.monotonic()

    def request_timeout() -> float:
        return max(1.0, min(_REQUEST_TIMEOUT_CAP, remaining()))

    sandbox = None
    try:
        sandbox = sandbox_cls.create(
            template=template,
            timeout=int(timeout_seconds) + int(_DEADLINE_MARGIN_SECONDS),
            allow_internet_access=False,
            request_timeout=request_timeout(),
        )
        for rel, body in members:
            sandbox.files.write(
                f"{_WORKDIR}/{rel}", body, request_timeout=request_timeout()
            )
        sandbox.files.write(
            f"{_WORKDIR}/{_TEST_FILE}", tests_source,
            request_timeout=request_timeout(),
        )
        result_path = f"{_WORKDIR}/.orch-result-{secrets.token_hex(8)}.json"
        sandbox.files.write(
            f"{_WORKDIR}/{_RUNNER_FILE}",
            suite_runner_source(_WORKDIR, _TEST_FILE, result_path),
            request_timeout=request_timeout(),
        )
        if remaining() <= 0:
            report["error"] = "host deadline exceeded before suite start"
            return report
        stdout_tail = _Tail()
        stderr_tail = _Tail()
        try:
            sandbox.commands.run(
                f"python3 -Es {_RUNNER_FILE}",
                cwd=_WORKDIR,
                envs=dict(_SANDBOX_ENV),
                timeout=min(timeout_seconds, remaining()),
                request_timeout=request_timeout(),
                on_stdout=stdout_tail.feed,
                on_stderr=stderr_tail.feed,
            )
            returncode = 0
        except exit_exc as exc:
            # A nonzero exit is the normal failing-suite shape, not an
            # adapter error — recover the code and any captured output.
            returncode = int(getattr(exc, "exit_code", None) or 1)
            for chunk, sink in (
                (getattr(exc, "stdout", None), stdout_tail),
                (getattr(exc, "stderr", None), stderr_tail),
            ):
                if chunk and not sink.text:
                    sink.feed(str(chunk))
        except (TimeoutError, timeout_exc):
            report.update(
                executed=True,
                timed_out=True,
                error=f"suite exceeded {timeout_seconds}s",
            )
            return report
        report["executed"] = True
        report["returncode"] = returncode
        tail = (stderr_tail.text or stdout_tail.text).strip().splitlines()
        report["output_tail"] = "\n".join(tail[-15:])[-4000:]
        try:
            raw = sandbox.files.read(result_path, request_timeout=request_timeout())
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("non-dict result payload")
        except Exception:
            report["error"] = (
                "no result payload from verifier runner — suite output is "
                "diagnostic-only and is never trusted for the verdict"
            )
            return report
        for key in ("tests_run", "failures", "errors", "skipped"):
            report[key] = int(payload.get(key) or 0)
        ran, error = suite_result_report(payload, returncode)
        report["tests_run"] = ran
        report["ok"] = bool(payload.get("ok")) and ran > 0
        if error:
            report["error"] = error
    except Exception as exc:  # endpoint unreachable, auth, template missing…
        report["error"] = f"sandbox runtime error: {_safe_error_message(exc)}"
    finally:
        if sandbox is not None:
            _kill_sandbox(sandbox, report)
    return report
