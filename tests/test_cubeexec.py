"""Adapter contract tests for the E2B-compatible isolated runtime.

The SDK is an optional `[e2b]` extra, so the suite injects a fake `e2b`
module; what is under test is the adapter's contract: report shape, path
sanitization before sandbox write, environment isolation, timeout mapping,
the out-of-band result payload, and kill-on-every-exit teardown.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from orchestral import codeexec, cubeexec


class _FakeResult:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeTimeout(Exception):
    pass


class _FakeCommandExit(Exception):
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        super().__init__(f"Command failed with exit code {exit_code}")
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeFiles:
    def __init__(self, written: dict[str, str]) -> None:
        self._written = written

    def write(self, path: str, body: str, **_kwargs) -> None:
        self._written[path] = body

    def read(self, path: str, **_kwargs) -> str:
        if path not in self._written:
            raise FileNotFoundError(path)
        return self._written[path]


class _FakeCommands:
    """Simulates the verifier runner: writes the result payload file, streams
    output through the callbacks, then raises on nonzero exit."""

    def __init__(
        self,
        payload: dict | None = None,
        payload_raw: str | None = None,
        stdout: str = "",
        stderr: str = "",
        exc: Exception | None = None,
        writes_payload: bool = True,
    ) -> None:
        self.calls: list[dict] = []
        self._payload = payload if payload is not None else {
            "tests_run": 3, "failures": 0, "errors": 0, "skipped": 0, "ok": True,
        }
        self._payload_raw = payload_raw
        self._stdout = stdout
        self._stderr = stderr
        self._exc = exc
        self._writes_payload = writes_payload
        self._written: dict[str, str] = {}

    def bind(self, written: dict[str, str]) -> None:
        self._written = written

    def run(self, command: str, **kwargs) -> _FakeResult:
        self.calls.append({"command": command, **kwargs})
        if self._writes_payload:
            runner = next(
                (v for k, v in self._written.items()
                 if k.startswith("/tmp/orch_runner_")),
                "",
            )
            match = re.search(r"open\('([^']+\.json)'", runner)
            assert match, "runner source must embed a result path"
            raw = self._payload_raw or json.dumps(self._payload)
            self._written[match.group(1)] = raw
        for chunk, cb in (
            (self._stdout, kwargs.get("on_stdout")),
            (self._stderr, kwargs.get("on_stderr")),
        ):
            if chunk and cb:
                cb(chunk)
        if self._exc is not None:
            raise self._exc
        if isinstance(self._payload, dict) and self._payload.get("ok"):
            return _FakeResult(0, stdout=self._stdout, stderr=self._stderr)
        raise _FakeCommandExit(1, stdout=self._stdout, stderr=self._stderr)


class _FakeSandboxBase:
    """Records the SDK surface the adapter uses."""

    created: ClassVar[list[dict]] = []
    instances: ClassVar[list[_FakeSandboxBase]] = []
    killed_count: ClassVar[int] = 0
    next_commands: ClassVar[_FakeCommands | None] = None
    create_exc: ClassVar[Exception | None] = None
    kill_exc: ClassVar[Exception | None] = None
    kill_return: ClassVar[bool] = True
    # Per-attempt kill outcomes; consumed in order. An Exception entry raises.
    kill_outcomes: ClassVar[list] = []

    @classmethod
    def reset(cls) -> None:
        cls.created = []
        cls.instances = []
        cls.killed_count = 0
        cls.next_commands = None
        cls.create_exc = None
        cls.kill_exc = None
        cls.kill_return = True
        cls.kill_outcomes = []

    def __init__(self, **kwargs) -> None:
        _FakeSandboxBase.created.append(kwargs)
        if _FakeSandboxBase.create_exc is not None:
            raise _FakeSandboxBase.create_exc
        self.written: dict[str, str] = {}
        self.files = _FakeFiles(self.written)
        commands = _FakeSandboxBase.next_commands or _FakeCommands(
            stderr="\nRan 3 tests in 0.01s\n\nOK\n"
        )
        commands.bind(self.written)
        self.commands = commands
        _FakeSandboxBase.instances.append(self)

    def _kill_impl(self) -> bool:
        _FakeSandboxBase.killed_count += 1
        if _FakeSandboxBase.kill_outcomes:
            outcome = _FakeSandboxBase.kill_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return bool(outcome)
        if _FakeSandboxBase.kill_exc is not None:
            raise _FakeSandboxBase.kill_exc
        return _FakeSandboxBase.kill_return

    def kill(self, **_kwargs) -> bool:
        return self._kill_impl()


class FakeSandbox(_FakeSandboxBase):
    """v2 shape: Sandbox.create() classmethod is the entry point."""

    @classmethod
    def create(cls, **kwargs):
        return cls(**kwargs)


class _FakeFilesV1(_FakeFiles):
    """v1's generated signature takes request_timeout positionally;
    the kwarg raises TypeError."""
    positional_calls: ClassVar[list[tuple]] = []

    def write(self, path: str, body: str, *args) -> None:
        # Positional-only like real v1: no **kwargs in the signature, so
        # Python binding itself raises TypeError on a request_timeout kwarg.
        _FakeFilesV1.positional_calls.append((path, body, *args))
        self._written[path] = body


class FakeSandboxV1(_FakeSandboxBase):
    """v1 shape: bare constructor (no create classmethod), positional-only
    files.write, positional kill.

    Modeled on e2b 1.11.x as live-verified against CubeSandbox: files.read
    and commands.run accept their kwargs on real v1, so only write and kill
    reject them here."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.files = _FakeFilesV1(self.written)

    def kill(self, *args) -> bool:
        # Positional-only like real v1's generated signature.
        return self._kill_impl()


