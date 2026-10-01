"""`harness.py doctor` — preflight diagnostics for the isolated code runtime.

Verifies the self-hosted E2B-compatible contract end to end before a paid
run: env vars present, SDK installed and wire-compatible, api.<domain>
resolves, TLS trusts the endpoint cert, the template exists, and a real
sandbox can be created, exec'd, and destroyed. The probe drives a trivial
suite through ``cubeexec.run_unittest_suite`` itself, so it exercises the
actual adapter path (file writes, nonce runner, result payload, teardown)
rather than a look-alike.

Exit code is 0 only when every required check passes. The egress probe is
informational — CubeSandbox is known to ignore ``allow_internet_access`` —
and reports rather than fails.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import socket
import ssl
import sys
from dataclasses import dataclass, field
from typing import Any

from orchestral.codeexec import CODE_RUNTIME_ENV, ISOLATED_CODE_RUNTIME
from orchestral.cubeexec import (
    E2B_TEMPLATE_ENV,
    _load_sdk,
    _sdk_call,
    run_unittest_suite,
)

_PROBE_TIMEOUT = 90.0
# Hosted E2B's default domain; any other domain is a self-hosted deployment
# and (as of CubeSandbox) serves only the v1 REST surface.
_HOSTED_DOMAIN = "e2b.dev"


@dataclass
class _Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


@dataclass
class _Result:
    checks: list[_Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        self.checks.append(_Check(name, ok, detail, required))

    @property
    def failed(self) -> list[_Check]:
        return [c for c in self.checks if c.required and not c.ok]


def _env_checks(result: _Result) -> dict[str, str]:
    runtime = os.environ.get(CODE_RUNTIME_ENV, "")
    result.add(
        CODE_RUNTIME_ENV,
        runtime == ISOLATED_CODE_RUNTIME,
        runtime or "unset — set to 'isolated' to enable the runtime",
    )
    domain = os.environ.get("E2B_DOMAIN", "")
    key = os.environ.get("E2B_API_KEY", "")
    template = os.environ.get(E2B_TEMPLATE_ENV, "")
    cert = os.environ.get("SSL_CERT_FILE", "")
    result.add("E2B_DOMAIN", bool(domain), domain or "unset")
    result.add("E2B_API_KEY", bool(key), "set" if key else "unset")
    result.add(
        E2B_TEMPLATE_ENV,
        bool(template),
        template or "unset (adapter default is code-interpreter)",
        required=False,
    )
    # certifi ignores the OS trust store — a self-signed domain needs the var
    self_hosted = bool(domain) and domain != _HOSTED_DOMAIN
    result.add(
        "SSL_CERT_FILE",
        bool(cert) or not self_hosted,
        cert or ("unset — required when <domain> uses a local CA" if self_hosted else "unset"),
        required=self_hosted,
    )
    return {"domain": domain, "key": key, "template": template or "code-interpreter"}


def _sdk_check(result: _Result, domain: str) -> tuple[Any, Any, Any] | None:
    loaded = _load_sdk()
    if loaded is None:
        result.add("e2b SDK", False, "not installed — pip install 'orchestral[e2b]'")
        return None
    if isinstance(loaded, str):
        result.add("e2b SDK", False, loaded)
        return None
    try:
        version = importlib.metadata.version("e2b")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    self_hosted = bool(domain) and domain != _HOSTED_DOMAIN
    major = int(version.split(".", 1)[0]) if version[:1].isdigit() else 0
    if self_hosted and major >= 2:
        result.add(
            "e2b SDK",
            False,
            f"v{version} — CubeSandbox serves only the v1 REST surface; pin 'e2b<2'",
        )
        return None
    result.add("e2b SDK", True, f"v{version}")
    return loaded


def _dns_check(result: _Result, domain: str) -> None:
    if not domain:
        result.add("api.<domain> DNS", False, "skipped — E2B_DOMAIN unset")
        return
    host = f"api.{domain}"
    try:
        addr = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        result.add("api.<domain> DNS", True, f"{host} -> {addr[0][4][0]}")
    except OSError as exc:
        result.add("api.<domain> DNS", False, f"{host}: {exc}")


def _tls_check(result: _Result, domain: str) -> None:
    if not domain:
        result.add("api.<domain> TLS", False, "skipped — E2B_DOMAIN unset")
        return
    host = f"api.{domain}"
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as sock, ctx.wrap_socket(
            sock, server_hostname=host
        ):
            result.add("api.<domain> TLS", True, f"handshake verified ({host})")
    except Exception as exc:
        result.add("api.<domain> TLS", False, f"{host}: {type(exc).__name__}: {exc}")


def _probe_check(result: _Result) -> None:
    """Real create/exec/destroy through the adapter — verdict comes back via
    the nonce result file, teardown is the adapter's own finally path."""
    report = run_unittest_suite(
        {"probe_app.py": "def add(a, b):\n    return a + b\n"},
        (
            "import unittest\n"
            "from probe_app import add\n"
            "class _T(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(1, 2), 3)\n"
        ),
        timeout_seconds=_PROBE_TIMEOUT,
    )
    if report["executed"] and report["ok"]:
        result.add(
            "sandbox probe",
            True,
            f"created+exec'd+destroyed via {report['sandbox_image']} "
            f"({report['tests_run']} test, cleanup={report['sandbox_cleanup']})",
        )
    else:
        result.add(
            "sandbox probe",
            False,
            report.get("error") or report.get("output_tail", "")[:200] or "see report",
        )


