"""Adapter contract tests for the E2B-compatible isolated runtime.

The SDK is an optional `[e2b]` extra, so the suite injects a fake `e2b`
module; what is under test is the adapter's contract: report shape, path
sanitization before sandbox write, environment isolation, timeout mapping,
and kill-on-every-exit teardown.
"""

from __future__ import annotations

import sys
import types
import unittest
from typing import ClassVar
from unittest.mock import patch

from orchestral import codeexec, cubeexec


class _FakeResult:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeFiles:
    def __init__(self, written: dict[str, str]) -> None:
        self._written = written

    def write(self, path: str, body: str) -> None:
        self._written[path] = body


class _FakeCommands:
    def __init__(self, result: _FakeResult | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result
        self._exc = exc

    def run(self, command: str, **kwargs) -> _FakeResult:
        self.calls.append({"command": command, **kwargs})
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


class FakeSandbox:
    """Records the SDK surface the adapter uses."""

    created: ClassVar[list[dict]] = []
    instances: ClassVar[list[FakeSandbox]] = []
    killed_count: ClassVar[int] = 0
    next_commands: ClassVar[_FakeCommands | None] = None
    create_exc: ClassVar[Exception | None] = None

    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.files = _FakeFiles(self.written)
        self.commands = FakeSandbox.next_commands or _FakeCommands(
            _FakeResult(0, stderr="\nRan 3 tests in 0.01s\n\nOK\n")
        )
        FakeSandbox.instances.append(self)

    @classmethod
    def create(cls, **kwargs):
        cls.created.append(kwargs)
        if cls.create_exc is not None:
            raise cls.create_exc
        return cls()

    def kill(self) -> None:
        FakeSandbox.killed_count += 1


class _FakeTimeout(Exception):
    pass


def _install_fake_e2b() -> dict[str, types.ModuleType]:
    """Fake the SDK surface: `e2b.Sandbox` plus `e2b.exceptions.TimeoutException`."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    exc_mod = types.ModuleType("e2b.exceptions")
    exc_mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": exc_mod}


class CubeExecBase(unittest.TestCase):
    def setUp(self) -> None:
        FakeSandbox.created = []
        FakeSandbox.instances = []
        FakeSandbox.killed_count = 0
        FakeSandbox.next_commands = None
        FakeSandbox.create_exc = None

    def _run(self, files=None, tests="import unittest\n", timeout=30.0):
        files = files if files is not None else {"solution.py": "def f():\n    return 1\n"}
        with patch.dict(sys.modules, _install_fake_e2b()):
            return cubeexec.run_unittest_suite(files, tests, timeout_seconds=timeout)


class TestMissingSdk(CubeExecBase):
    def test_missing_sdk_reports_cleanly(self) -> None:
        with patch.object(cubeexec, "_load_sandbox_class", return_value=None):
            r = cubeexec.run_unittest_suite({"a.py": "x = 1\n"}, "import unittest\n")
        self.assertFalse(r["executed"])
        self.assertEqual(r["runtime"], "e2b")
        self.assertIn("e2b", r["error"])

    def test_default_runtime_stays_disabled(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
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
        self.assertEqual(FakeSandbox.killed_count, 1)

    def test_files_and_tests_written_under_workdir(self) -> None:
        self._run(files={"pkg/mod.py": "X = 1\n"})
        self.assertIs(FakeSandbox.created[0].get("allow_internet_access"), False)
        written = FakeSandbox.instances[0].written
        self.assertEqual(written["/home/user/pkg/mod.py"], "X = 1\n")
        self.assertIn("/home/user/task_tests.py", written)

    def test_guest_env_is_allowlist_only(self) -> None:
        cmds = _FakeCommands(_FakeResult(0, stderr="Ran 1 tests\nOK\n"))
        FakeSandbox.next_commands = cmds
        self._run()
        envs = cmds.calls[0]["envs"]
        self.assertIn("PATH", envs)
        self.assertNotIn("OPENROUTER_API_KEY", envs)
        self.assertNotIn("E2B_API_KEY", envs)


class TestFailurePaths(CubeExecBase):
    def test_failing_suite_reports_exit_code(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(
            _FakeResult(1, stderr="Ran 4 tests\nFAILED (failures=1)")
        )
        r = self._run()
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["tests_run"], 4)
        self.assertIn("unittest exited 1", r["error"])
        self.assertEqual(FakeSandbox.killed_count, 1)

    def test_zero_tests_is_a_finding(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(
            _FakeResult(5, stderr="NO TESTS RAN")
        )
        r = self._run()
        self.assertFalse(r["ok"])
        self.assertIn("zero tests", r["error"])

    def test_timeout_marks_timed_out_and_kills(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(exc=_FakeTimeout("t/o"))
        r = self._run(timeout=5.0)
        self.assertTrue(r["executed"])
        self.assertTrue(r["timed_out"])
        self.assertIn("exceeded", r["error"])
        self.assertEqual(FakeSandbox.killed_count, 1)

    def test_create_failure_reports_without_crashing(self) -> None:
        FakeSandbox.create_exc = ConnectionError("cube gateway unreachable")
        r = self._run()
        self.assertFalse(r["executed"])
        self.assertIn("unreachable", r["error"])

    def test_unsafe_artifact_path_never_creates_sandbox(self) -> None:
        r = self._run(files={"../escape.py": "x = 1\n"})
        self.assertFalse(r["executed"])
        self.assertIn("path", r["error"])
        self.assertEqual(len(FakeSandbox.created), 0)

    def test_shadowing_member_rejected(self) -> None:
        r = self._run(files={"unittest/__main__.py": "raise SystemExit\n"})
        self.assertFalse(r["executed"])
        self.assertEqual(len(FakeSandbox.created), 0)


if __name__ == "__main__":
    unittest.main()