def _install_fake_e2b() -> dict[str, types.ModuleType]:
    """Fake the SDK v2 surface: exceptions live under e2b.exceptions."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    exc_mod = types.ModuleType("e2b.exceptions")
    exc_mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    exc_mod.CommandExitException = _FakeCommandExit  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": exc_mod}


def _install_fake_e2b_v1() -> dict[str, types.ModuleType]:
    """Fake the SDK v1 surface: CommandExitException at package top level,
    exceptions module carries TimeoutException only."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandboxV1  # type: ignore[attr-defined]
    mod.CommandExitException = _FakeCommandExit  # type: ignore[attr-defined]
    mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    exc_mod = types.ModuleType("e2b.exceptions")
    exc_mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": exc_mod}


def _install_fake_e2b_v1_no_exc_module() -> dict[str, types.ModuleType | None]:
    """Fake a v1-compatible distribution with no e2b.exceptions submodule —
    all exports live at the package top level. A None sys.modules entry makes
    `import e2b.exceptions` raise ImportError regardless of any real install."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandboxV1  # type: ignore[attr-defined]
    mod.CommandExitException = _FakeCommandExit  # type: ignore[attr-defined]
    mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": None}


def _install_fake_e2b_incompatible() -> dict[str, types.ModuleType | None]:
    """Fake an SDK that imports but lacks CommandExitException anywhere."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": None}


def _install_fake_e2b_no_sandbox() -> dict[str, types.ModuleType | None]:
    """Fake an SDK that imports but exports no Sandbox at all."""
    mod = types.ModuleType("e2b")
    mod.CommandExitException = _FakeCommandExit  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": None}


def _install_fake_e2b_no_timeout() -> dict[str, types.ModuleType | None]:
    """Fake an SDK with no TimeoutException anywhere — the adapter must
    fall back to builtin TimeoutError."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    mod.CommandExitException = _FakeCommandExit  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": None}


class FakeSandboxCreateMismatch(_FakeSandboxBase):
    """create() exists but accepts none of the create kwargs — signature
    inspection must reject it before it's ever called; a post-allocation
    TypeError retry would risk a double-create."""

    @classmethod
    def create(cls):
        raise AssertionError("create() must not be called")


def _install_fake_e2b_create_mismatch() -> dict[str, types.ModuleType | None]:
    """Fake an SDK whose create() signature rejects the adapter kwargs."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandboxCreateMismatch  # type: ignore[attr-defined]
    exc_mod = types.ModuleType("e2b.exceptions")
    exc_mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    exc_mod.CommandExitException = _FakeCommandExit  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": exc_mod}


