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
runner itself lives outside the fileset directory — its ``sys.path[0]`` is a
nonce-named ``/tmp`` path worker members cannot populate, so worker files
cannot shadow the stdlib modules the runner imports. The runner exits via
``os._exit`` to deny atexit handlers a rewrite window; a determined artifact
can still discover the result path at runtime and race a detached writer —
the remaining bar is deliberate same-uid sabotage, not incidental output, and
``passes`` on graded tasks should be read as advisory against an adversarial
worker.
"""

from __future__ import annotations

import importlib
import inspect
import io
import json
import os
import re
import secrets
import shlex
import sys
import tarfile
import time
from pathlib import PurePosixPath
from typing import Any

from orchestral.codeexec import (
    shadowing_members,
    suite_result_report,
    suite_runner_source,
)
from orchestral.fileset import FilesetError, sanitize_path, sanitize_path_exec

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
# forwarded, and E2B_API_KEY stays strictly control-plane. PYTHONHASHSEED is
# inert under `python3 -E` (which drops all PYTHON* vars); kept so the env is
# already correct if -E is ever lifted.
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
# Values scrubbed from persisted error strings. E2B_DOMAIN isn't a credential
# but its hostname identifies the self-hosted endpoint — keep it out of
# publishable reports.
_SECRET_ENV_KEYS = (
    "E2B_API_KEY",
    "E2B_ACCESS_TOKEN",
    "OPENROUTER_API_KEY",
    "E2B_DOMAIN",
)
# A fileset member whose top-level name matches a stdlib module would shadow
# that module for the verifier runner and any suite imports (sys.path[0] is
# the suite directory at collection time).
_STDLIB_SHADOW = frozenset(sys.stdlib_module_names)

# Repo-suite additions to the shadowing denylist: a worker-written pytest.py,
# conftest.py, or pytest.ini would let the artifact grade itself.
_VERIFY_TOOLCHAIN_SHADOW = frozenset({
    "pytest", "_pytest", "pluggy", "conftest",
})
_VERIFY_CONFIG_FILES = frozenset({
    "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
})


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


def _load_sdk() -> tuple[Any, type[BaseException], type[BaseException]] | str | None:
    """Return (Sandbox, TimeoutException, CommandExitException), a reason
    string when the package imports but its surface is incompatible, or None
    when the SDK is absent."""
    try:
        import e2b  # type: ignore[import-not-found]
    except ImportError:
        return None
    sandbox_cls = getattr(e2b, "Sandbox", None)
    if not callable(sandbox_cls):
        return "e2b SDK installed but its surface is incompatible (no Sandbox)"

    def exc_class(name: str) -> type[BaseException] | None:
        # v2 exports exceptions under e2b.exceptions; some v1-compatible
        # distributions lack the submodule and export at package top level.
        try:
            exc_mod = importlib.import_module("e2b.exceptions")
        except ImportError:
            exc_mod = None
        for source in (exc_mod, e2b):
            resolved = getattr(source, name, None)
            if isinstance(resolved, type) and issubclass(resolved, BaseException):
                return resolved
        return None

    command_exit = exc_class("CommandExitException")
    if command_exit is None:
        return (
            "e2b SDK installed but its surface is incompatible "
            "(no CommandExitException)"
        )
    return sandbox_cls, exc_class("TimeoutException") or TimeoutError, command_exit


def _safe_error_message(exc: BaseException) -> str:
    """Bounded, credential-scrubbed error string for the persisted report."""
    msg = re.sub(r"\s+", " ", str(exc)).strip()
    for key in _SECRET_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            msg = msg.replace(value, "[redacted]")
    return f"{type(exc).__name__}: {msg[:200]}"


def _sdk_call(
    fn: Any, *args: Any, timeout: float, positional_tail: tuple[Any, ...] = ()
) -> Any:
    """Call an SDK client method with request_timeout in the right shape.

    v2's generated client accepts ``request_timeout`` as a kwarg; v1's write
    takes it positionally (v1's read/kill accept the kwarg on 1.11.x, verified
    live). Dispatch on the signature rather than retrying on TypeError — a
    TypeError raised *inside* a method would otherwise trigger a spurious
    second call (a double-write/double-kill window).
    """
    try:
        params = inspect.signature(fn).parameters
        accepts_kwarg = "request_timeout" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_kwarg = True  # uninspectable — assume the kwarg shape
    if accepts_kwarg:
        return fn(*args, request_timeout=timeout)
    return fn(*args, *positional_tail, timeout)


def _files_write(sandbox: Any, path: str, body: str | bytes, timeout: float) -> None:
    # v1's generated write signature is (path, data, user, request_timeout).
    _sdk_call(
        sandbox.files.write, path, body, timeout=timeout, positional_tail=("user",)
    )


def _create_sandbox(
    sandbox_cls: Any, timeout_seconds: float, request_timeout: float
) -> Any:
    """Create a sandbox on whichever SDK entry point accepts our kwargs.

    SDK v2 exposes Sandbox.create(); v1 only has the constructor. Pick
    the entry point from create's signature up front rather than
    catching TypeError after the fact — a TypeError raised *inside*
    create could post-date a server-side allocation, and retrying with
    the ctor would open a double-create window. If either path raises
    after allocation, the sandbox's own timeout bounds the orphan.
    """
    create_kwargs = {
        "template": os.environ.get(E2B_TEMPLATE_ENV, DEFAULT_TEMPLATE).strip()
        or DEFAULT_TEMPLATE,
        "timeout": int(timeout_seconds) + int(_DEADLINE_MARGIN_SECONDS),
        "allow_internet_access": False,
        "request_timeout": request_timeout,
    }
    create = getattr(sandbox_cls, "create", None)
    entry: Any = create if callable(create) else None
    if entry is not None:
        try:
            params = inspect.signature(entry).parameters
            if not (
                all(k in params for k in create_kwargs)
                or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in params.values()
                )
            ):
                entry = None  # signature rejects our kwargs — use the ctor
        except (TypeError, ValueError):
            pass  # uninspectable signature — assume it accepts the kwargs
    return (
        entry(**create_kwargs) if entry is not None else sandbox_cls(**create_kwargs)
    )


def _kill_sandbox(
    sandbox: Any, report: dict[str, Any], timeout_fn: Any
) -> None:
    """Destroy the sandbox and record the outcome honestly in the report."""
    last: BaseException | None = None
    for _ in range(2):
        try:
            # v1's return value is not a reliable outcome signal — falsy is
            # observed even when teardown succeeded. The label records what
            # the SDK reported, not ground truth.
            killed = _sdk_call(sandbox.kill, timeout=timeout_fn())
            report["sandbox_cleanup"] = "destroyed" if killed else "not_found"
            return
        except Exception as exc:
            last = exc
    report["sandbox_cleanup"] = "kill_failed"
    if report["error"] is None and last is not None:
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

    try:
        sdk = _load_sdk()
    except Exception as exc:
        report["error"] = f"e2b SDK failed to load: {_safe_error_message(exc)}"
        return report
    if sdk is None:
        report["error"] = (
            "e2b SDK not installed — `pip install orchestral[e2b]` "
            "or unset ORCHESTRAL_CODE_RUNTIME"
        )
        return report
    if isinstance(sdk, str):
        report["error"] = sdk
        return report

    try:
        members = []
        for rel, body in files.items():
            clean = sanitize_path(rel)
            if clean != sanitize_path_exec(rel):
                # sanitize_path case-folds; the guest fs is case-sensitive, so
                # folding silently renames a member like Main.java — reject
                # instead of misattributing the failure to the suite.
                raise FilesetError(
                    f"Path {rel!r} folds case to {clean!r}; sandboxed "
                    "execution requires case-canonical member names"
                )
            members.append((clean, body))
    except FilesetError as exc:
        report["error"] = f"artifact path rejected before sandbox write: {exc}"
        return report
    shadowed = shadowing_members(rel for rel, _ in members)
    # A top-level member named like a stdlib module (json.py, test/, …) would
    # shadow that module for the suite's imports once the workdir lands on
    # sys.path — reject rather than let worker files impersonate stdlib.
    shadowed += sorted(
        rel for rel, _ in members
        if (
            rel.split("/", 1)[0] if "/" in rel else PurePosixPath(rel).stem
        ) in _STDLIB_SHADOW
    )
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
        sandbox = _create_sandbox(sandbox_cls, timeout_seconds, request_timeout())
        for rel, body in members:
            if remaining() <= 0:
                report["error"] = "host deadline exceeded during sandbox writes"
                return report
            _files_write(sandbox, f"{_WORKDIR}/{rel}", body, request_timeout())
        _files_write(sandbox, f"{_WORKDIR}/{_TEST_FILE}", tests_source, request_timeout())
        # The verifier runner must live outside the worker fileset: a script's
        # sys.path[0] is its own directory, so a runner under _WORKDIR would
        # let a member like json.py shadow stdlib for the runner's imports and
        # forge the result payload.
        nonce = secrets.token_hex(8)
        runner_path = f"/tmp/orch_runner_{nonce}.py"
        result_path = f"/tmp/orch_result_{nonce}.json"
        _files_write(
            sandbox,
            runner_path,
            suite_runner_source(_WORKDIR, _TEST_FILE, result_path),
            request_timeout(),
        )
        if remaining() <= 0:
            report["error"] = "host deadline exceeded before suite start"
            return report
        stdout_tail = _Tail()
        stderr_tail = _Tail()
        try:
            cmd_result = sandbox.commands.run(
                f"python3 -Es {runner_path}",
                cwd=_WORKDIR,
                envs=dict(_SANDBOX_ENV),
                # SDK v1 serializes the timeout header as timeout*1000; a
                # float lands as "30000.0" which envd's ParseInt rejects.
                # Floor at 1s — a sub-second remaining() would truncate to 0,
                # whose envd semantics are undefined.
                timeout=max(1, int(min(timeout_seconds, remaining()))),
                request_timeout=request_timeout(),
                on_stdout=stdout_tail.feed,
                on_stderr=stderr_tail.feed,
            )
            returncode = int(getattr(cmd_result, "exit_code", 0) or 0)
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
        except timeout_exc:
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
            # v1's generated read signature is
            # (path, format, user, request_timeout) — it accepts the kwarg on
            # 1.11.x (verified live); the tail covers off-spec variants.
            raw = _sdk_call(
                sandbox.files.read, result_path, timeout=request_timeout(),
                positional_tail=("text", "user"),
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("non-dict result payload")
            counts = {
                key: int(payload.get(key) or 0)
                for key in ("tests_run", "failures", "errors", "skipped")
            }
        except Exception:
            report["error"] = (
                "no result payload from verifier runner — suite output is "
                "diagnostic-only and is never trusted for the verdict"
            )
            return report
        report.update(counts)
        ran, error = suite_result_report(payload, returncode)
        report["tests_run"] = ran
        report["ok"] = bool(payload.get("ok")) and ran > 0
        if error:
            report["error"] = error
    except Exception as exc:  # endpoint unreachable, auth, template missing…
        report["error"] = f"sandbox runtime error: {_safe_error_message(exc)}"
    finally:
        if sandbox is not None:
            _kill_sandbox(sandbox, report, request_timeout)
    return report


def _run_guest(
    sandbox: Any,
    cmd: str,
    *,
    cwd: str,
    budget_seconds: float,
    request_timeout: float,
    stdout_tail: _Tail,
    stderr_tail: _Tail,
    timeout_exc: type[BaseException],
    exit_exc: type[BaseException],
) -> int:
    """One commands.run with the shared tail capture + exit-code contract."""
    try:
        result = sandbox.commands.run(
            cmd,
            cwd=cwd,
            envs=dict(_SANDBOX_ENV),
            # SDK v1 serializes the timeout header as timeout*1000; a float
            # lands as "30000.0" which envd's ParseInt rejects.
            timeout=max(1, int(budget_seconds)),
            request_timeout=request_timeout,
            on_stdout=stdout_tail.feed,
            on_stderr=stderr_tail.feed,
        )
        return int(getattr(result, "exit_code", 0) or 0)
    except exit_exc as exc:
        # A nonzero exit is the normal failing-command shape, not an adapter
        # error — recover the code and any captured output.
        for chunk, sink in (
            (getattr(exc, "stdout", None), stdout_tail),
            (getattr(exc, "stderr", None), stderr_tail),
        ):
            if chunk and not sink.text:
                sink.feed(str(chunk))
        return int(getattr(exc, "exit_code", None) or 1)


def run_repo_suite(
    files: dict[str, str],
    *,
    fixture_tarball: bytes,
    fixture_id: str,
    workdir: str = "repo",
    setup_commands: list[str] | None = None,
    verify_command: list[str] | None = None,
    fail_to_pass: list[str] | None = None,
    test_files: dict[str, str] | None = None,
    timeout_seconds: float = 240.0,
) -> dict[str, Any]:
    """Stage a repo fixture in a disposable sandbox and run its test command.

    Same lifecycle + fail-closed report contract as ``run_unittest_suite``,
    plus fixture provenance (``fixture_id``, ``staged_members``,
    ``verify_command``, ``granularity: "command"``). The verdict is the verify
    command's exit code — guest output is diagnostic-only, never trusted.

    The tarball (``repo/`` + ``wheelhouse/`` top-levels) ships as raw bytes in
    one ``files.write`` and untars under ``_WORKDIR``; worker files overlay
    ``_WORKDIR/<workdir>/`` so a worker may return only the files it changed.
    ``setup_commands`` and ``verify_command`` run with the suite env — the
    guest never needs network (wheelhouse installs use ``--no-index``).
    """
    template = os.environ.get(E2B_TEMPLATE_ENV, DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
    report = _base_report(timeout_seconds, template)
    report["fixture_id"] = fixture_id
    report["granularity"] = "command"
    report["verify_command"] = list(verify_command or [])
    report["fail_to_pass"] = list(fail_to_pass or [])
    report["staged_members"] = 0

    try:
        sdk = _load_sdk()
    except Exception as exc:
        report["error"] = f"e2b SDK failed to load: {_safe_error_message(exc)}"
        return report
    if sdk is None:
        report["error"] = (
            "e2b SDK not installed — `pip install orchestral[e2b]` "
            "or unset ORCHESTRAL_CODE_RUNTIME"
        )
        return report
    if isinstance(sdk, str):
        report["error"] = sdk
        return report
    if not verify_command:
        report["error"] = "repo suite requires metadata.verify.command"
        return report
    if not fixture_tarball:
        report["error"] = f"fixture {fixture_id!r}: empty tarball — re-run `fixtures fetch`"
        return report

    try:
        members = []
        for rel, body in files.items():
            clean = sanitize_path(rel)
            if clean != sanitize_path_exec(rel):
                raise FilesetError(
                    f"Path {rel!r} folds case to {clean!r}; sandboxed "
                    "execution requires case-canonical member names"
                )
            members.append((clean, body))
    except FilesetError as exc:
        report["error"] = f"artifact path rejected before sandbox write: {exc}"
        return report

    # Oracle paths are verifier-authored but still validated before the
    # sandbox exists — and kept case-preserving, since the verify command
    # names the declared paths. sanitize_path would silently lowercase them.
    try:
        oracle = [
            (sanitize_path_exec(rel), body)
            for rel, body in (test_files or {}).items()
        ]
    except FilesetError as exc:
        report["error"] = f"test_files path rejected before sandbox write: {exc}"
        return report

    if PurePosixPath(workdir or "repo").name != "repo":
        report["error"] = (
            f"verify workdir {workdir!r} unsupported — the fixture extracts "
            "to repo/ only; an empty directory would grade nothing"
        )
        return report
    repo_dir = f"{_WORKDIR}/repo"

    # Repo test files the worker may not silently rewrite: any tarball member
    # under a test path that the oracle does not explicitly own. List members
    # host-side — the guest untar is literal bytes, no resolution needed.
    oracle_paths = {rel for rel, _ in oracle}
    repo_tests: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(fixture_tarball), mode="r:gz") as ftf:
            for m in ftf.getmembers():
                if not (m.isfile() and m.name.startswith("repo/")):
                    continue
                rel = m.name[5:]
                name = PurePosixPath(rel).name
                if (
                    "tests/" in rel
                    or name.startswith("test_")
                    or name.endswith("_test.py")
                ):
                    repo_tests.add(rel)
    except (tarfile.TarError, EOFError, OSError):
        # unreadable host-side → the guest untar reports its own error;
        # the overwrite guard simply has no baseline to check against
        repo_tests = set()

    # Worker members land under the staged repo, so the top-level shadowing
    # check applies to their repo-relative first segment just as it does to
    # the flat suite fileset. The verifier toolchain + config files join the
    # denylist: a worker-written pytest.py, conftest.py, or pytest.ini would
    # otherwise bypass grading.
    shadowed = shadowing_members(rel for rel, _ in members)
    shadowed += sorted(
        rel for rel, _ in members
        if (
            (
                rel.split("/", 1)[0] if "/" in rel else PurePosixPath(rel).stem
            ) in (_STDLIB_SHADOW | _VERIFY_TOOLCHAIN_SHADOW)
            or PurePosixPath(rel).name in _VERIFY_CONFIG_FILES
            or PurePosixPath(rel).name == "conftest.py"
            or (rel in repo_tests and rel not in oracle_paths)
        )
    )
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

    stdout_tail = _Tail()
    stderr_tail = _Tail()
    sandbox = None
    try:
        sandbox = _create_sandbox(sandbox_cls, timeout_seconds, request_timeout())

        # Stage: tarball bytes -> untar under _WORKDIR -> overlay worker files.
        _files_write(sandbox, "/tmp/_orch_fixture.tgz", fixture_tarball, request_timeout())
        rc = _run_guest(
            sandbox,
            f"mkdir -p {shlex.quote(repo_dir)} && "
            f"tar xzf /tmp/_orch_fixture.tgz -C {_WORKDIR}",
            cwd=_WORKDIR,
            budget_seconds=min(60.0, remaining()),
            request_timeout=request_timeout(),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            timeout_exc=timeout_exc,
            exit_exc=exit_exc,
        )
        if rc != 0:
            report.update(executed=True, returncode=rc,
                          error=f"fixture untar failed (exit {rc})")
            return report
        try:
            listing = sandbox.commands.run(
                f"find {shlex.quote(_WORKDIR)} -type f | wc -l",
                envs=dict(_SANDBOX_ENV),
                timeout=30,
                request_timeout=request_timeout(),
            )
            report["staged_members"] = int((listing.stdout or "0").strip() or 0)
        except Exception:
            pass  # provenance nicety — never gates the verdict

        for rel, body in members:
            if remaining() <= 0:
                report["error"] = "host deadline exceeded during sandbox writes"
                return report
            target = f"{repo_dir}/{rel}"
            _run_guest(
                sandbox,
                f"mkdir -p {shlex.quote(str(PurePosixPath(target).parent))}",
                cwd=_WORKDIR,
                budget_seconds=15.0,
                request_timeout=request_timeout(),
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                timeout_exc=timeout_exc,
                exit_exc=exit_exc,
            )
            _files_write(sandbox, target, body, request_timeout())

        # Oracle overlay lands after the worker fileset — a worker may not
        # overwrite the tests it is graded by. Validated pre-sandbox above.
        for rel, body in oracle:
            target = f"{repo_dir}/{rel}"
            _run_guest(
                sandbox,
                f"mkdir -p {shlex.quote(str(PurePosixPath(target).parent))}",
                cwd=_WORKDIR,
                budget_seconds=15.0,
                request_timeout=request_timeout(),
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                timeout_exc=timeout_exc,
                exit_exc=exit_exc,
            )
            _files_write(sandbox, target, body, request_timeout())
        report["oracle_members"] = len(oracle)

        report["executed"] = True
        for i, cmd in enumerate(setup_commands or []):
            if remaining() <= 0:
                report["error"] = "host deadline exceeded during setup"
                return report
            rc = _run_guest(
                sandbox, cmd, cwd=repo_dir,
                budget_seconds=min(120.0, remaining()),
                request_timeout=request_timeout(),
                stdout_tail=stdout_tail, stderr_tail=stderr_tail,
                timeout_exc=timeout_exc, exit_exc=exit_exc,
            )
            if rc != 0:
                report.update(
                    returncode=rc,
                    error=f"setup command {i + 1} failed (exit {rc}): {cmd[:80]}",
                    setup_failed=True,
                )
                tail = (stderr_tail.text or stdout_tail.text).strip().splitlines()
                report["output_tail"] = "\n".join(tail[-15:])[-4000:]
                return report

        if remaining() <= 0:
            report["error"] = "host deadline exceeded before verify"
            return report
        try:
            returncode = _run_guest(
                sandbox,
                shlex.join(verify_command),
                cwd=repo_dir,
                budget_seconds=min(timeout_seconds, remaining()),
                request_timeout=request_timeout(),
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                timeout_exc=timeout_exc,
                exit_exc=exit_exc,
            )
        except timeout_exc:
            report.update(
                timed_out=True,
                error=f"verify exceeded {timeout_seconds}s",
            )
            return report

        report["returncode"] = returncode
        report["ok"] = returncode == 0
        # Command granularity can't attribute which declared tests failed —
        # report the whole fail_to_pass batch as the outcome so a nonzero
        # exit scores 0, not (n-1)/n fake partial credit.
        declared = len(fail_to_pass or [])
        report["tests_run"] = declared if declared else 1
        report["failures"] = 0 if report["ok"] else report["tests_run"]
        report["errors"] = 0
        tail = (stderr_tail.text or stdout_tail.text).strip().splitlines()
        report["output_tail"] = "\n".join(tail[-15:])[-4000:]
    except Exception as exc:  # endpoint unreachable, auth, template missing…
        report["error"] = f"sandbox runtime error: {_safe_error_message(exc)}"
    finally:
        if sandbox is not None:
            _kill_sandbox(sandbox, report, request_timeout)
    return report