def _egress_check(result: _Result, loaded: tuple[Any, Any, Any], template: str) -> None:
    """Informational: attempt outbound HTTPS from inside a sandbox. CubeAPI
    ignores allow_internet_access=False — report what the deployment actually
    does instead of trusting the flag."""
    sandbox_cls = loaded[0]
    sb = None
    try:
        entry = getattr(sandbox_cls, "create", None)
        kwargs: dict[str, Any] = {
            "template": template,
            "timeout": int(_PROBE_TIMEOUT),
        }
        sb = entry(**kwargs) if callable(entry) else sandbox_cls(**kwargs)
        proc = _sdk_call(
            sb.commands.run,
            "curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://pypi.org",
            timeout=15.0,
        )
        reachable = getattr(proc, "stdout", "").strip()
        result.add(
            "guest egress",
            True,
            f"OPEN — pypi.org returned HTTP {reachable} "
            "(allow_internet_access is not enforced by this endpoint; "
            "deny egress at the deployment level for oracle-bearing tasks)",
            required=False,
        )
    except Exception as exc:
        # A failing curl surfaces as a non-zero exit — that is a closed egress
        detail = str(exc)[:120]
        result.add(
            "guest egress",
            True,
            f"closed (outbound attempt failed: {detail})",
            required=False,
        )
    finally:
        if sb is not None:
            # diagnostic only — the sandbox timeout bounds the orphan
            with contextlib.suppress(Exception):
                _sdk_call(sb.kill, timeout=10.0, positional_tail=(10.0,))


def run_doctor(*, probe: bool = True, egress: bool = True) -> int:
    """Run all checks, print a report, return process exit code."""
    result = _Result()
    env = _env_checks(result)
    loaded = _sdk_check(result, env["domain"])
    if env["domain"]:
        _dns_check(result, env["domain"])
        _tls_check(result, env["domain"])
    if probe and loaded is not None and env["domain"] and env["key"]:
        _probe_check(result)
        if egress:
            _egress_check(result, loaded, env["template"])
    elif probe:
        result.add("sandbox probe", False, "skipped — earlier checks failed")

    width = max(len(c.name) for c in result.checks) if result.checks else 10
    for c in result.checks:
        mark = "ok" if c.ok else ("FAIL" if c.required else "warn")
        print(f"{mark:>4}  {c.name:<{width}}  {c.detail}")
    if result.failed:
        print(f"\ndoctor: {len(result.failed)} required check(s) failed", file=sys.stderr)
        return 1
    print("\ndoctor: all required checks passed")
    return 0