class CubeExecBase(unittest.TestCase):
    def setUp(self) -> None:
        _FakeSandboxBase.reset()
        _FakeFilesV1.positional_calls = []

    def _run(self, files=None, tests="import unittest\n", timeout=30.0,
             install=_install_fake_e2b):
        files = files if files is not None else {"solution.py": "def f():\n    return 1\n"}
        with patch.dict(sys.modules, install()):
            return cubeexec.run_unittest_suite(files, tests, timeout_seconds=timeout)


class TestMissingSdk(CubeExecBase):
    def test_missing_sdk_reports_cleanly(self) -> None:
        with patch.object(cubeexec, "_load_sdk", return_value=None):
            r = cubeexec.run_unittest_suite({"a.py": "x = 1\n"}, "import unittest\n")
        self.assertFalse(r["executed"])
        self.assertEqual(r["runtime"], "e2b")
        self.assertIn("e2b", r["error"])

    def test_default_runtime_stays_disabled(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(codeexec.CODE_RUNTIME_ENV, None)
            r = codeexec.run_unittest_suite({"a.py": "x = 1\n"}, "import unittest\n")
        self.assertFalse(r["executed"])
        self.assertEqual(r["runtime"], "disabled")

    def test_isolated_env_dispatches_to_adapter(self) -> None:
        with patch.dict("os.environ", {codeexec.CODE_RUNTIME_ENV: "isolated"}), \
             patch.object(cubeexec, "run_unittest_suite",
                          return_value={"runtime": "e2b", "ok": True}) as spy:
            r = codeexec.run_unittest_suite({"a.py": "x = 1\n"}, "import unittest\n")
        spy.assert_called_once()
        self.assertEqual(r["runtime"], "e2b")


class TestHappyPath(CubeExecBase):
    def test_passing_suite_reports_ok(self) -> None:
        r = self._run()
        self.assertTrue(r["executed"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["tests_run"], 3)
        self.assertIsNone(r["error"])
        self.assertEqual(r["sandbox_cleanup"], "destroyed")
        self.assertEqual(_FakeSandboxBase.killed_count, 1)

    def test_files_and_tests_written_under_workdir(self) -> None:
        self._run(files={"pkg/mod.py": "X = 1\n"})
        self.assertIs(_FakeSandboxBase.created[0].get("allow_internet_access"), False)
        written = _FakeSandboxBase.instances[0].written
        self.assertEqual(written["/home/user/pkg/mod.py"], "X = 1\n")
        self.assertIn("/home/user/task_tests.py", written)
        # the verifier runner lives outside the fileset dir so a worker
        # member can't shadow stdlib for its imports
        runner_keys = [k for k in written if k.startswith("/tmp/orch_runner_")]
        self.assertEqual(len(runner_keys), 1)

    def test_guest_env_is_allowlist_only(self) -> None:
        cmds = _FakeCommands(
            payload={"tests_run": 1, "ok": True}, stderr="Ran 1 tests\nOK\n"
        )
        _FakeSandboxBase.next_commands = cmds
        with patch.dict(os.environ, {
            "E2B_API_KEY": "sentinel-a",
            "OPENROUTER_API_KEY": "sentinel-b",
        }):
            self._run()
        envs = cmds.calls[0]["envs"]
        self.assertEqual(set(envs), {"PATH", "PYTHONHASHSEED", "LANG"})

    def test_verdict_comes_from_result_payload_not_stdout(self) -> None:
        # stdout claims a huge pass; the verifier payload says 1 ran, failed.
        _FakeSandboxBase.next_commands = _FakeCommands(
            payload={"tests_run": 1, "failures": 1, "errors": 0, "skipped": 0, "ok": False},
            stderr="Ran 99 tests in 0.01s\n\nOK\n",
        )
        r = self._run()
        self.assertEqual(r["tests_run"], 1)
        self.assertFalse(r["ok"])

    def test_output_tail_is_bounded_by_stream_callbacks(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(
            payload={"tests_run": 1, "ok": True},
            stderr="x" * (cubeexec._STREAM_TAIL_BYTES + 5000) + "\nOK\n",
        )
        r = self._run()
        self.assertLessEqual(len(r["output_tail"]), 4000)


class TestFailurePaths(CubeExecBase):
    def test_failing_suite_reports_exit_code(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(
            payload={"tests_run": 4, "failures": 1, "errors": 0, "skipped": 0, "ok": False},
            stderr="Ran 4 tests\nFAILED (failures=1)",
        )
        r = self._run()
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["tests_run"], 4)
        self.assertEqual(r["returncode"], 1)
        self.assertIn("unittest exited 1", r["error"])
        self.assertEqual(_FakeSandboxBase.killed_count, 1)

    def test_zero_tests_is_a_finding(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(
            payload={"tests_run": 0, "failures": 0, "errors": 0, "skipped": 0, "ok": True},
            stderr="NO TESTS RAN",
        )
        r = self._run()
        self.assertFalse(r["ok"])
        self.assertIn("zero tests", r["error"])

    def test_missing_result_payload_is_an_error(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(writes_payload=False)
        r = self._run()
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertIn("result payload", r["error"])

    def test_timeout_marks_timed_out_and_kills(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(exc=_FakeTimeout("t/o"))
        r = self._run(timeout=5.0)
        self.assertTrue(r["executed"])
        self.assertTrue(r["timed_out"])
        self.assertIn("exceeded", r["error"])
        self.assertEqual(_FakeSandboxBase.killed_count, 1)

    def test_create_failure_reports_sanitized_error(self) -> None:
        _FakeSandboxBase.create_exc = ConnectionError("cube gateway   unreachable\nby key s3cr3t")
        with patch.dict("os.environ", {"E2B_API_KEY": "s3cr3t"}):
            r = self._run()
        self.assertFalse(r["executed"])
        self.assertIn("unreachable", r["error"])
        self.assertNotIn("s3cr3t", r["error"])
        self.assertEqual(r["sandbox_cleanup"], "none")

    def test_kill_failure_is_reported_not_suppressed(self) -> None:
        _FakeSandboxBase.kill_exc = RuntimeError("kill rpc failed")
        r = self._run()
        self.assertEqual(r["sandbox_cleanup"], "kill_failed")
        self.assertIn("teardown", r["error"])
        self.assertEqual(_FakeSandboxBase.killed_count, 2)  # retry observed

    def test_transient_kill_failure_retries(self) -> None:
        _FakeSandboxBase.kill_outcomes = [RuntimeError("flake"), True]
        r = self._run()
        self.assertEqual(r["sandbox_cleanup"], "destroyed")
        self.assertEqual(_FakeSandboxBase.killed_count, 2)

    def test_kill_failure_preserves_suite_error(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(
            payload={"tests_run": 1, "failures": 1, "errors": 0,
                     "skipped": 0, "ok": False},
        )
        _FakeSandboxBase.kill_exc = RuntimeError("kill rpc failed")
        r = self._run()
        self.assertEqual(r["sandbox_cleanup"], "kill_failed")
        self.assertEqual(r["error"], "unittest exited 1")

    def test_kill_not_found_is_recorded(self) -> None:
        _FakeSandboxBase.kill_return = False
        r = self._run()
        self.assertEqual(r["sandbox_cleanup"], "not_found")

    def test_unsafe_artifact_path_never_creates_sandbox(self) -> None:
        r = self._run(files={"../escape.py": "x = 1\n"})
        self.assertFalse(r["executed"])
        self.assertIn("path", r["error"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_shadowing_member_rejected(self) -> None:
        r = self._run(files={"unittest/__main__.py": "raise SystemExit\n"})
        self.assertFalse(r["executed"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_stdlib_named_member_rejected(self) -> None:
        # a top-level json.py would shadow stdlib json for the verifier
        # runner's own imports once the workdir is on sys.path
        r = self._run(files={"json.py": "def dump(*a, **k): pass\n"})
        self.assertFalse(r["executed"])
        self.assertIn("shadow", r["error"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_stdlib_named_package_rejected(self) -> None:
        r = self._run(files={"json/__init__.py": "", "json/x.py": ""})
        self.assertFalse(r["executed"])
        self.assertIn("shadow", r["error"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_nested_stdlib_named_member_allowed(self) -> None:
        # pkg/json.py does not shadow stdlib json — only top-level names do
        r = self._run(files={"pkg/json.py": "X = 1\n"})
        self.assertTrue(r["executed"])

    def test_case_folding_member_rejected(self) -> None:
        # sanitize_path folds Main.java to main.java — on a case-sensitive
        # guest fs that's a silent rename; reject rather than misattribute
        r = self._run(files={"Main.java": "class Main {}\n"})
        self.assertFalse(r["executed"])
        self.assertIn("folds case", r["error"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_malformed_payload_reports_diagnostic_only(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(payload_raw="not json{")
        r = self._run()
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertIn("no result payload", r["error"])

    def test_deadline_exceeded_during_writes(self) -> None:
        t0 = 1000.0
        calls: list[int] = []

        def fake_monotonic() -> float:
            calls.append(1)
            return t0 if len(calls) == 1 else t0 + 100.0

        with patch.object(cubeexec.time, "monotonic", side_effect=fake_monotonic):
            r = self._run(timeout=30.0)
        self.assertFalse(r["executed"])
        self.assertIn("deadline", r["error"])
        self.assertEqual(_FakeSandboxBase.killed_count, 1)  # still torn down

    def test_transport_timeout_is_not_labeled_suite_timeout(self) -> None:
        # builtin TimeoutError during commands.run is a transport fault, not
        # the suite exceeding its budget — the SDK's own TimeoutException is
        # the timeout channel
        _FakeSandboxBase.next_commands = _FakeCommands(exc=TimeoutError("conn"))
        r = self._run()
        self.assertFalse(r["timed_out"])
        self.assertIn("sandbox runtime error", r["error"])

    def test_no_timeout_exception_falls_back_to_builtin(self) -> None:
        _FakeSandboxBase.next_commands = _FakeCommands(exc=TimeoutError("t/o"))
        r = self._run(install=_install_fake_e2b_no_timeout)
        self.assertTrue(r["executed"])
        self.assertTrue(r["timed_out"])
        self.assertIn("exceeded", r["error"])


class TestSdkCompatShapes(CubeExecBase):
    """Each SDK-surface branch in _load_sdk / _files_write / _kill_sandbox /
    the timeout cast gets a fake modeling that exact surface."""

    def test_v1_surface_full_suite_passes(self) -> None:
        r = self._run(install=_install_fake_e2b_v1)
        self.assertTrue(r["executed"])
        self.assertTrue(r["ok"])
        # constructor entry point (no create classmethod) captured kwargs
        self.assertEqual(len(_FakeSandboxBase.created), 1)
        self.assertIs(_FakeSandboxBase.created[0].get("allow_internet_access"), False)
        # positional files.write fallback was actually exercised
        self.assertTrue(_FakeFilesV1.positional_calls)
        for call in _FakeFilesV1.positional_calls:
            self.assertEqual(call[2], "user")
            self.assertIsInstance(call[3], float)
        # positional kill dispatch ran and cleanup recorded exactly
        self.assertEqual(_FakeSandboxBase.killed_count, 1)
        self.assertEqual(r["sandbox_cleanup"], "destroyed")

    def test_v1_without_exceptions_submodule(self) -> None:
        r = self._run(install=_install_fake_e2b_v1_no_exc_module)
        self.assertTrue(r["executed"])
        self.assertTrue(r["ok"])

    def test_create_signature_mismatch_uses_ctor_without_calling_create(self) -> None:
        r = self._run(install=_install_fake_e2b_create_mismatch)
        self.assertTrue(r["executed"])
        self.assertTrue(r["ok"])
        self.assertEqual(len(_FakeSandboxBase.created), 1)

    def test_incompatible_surface_is_not_reported_as_missing(self) -> None:
        r = self._run(install=_install_fake_e2b_incompatible)
        self.assertFalse(r["executed"])
        self.assertIn("incompatible", r["error"])
        self.assertNotIn("not installed", r["error"])

    def test_missing_sandbox_is_incompatible_not_absent(self) -> None:
        r = self._run(install=_install_fake_e2b_no_sandbox)
        self.assertFalse(r["executed"])
        self.assertIn("incompatible", r["error"])
        self.assertNotIn("not installed", r["error"])

    def test_command_timeout_is_floored_int(self) -> None:
        cmds = _FakeCommands(payload={"tests_run": 1, "ok": True})
        _FakeSandboxBase.next_commands = cmds
        self._run()
        timeout = cmds.calls[0]["timeout"]
        self.assertIsInstance(timeout, int)
        self.assertGreaterEqual(timeout, 1)

    def test_timeout_floor_holds_near_deadline(self) -> None:
        # remaining() in (0, 1) when commands.run is reached → int() would
        # truncate to 0 without the max(1, ...) floor.
        clock_calls: list[int] = []
        t0 = 1000.0

        def fake_monotonic() -> float:
            clock_calls.append(1)
            return t0 if len(clock_calls) == 1 else t0 + 59.5

        cmds = _FakeCommands(payload={"tests_run": 1, "ok": True})
        _FakeSandboxBase.next_commands = cmds
        with patch.object(cubeexec.time, "monotonic", side_effect=fake_monotonic):
            self._run(timeout=30.0)
        self.assertEqual(cmds.calls[0]["timeout"], 1)


class _RepoCommands:
    """Commands surface for the repo-fixture path: every call succeeds
    unless a substring in ``failures`` maps it to a nonzero exit. The repo
    verifier has no runner-payload contract — the verdict is the verify
    command's exit code alone."""

    def __init__(
        self,
        failures: dict[str, int] | None = None,
        listing: str = "5\n",
        stdout: str = "",
        stderr: str = "",
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self._failures = failures or {}
        self._listing = listing
        self._stdout = stdout
        self._stderr = stderr
        self._exc = exc

    def bind(self, written: dict[str, str]) -> None:
        pass

    def run(self, command: str, **kwargs) -> _FakeResult:
        self.calls.append(command)
        self.kwargs.append(kwargs)
        if self._exc is not None:
            raise self._exc
        code = 0
        for pat, rc in self._failures.items():
            if pat in command:
                code = rc
        stdout = self._listing if "wc -l" in command else self._stdout
        for chunk, cb in (
            (stdout, kwargs.get("on_stdout")),
            (self._stderr, kwargs.get("on_stderr")),
        ):
            if chunk and cb:
                cb(chunk)
        if code == 0:
            return _FakeResult(0, stdout=stdout, stderr=self._stderr)
        raise _FakeCommandExit(code, stdout=stdout, stderr=self._stderr)


class TestRepoSuite(CubeExecBase):
    def _repo(self, *, files=None, tarball=b"\x1f\x8bfake", install=_install_fake_e2b,
              commands=None, **kw):
        if commands is not None:
            _FakeSandboxBase.next_commands = commands
        args: dict = {
            "fixture_tarball": tarball,
            "fixture_id": "fx-1",
            "workdir": "repo",
            "setup_commands": ["pip install --no-index --find-links /home/user/wheelhouse pytest"],
            "verify_command": ["python", "-m", "pytest", "tests/test_x.py", "-q"],
            "fail_to_pass": ["test_a", "test_b"],
            "test_files": {"tests/test_x.py": "def test_a():\n    pass\n"},
            "timeout_seconds": 60.0,
        }
        args.update(kw)
        with patch.dict(sys.modules, install()):
            return cubeexec.run_repo_suite(files or {"pkg/mod.py": "X = 1\n"}, **args)

    def test_happy_path_full_lifecycle(self) -> None:
        r = self._repo(commands=_RepoCommands())
        self.assertTrue(r["executed"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["returncode"], 0)
        self.assertEqual(r["tests_run"], 2)  # len(fail_to_pass)
        self.assertEqual(r["staged_members"], 5)
        self.assertEqual(r["oracle_members"], 1)
        self.assertEqual(r["fixture_id"], "fx-1")
        self.assertEqual(_FakeSandboxBase.killed_count, 1)
        cmds = _FakeSandboxBase.instances[0].commands.calls
        self.assertEqual(cmds[-1], "python -m pytest tests/test_x.py -q")
        self.assertTrue(any("tar xzf" in c for c in cmds))

    def test_tarball_written_raw_not_base64(self) -> None:
        self._repo(commands=_RepoCommands())
        written = _FakeSandboxBase.instances[0].written
        self.assertEqual(written["/tmp/_orch_fixture.tgz"], b"\x1f\x8bfake")

    def test_worker_and_oracle_overlay_paths(self) -> None:
        self._repo(commands=_RepoCommands())
        written = _FakeSandboxBase.instances[0].written
        self.assertEqual(written["/home/user/repo/pkg/mod.py"], "X = 1\n")
        self.assertIn("/home/user/repo/tests/test_x.py", written)

    def test_oracle_wins_member_collision(self) -> None:
        # worker submits the same path the oracle owns — the oracle overlay
        # lands after the worker fileset, so the verifier's body wins.
        files = {"tests/test_x.py": "def test_a():\n    assert False\n"}
        self._repo(files=files, commands=_RepoCommands())
        written = _FakeSandboxBase.instances[0].written
        self.assertEqual(written["/home/user/repo/tests/test_x.py"],
                         "def test_a():\n    pass\n")

    def test_worker_cannot_shadow_verifier_toolchain(self) -> None:
        # a worker-written pytest.py, conftest.py, or pytest.ini would
        # let the artifact grade itself — all rejected before staging
        for bad in (
            {"pytest.py": "import sys; sys.exit(0)\n"},
            {"conftest.py": "collect_ignore = ['tests']\n"},
            {"pkg/conftest.py": "collect_ignore = ['tests']\n"},
            {"pytest.ini": "[pytest]\naddopts = --collect-only\n"},
            {"tox.ini": "[tox]\n"},
        ):
            with self.subTest(bad=bad):
                r = self._repo(files=bad, commands=_RepoCommands())
                self.assertFalse(r["executed"])
                self.assertIn("shadow", r["error"])
                self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_worker_cannot_overwrite_repo_tests(self) -> None:
        # the tarball owns tests/test_core.py; the oracle owns only
        # tests/test_x.py — a worker rewrite of the repo's own suite is a
        # grading bypass, not a patch
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("repo/tests/test_core.py", "repo/pkg/mod.py"):
                body = b"x = 1\n"
                ti = tarfile.TarInfo(name)
                ti.size = len(body)
                tf.addfile(ti, io.BytesIO(body))
        files = {"tests/test_core.py": "def test_core():\n    pass\n"}
        r = self._repo(files=files, tarball=buf.getvalue(),
                       commands=_RepoCommands())
        self.assertFalse(r["executed"])
        self.assertIn("shadow", r["error"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_oracle_owned_repo_test_is_worker_writable(self) -> None:
        # the same path the oracle owns is fine — the oracle overlay wins
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            body = b"x = 1\n"
            ti = tarfile.TarInfo("repo/tests/test_x.py")
            ti.size = len(body)
            tf.addfile(ti, io.BytesIO(body))
        files = {"tests/test_x.py": "def test_a():\n    assert False\n"}
        r = self._repo(files=files, tarball=buf.getvalue(),
                       commands=_RepoCommands())
        self.assertTrue(r["executed"])
        self.assertTrue(r["ok"])

    def test_workdir_other_than_repo_rejected(self) -> None:
        r = self._repo(workdir="pkg", commands=_RepoCommands())
        self.assertFalse(r["executed"])
        self.assertIn("workdir", r["error"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_oracle_path_case_preserved(self) -> None:
        r = self._repo(test_files={"tests/Test_Chunked.py": "def test_a():\n    pass\n"},
                       commands=_RepoCommands())
        self.assertTrue(r["executed"])
        written = _FakeSandboxBase.instances[0].written
        self.assertIn("/home/user/repo/tests/Test_Chunked.py", written)

    def test_setup_failure_fails_closed(self) -> None:
        r = self._repo(commands=_RepoCommands(failures={"pip install": 2}))
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertIn("setup command", r["error"])
        self.assertEqual(r["returncode"], 2)
        self.assertTrue(r["setup_failed"])

    def test_verify_nonzero_is_failure_not_error(self) -> None:
        # "-m pytest" matches only the verify command, not the setup
        # `pip install ... pytest`.
        r = self._repo(commands=_RepoCommands(failures={"-m pytest": 1}))
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["returncode"], 1)
        self.assertIsNone(r["error"])

    def test_verify_timeout_marks_timed_out(self) -> None:
        cmds = _RepoCommands()
        orig_run = cmds.run

        def run(command, **kwargs):
            if "-m pytest" in command:  # verify only — setup also says "pytest"
                raise _FakeTimeout()
            return orig_run(command, **kwargs)

        cmds.run = run
        r = self._repo(commands=cmds)
        self.assertTrue(r["executed"])
        self.assertTrue(r["timed_out"])
        self.assertFalse(r["ok"])

    def test_empty_verify_command_is_fail_closed(self) -> None:
        r = self._repo(commands=_RepoCommands(), verify_command=[])
        self.assertFalse(r["executed"])
        self.assertFalse(r["ok"])
        self.assertIn("verify.command", r["error"])

    def test_unsafe_oracle_member_rejected(self) -> None:
        r = self._repo(commands=_RepoCommands(),
                       test_files={"../escape.py": "x = 1\n"})
        self.assertFalse(r["executed"])
        self.assertIn("test_files", r["error"])

    def test_disabled_runtime_dispatch(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(codeexec.CODE_RUNTIME_ENV, None)
            r = codeexec.run_repo_suite({"a.py": "x = 1\n"}, fixture_id="fx-1")
        self.assertFalse(r["executed"])
        self.assertEqual(r["runtime"], "disabled")
        self.assertEqual(r["fixture_id"], "fx-1")

    def test_missing_tarball_fails_before_sandbox(self) -> None:
        missing = Path("/nonexistent/fx-1.tar.gz")
        with patch.dict("os.environ", {codeexec.CODE_RUNTIME_ENV: "isolated"}), \
             patch("orchestral.fixtures.tarball_path", return_value=missing):
            r = codeexec.run_repo_suite({"a.py": "x = 1\n"}, fixture_id="fx-1")
        self.assertFalse(r["executed"])
        self.assertIn("not fetched", r["error"])
        self.assertEqual(len(_FakeSandboxBase.created), 0)

    def test_isolated_dispatch_passes_test_files(self) -> None:
        with patch.dict("os.environ", {codeexec.CODE_RUNTIME_ENV: "isolated"}), \
             patch("orchestral.fixtures.verified_fixture_bytes",
                   return_value=b"tar-bytes"), \
             patch.object(cubeexec, "run_repo_suite",
                          return_value={"runtime": "e2b", "ok": True}) as spy:
            codeexec.run_repo_suite(
                {"a.py": "x = 1\n"},
                fixture_id="fx-1",
                test_files={"tests/t.py": "pass\n"},
            )
        self.assertEqual(spy.call_args.kwargs["test_files"],
                         {"tests/t.py": "pass\n"})


if __name__ == "__main__":
    unittest.main()
