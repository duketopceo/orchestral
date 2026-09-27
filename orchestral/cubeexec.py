"""Isolated unittest execution through an E2B-compatible sandbox endpoint.

Selected by ``ORCHESTRAL_CODE_RUNTIME=isolated``. The endpoint is any
E2B-compatible API, addressed through the SDK's own env contract:

- ``E2B_DOMAIN`` — API base. Self-hosted CubeSandbox (a local node or a
  remote box such as a headless server) sets this to its gateway; unset, the
  SDK targets hosted E2B.
- ``E2B_API_KEY`` — control-plane credential. It is read by the SDK for the
  API handshake only; it is never written into the sandbox environment.
- ``ORCHESTRAL_CUBE_TEMPLATE`` — sandbox template/image ID (default
  ``code-interpreter``; self-hosted nodes create it with
  ``cubemastercli tpl create-from-image``).

Confidentiality: the worker fileset *and* the hidden verifier source are sent
to the endpoint. Self-hosted CubeSandbox keeps both on owned infrastructure;
hosted E2B discloses evaluation oracles to a third party — do not point
``E2B_DOMAIN`` at a host you do not control when grading holdout or
oracle-bearing tasks.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from orchestral.codeexec import shadowing_members, summarize_unittest_output
from orchestral.fileset import FilesetError, sanitize_path

E2B_TEMPLATE_ENV = "ORCHESTRAL_CUBE_TEMPLATE"
DEFAULT_TEMPLATE = "code-interpreter"
_RUNTIME_NAME = "e2b"
# Guest-side working directory and interpreter hardening: -Es ignores
# PYTHONPATH/PYTHONSTARTUP so a template default or leaked env cannot inject
# modules into the verifier process.
_WORKDIR = "/home/user"
_TEST_FILE = "task_tests.py"
# The guest env is template default plus this allowlist — os.environ is never
# forwarded, and E2B_API_KEY stays strictly control-plane.
_SANDBOX_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONHASHSEED": "0",
    "LANG": "C.UTF-8",
}


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
        "error": None,
    }


def _load_sandbox_class() -> tuple[Any, type[BaseException]] | None:
    """Return (e2b Sandbox class, e2b TimeoutException), or None if absent."""
    try:
        from e2b import Sandbox  # type: ignore[import-not-found]
        from e2b.exceptions import TimeoutException  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Sandbox, TimeoutException


def run_unittest_suite(
    files: dict[str, str],
    tests_source: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Run the hidden suite inside a disposable E2B-compatible sandbox.

    Same report contract as the disabled posture plus ``sandbox_image`` for
    backend provenance. The sandbox is destroyed on every exit path.
    """
    template = os.environ.get(E2B_TEMPLATE_ENV, DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
    report = _base_report(timeout_seconds, template)

    sdk = _load_sandbox_class()
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

    sandbox_cls, timeout_exc = sdk
    sandbox = None
    try:
        sandbox = sandbox_cls.create(
            template=template,
            timeout=int(timeout_seconds) + 30,
            allow_internet_access=False,
        )
        for rel, body in members:
            sandbox.files.write(f"{_WORKDIR}/{rel}", body)
        sandbox.files.write(f"{_WORKDIR}/{_TEST_FILE}", tests_source)
        try:
            result = sandbox.commands.run(
                f"python3 -Es -m unittest -v {_TEST_FILE[:-3]}",
                cwd=_WORKDIR,
                envs=dict(_SANDBOX_ENV),
                timeout=timeout_seconds,
            )
        except (TimeoutError, timeout_exc):
            report.update(
                executed=True,
                timed_out=True,
                error=f"suite exceeded {timeout_seconds}s",
            )
            return report
        report["executed"] = True
        report["returncode"] = result.exit_code
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        report["output_tail"] = "\n".join(tail[-15:])
        ran, error = summarize_unittest_output("\n".join(tail), result.exit_code)
        report["tests_run"] = ran
        report["ok"] = result.exit_code == 0 and ran > 0
        if error:
            report["error"] = error
    except Exception as exc:  # endpoint unreachable, auth, template missing…
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sandbox is not None:
            # Teardown failure must not mask the run result.
            with contextlib.suppress(Exception):
                sandbox.kill()
    return report
