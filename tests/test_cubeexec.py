"""Adapter contract tests for the E2B-compatible isolated runtime.

The SDK is an optional `[e2b]` extra, so the suite injects a fake `e2b`
module; what is under test is the adapter's contract: report shape, path
sanitization before sandbox write, environment isolation, timeout mapping,
the out-of-band result payload, and kill-on-every-exit teardown.
"""

from __future__ import annotations

import json
import re
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
            runner = self._written.get("/home/user/_orch_runner.py", "")
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


class FakeSandbox:
    """Records the SDK surface the adapter uses."""

    created: ClassVar[list[dict]] = []
    instances: ClassVar[list[FakeSandbox]] = []
    killed_count: ClassVar[int] = 0
    next_commands: ClassVar[_FakeCommands | None] = None
    create_exc: ClassVar[Exception | None] = None
    kill_exc: ClassVar[Exception | None] = None
    kill_return: ClassVar[bool] = True

    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.files = _FakeFiles(self.written)
        commands = FakeSandbox.next_commands or _FakeCommands(
            stderr="\nRan 3 tests in 0.01s\n\nOK\n"
        )
        commands.bind(self.written)
        self.commands = commands
        FakeSandbox.instances.append(self)

    @classmethod
    def create(cls, **kwargs):
        cls.created.append(kwargs)
        if cls.create_exc is not None:
            raise cls.create_exc
        return cls()

    def kill(self, **_kwargs) -> bool:
        if FakeSandbox.kill_exc is not None:
            raise FakeSandbox.kill_exc
        FakeSandbox.killed_count += 1
        return FakeSandbox.kill_return


def _install_fake_e2b() -> dict[str, types.ModuleType]:
    """Fake the SDK surface: Sandbox, TimeoutException, CommandExitException."""
    mod = types.ModuleType("e2b")
    mod.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    exc_mod = types.ModuleType("e2b.exceptions")
    exc_mod.TimeoutException = _FakeTimeout  # type: ignore[attr-defined]
    exc_mod.CommandExitException = _FakeCommandExit  # type: ignore[attr-defined]
    return {"e2b": mod, "e2b.exceptions": exc_mod}


class CubeExecBase(unittest.TestCase):
    def setUp(self) -> None:
        FakeSandbox.created = []
        FakeSandbox.instances = []
        FakeSandbox.killed_count = 0
        FakeSandbox.next_commands = None
        FakeSandbox.create_exc = None
        FakeSandbox.kill_exc = None
        FakeSandbox.kill_return = True

    def _run(self, files=None, tests="import unittest\n", timeout=30.0):
        files = files if files is not None else {"solution.py": "def f():\n    return 1\n"}
        with patch.dict(sys.modules, _install_fake_e2b()):
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
        self.assertEqual(r["sandbox_cleanup"], "destroyed")
        self.assertEqual(FakeSandbox.killed_count, 1)

    def test_files_and_tests_written_under_workdir(self) -> None:
        self._run(files={"pkg/mod.py": "X = 1\n"})
        self.assertIs(FakeSandbox.created[0].get("allow_internet_access"), False)
        written = FakeSandbox.instances[0].written
        self.assertEqual(written["/home/user/pkg/mod.py"], "X = 1\n")
        self.assertIn("/home/user/task_tests.py", written)
        self.assertIn("/home/user/_orch_runner.py", written)

    def test_guest_env_is_allowlist_only(self) -> None:
        cmds = _FakeCommands(
            payload={"tests_run": 1, "ok": True}, stderr="Ran 1 tests\nOK\n"
        )
        FakeSandbox.next_commands = cmds
        self._run()
        envs = cmds.calls[0]["envs"]
        self.assertIn("PATH", envs)
        self.assertNotIn("OPENROUTER_API_KEY", envs)
        self.assertNotIn("E2B_API_KEY", envs)

    def test_verdict_comes_from_result_payload_not_stdout(self) -> None:
        # stdout claims a huge pass; the verifier payload says 1 ran, failed.
        FakeSandbox.next_commands = _FakeCommands(
            payload={"tests_run": 1, "failures": 1, "errors": 0, "skipped": 0, "ok": False},
            stderr="Ran 99 tests in 0.01s\n\nOK\n",
        )
        r = self._run()
        self.assertEqual(r["tests_run"], 1)
        self.assertFalse(r["ok"])

    def test_output_tail_is_bounded_by_stream_callbacks(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(
            payload={"tests_run": 1, "ok": True},
            stderr="x" * (cubeexec._STREAM_TAIL_BYTES + 5000) + "\nOK\n",
        )
        r = self._run()
        self.assertLessEqual(len(r["output_tail"]), 4000)


class TestFailurePaths(CubeExecBase):
    def test_failing_suite_reports_exit_code(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(
            payload={"tests_run": 4, "failures": 1, "errors": 0, "skipped": 0, "ok": False},
            stderr="Ran 4 tests\nFAILED (failures=1)",
        )
        r = self._run()
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["tests_run"], 4)
        self.assertEqual(r["returncode"], 1)
        self.assertIn("unittest exited 1", r["error"])
        self.assertEqual(FakeSandbox.killed_count, 1)

    def test_zero_tests_is_a_finding(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(
            payload={"tests_run": 0, "failures": 0, "errors": 0, "skipped": 0, "ok": True},
            stderr="NO TESTS RAN",
        )
        r = self._run()
        self.assertFalse(r["ok"])
        self.assertIn("zero tests", r["error"])

    def test_missing_result_payload_is_an_error(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(writes_payload=False)
        r = self._run()
        self.assertTrue(r["executed"])
        self.assertFalse(r["ok"])
        self.assertIn("result payload", r["error"])

    def test_timeout_marks_timed_out_and_kills(self) -> None:
        FakeSandbox.next_commands = _FakeCommands(exc=_FakeTimeout("t/o"))
        r = self._run(timeout=5.0)
        self.assertTrue(r["executed"])
        self.assertTrue(r["timed_out"])
        self.assertIn("exceeded", r["error"])
        self.assertEqual(FakeSandbox.killed_count, 1)

    def test_create_failure_reports_sanitized_error(self) -> None:
        FakeSandbox.create_exc = ConnectionError("cube gateway   unreachable\nby key s3cr3t")
        with patch.dict("os.environ", {"E2B_API_KEY": "s3cr3t"}):
            r = self._run()
        self.assertFalse(r["executed"])
        self.assertIn("unreachable", r["error"])
        self.assertNotIn("s3cr3t", r["error"])

    def test_kill_failure_is_reported_not_suppressed(self) -> None:
        FakeSandbox.kill_exc = RuntimeError("kill rpc failed")
        r = self._run()
        self.assertEqual(r["sandbox_cleanup"], "kill_failed")
        self.assertIn("teardown", r["error"])

    def test_kill_not_found_is_recorded(self) -> None:
        FakeSandbox.kill_return = False
        r = self._run()
        self.assertEqual(r["sandbox_cleanup"], "not_found")

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
