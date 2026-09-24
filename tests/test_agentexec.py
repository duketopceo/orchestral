"""Executor-core tests — a stub CLI on PATH drives every path, no real agent.

The stub (`agent-stub`) is a Python script whose first argv word names a
behavior: write a file, sleep, fork a grandchild, flood stdout, leak env,
make symlinks/binaries, etc. run_attempt never invokes a real coding CLI.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestral import agentexec
from orchestral.agentexec import (
    ADAPTERS,
    AgentAdapter,
    ExecutorCancelled,
    ExecutorExitError,
    ExecutorNoOutputError,
    ExecutorPreflightError,
    ExecutorTimeoutError,
    WorkspaceError,
)
from orchestral.patch import _parse_diff

STUB = """#!/usr/bin/env python3
import os, sys, time, signal

behavior = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
if os.path.exists(behavior):  # prompt_via="file" passes a prompt path
    behavior = open(behavior).read().strip()

if behavior == "write":
    open("out.py", "w").write("# generated\\nprint('hi')\\n")
    print('{"type":"text","text":"done"}')
elif behavior == "modify":
    open("seed.py", "w").write("x = 2\\n")
elif behavior == "delete":
    if os.path.exists("delme.py"):
        os.remove("delme.py")
elif behavior == "casefile":
    open("Main.java", "w").write("class Main {}\\n")
elif behavior == "dotfile":
    open(".env", "w").write("SECRET=1\\n")
elif behavior == "githook":
    os.makedirs(".git/hooks", exist_ok=True)
    hook = ".git/hooks/post-diff"
    open(hook, "w").write("#!/bin/sh\\ntouch pwned\\n")
    os.chmod(hook, 0o755)
    open("real.py", "w").write("x = 1\\n")
elif behavior == "excludes":
    os.makedirs(".git/hooks", exist_ok=True)
    open(".git/config", "w").write("x")
    os.makedirs("node_modules", exist_ok=True)
    open("node_modules/x.js", "w").write("x")
    os.makedirs("__pycache__", exist_ok=True)
    open("__pycache__/y.pyc", "w").write("x")
    open("real.py", "w").write("x = 1\\n")
elif behavior == "sleep":
    time.sleep(120)
elif behavior == "fork":
    pidfile = sys.argv[2] if len(sys.argv) > 2 else None
    pid = os.fork()
    if pid == 0:
        if pidfile:
            open(pidfile, "w").write(str(os.getpid()))
        time.sleep(120)
    else:
        time.sleep(120)
elif behavior == "setsid":
    pidfile = sys.argv[2] if len(sys.argv) > 2 else None
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if pidfile:
            open(pidfile, "w").write(str(os.getpid()))
        time.sleep(120)
    else:
        time.sleep(120)
elif behavior == "forkexit":
    # daemon grandchild outlives the leader inside the group
    if os.fork() == 0:
        open("grandchild.pid", "w").write(str(os.getpid()))
        time.sleep(120)
    open("out.py", "w").write("x = 1\\n")
elif behavior == "exit1":
    print("failing")
    sys.exit(1)
elif behavior == "noop":
    print("did nothing")
elif behavior == "flood":
    sys.stdout.write("x" * 20_000_000)
elif behavior == "echoenv":
    open("leak.txt", "w").write(os.environ.get("ORCHESTRAL_AGENT_API_KEY", "unset"))
    open("real.py", "w").write("x = 1\\n")
elif behavior == "envdump":
    open("env.txt", "w").write(
        "\\n".join(f"{k}={v}" for k, v in sorted(os.environ.items())))
    open("real.py", "w").write("x = 1\\n")
elif behavior == "symlink":
    os.symlink("/etc/passwd", "link")
elif behavior == "fifo":
    os.mkfifo("pipe")
elif behavior == "binary":
    open("bin.dat", "wb").write(b"\\x00\\x01\\x02")
elif behavior == "manyfiles":
    for i in range(60):
        open(f"f{i}.txt", "w").write("x")
elif behavior == "usage":
    open("out.py", "w").write("x = 1\\n")
    print('{"tokens": 1234}')
else:
    print("unknown behavior " + behavior)
    sys.exit(2)
"""


class StubAdapter(AgentAdapter):
    def run_argv(self, binary_path: str, prompt: str) -> list[str]:
        argv = [binary_path]
        if prompt:
            argv.append(prompt)
        return argv


def _adapter(**kw) -> AgentAdapter:
    base = {
        "name": "stub",
        "binary": "agent-stub",
        "env_keys": ("ORCHESTRAL_AGENT_API_KEY",),
        "config_env": {"STUB_CONFIG_PINNED": "1"},
        "prompt_via": "argv",
    }
    base.update(kw)
    return StubAdapter(**base)


class AgentexecTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        stub = self.bindir / "agent-stub"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.env_patch = patch.dict(
            os.environ,
            {
                "PATH": f"{self.bindir}:{os.environ.get('PATH', '')}",
                "ORCHESTRAL_AGENT_API_KEY": "sk-agent-testkey-123456",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)


class TestHappyPath(AgentexecTestBase):
    def test_write_file_harvests_parsable_diff(self):
        evidence = Path(self.tmp.name) / "evidence"
        result = agentexec.run_attempt(
            _adapter(), prompt="write",
            files={"seed.py": "x = 1\n"},
            evidence_dir=evidence,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("out.py", result.changed_paths)
        parsed = _parse_diff(result.diff)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["new"], "out.py")
        self.assertIn("print('hi')", result.diff)
        # evidence copied; external workspace removed
        self.assertTrue((evidence / "transcript.log").exists())
        self.assertTrue((evidence / "artifact.diff").exists())
        self.assertFalse(result.workspace.exists())
        # transcript hash covers the redacted transcript
        sha = hashlib.sha256(
            (evidence / "transcript.log").read_bytes()).hexdigest()
        self.assertEqual(result.transcript_sha256, sha)

    def test_seed_modification_diffs_against_snapshot(self):
        result = agentexec.run_attempt(
            _adapter(), prompt="modify", files={"seed.py": "x = 1\n"})
        self.assertIn("seed.py", result.changed_paths)
        self.assertIn("-x = 1", result.diff)
        self.assertIn("+x = 2", result.diff)

    def test_case_preserved_in_diff(self):
        result = agentexec.run_attempt(_adapter(), prompt="casefile")
        self.assertIn("b/Main.java", result.diff)
        self.assertIn("Main.java", result.changed_paths)

    def test_deletion_emits_dev_null_diff(self):
        result = agentexec.run_attempt(
            _adapter(), prompt="delete", files={"delme.py": "x = 1\n"})
        self.assertIn("delme.py", result.deleted_paths)
        self.assertIn("/dev/null", result.diff)

    def test_usage_parsed_by_adapter(self):
        result = agentexec.run_attempt(_adapter(), prompt="usage")
        self.assertEqual(result.usage, {"tokens": 1234})

    def test_stdin_prompt_via(self):
        adapter = _adapter(prompt_via="stdin")
        result = agentexec.run_attempt(adapter, prompt="write")
        self.assertIn("out.py", result.changed_paths)

    def test_file_prompt_via(self):
        adapter = _adapter(prompt_via="file")
        result = agentexec.run_attempt(adapter, prompt="write")
        self.assertIn("out.py", result.changed_paths)

    def test_git_hooks_never_executed_by_harvest(self):
        """A .git/hooks script that touches `pwned` never runs — the
        harvester is pure Python and walks no git machinery."""
        result = agentexec.run_attempt(_adapter(), prompt="githook")
        self.assertIn("real.py", result.changed_paths)
        self.assertNotIn("pwned", result.changed_paths)
        self.assertNotIn("pwned", result.diff)

    def test_hidden_paths_surface_for_milestone(self):
        result = agentexec.run_attempt(
            _adapter(), prompt="dotfile", allow_hidden=True)
        self.assertEqual(result.hidden_paths, [".env"])

    def test_daemon_grandchild_killed_and_flagged(self):
        result = agentexec.run_attempt(
            _adapter(), prompt="forkexit", keep_workspace=True)
        try:
            self.assertTrue(result.group_survivors)
            self.assertIn("out.py", result.changed_paths)
            grandchild = int(
                (result.workspace / "grandchild.pid").read_text().strip())
            with self.assertRaises((ProcessLookupError, PermissionError)):
                os.kill(grandchild, 0)
        finally:
            shutil.rmtree(result.workspace, ignore_errors=True)

    def test_reseed_gives_pristine_workspace(self):
        first = agentexec.run_attempt(_adapter(), prompt="write")
        second = agentexec.run_attempt(_adapter(), prompt="modify",
                                       files={"seed.py": "x = 1\n"})
        self.assertNotIn("out.py", second.changed_paths)  # attempt 1's file gone
        self.assertNotEqual(first.workspace, second.workspace)


class TestLifecycle(AgentexecTestBase):
    def test_timeout_kills_group(self):
        evidence = Path(self.tmp.name) / "evidence"
        with self.assertRaises(ExecutorTimeoutError):
            agentexec.run_attempt(
                _adapter(), prompt="sleep", timeout=0.5, evidence_dir=evidence)
        self.assertTrue((evidence / "transcript.log").exists())

    def test_kill_group_reaps_grandchildren(self):
        pidfile = Path(self.tmp.name) / "grandchild.pid"
        stub = str(self.bindir / "agent-stub")
        proc = subprocess.Popen([stub, "fork", str(pidfile)],
                                start_new_session=True)
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pidfile.exists(), "grandchild never spawned")
        survivors = agentexec._kill_group(proc, proc.pid)
        proc.wait(timeout=5)
        self.assertFalse(survivors)
        grandchild = int(pidfile.read_text().strip())
        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.kill(grandchild, 0)

    def test_setsid_escape_is_a_documented_limit(self):
        """A setsid() descendant leaves the group — the kill path cannot
        reach it. The test proves the limit is real, then cleans up."""
        pidfile = Path(self.tmp.name) / "escapee.pid"
        stub = str(self.bindir / "agent-stub")
        proc = subprocess.Popen([stub, "setsid", str(pidfile)],
                                start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pidfile.exists())
            agentexec._kill_group(proc, proc.pid)
            proc.wait(timeout=5)
            escapee = int(pidfile.read_text().strip())
            # group dead, escapee alive — the documented containment limit
            self.assertFalse(agentexec._group_alive(proc.pid))
            os.kill(escapee, 0)  # still running = escape confirmed
        finally:
            if pidfile.exists():
                with contextlib_suppress():
                    os.kill(int(pidfile.read_text().strip()), 9)

    def test_pgid_race_dead_group_is_safe(self):
        """Signalling a group that already exited suppresses the lookup
        error and never risks a recycled PGID."""
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait()
        self.assertFalse(agentexec._kill_group(proc, proc.pid))

    def test_cancel_mid_run_kills_and_raises(self):
        event = threading.Event()
        timer = threading.Timer(0.3, event.set)
        timer.start()
        try:
            with self.assertRaises(ExecutorCancelled):
                agentexec.run_attempt(
                    _adapter(), prompt="sleep", timeout=60, cancel_event=event)
        finally:
            timer.cancel()

    def test_exit_nonzero_is_executor_exit(self):
        with self.assertRaises(ExecutorExitError):
            agentexec.run_attempt(_adapter(), prompt="exit1")

    def test_no_output_is_explicit_failure(self):
        with self.assertRaises(ExecutorNoOutputError):
            agentexec.run_attempt(_adapter(), prompt="noop")


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(Exception)


class TestContainment(AgentexecTestBase):
    def test_env_is_minimal_and_scratched(self):
        """The child sees only the declared env: scratch HOME/XDG/TMPDIR
        under _orchestral/, pinned config, the dedicated key — and never
        the harness's own OPENROUTER_API_KEY."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-harness-secret"}):
            result = agentexec.run_attempt(_adapter(), prompt="envdump")
        env_dump = ""
        # env.txt lands in the diff as a new file
        for line in result.diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                env_dump += line[1:] + "\n"
        self.assertIn("HOME=", env_dump)
        self.assertIn("_orchestral/home", env_dump)
        self.assertNotIn("/home/lukekimball", env_dump)
        self.assertIn("STUB_CONFIG_PINNED=1", env_dump)
        self.assertIn("[REDACTED_ENV_ORCHESTRAL_AGENT_API_KEY]", env_dump)
        self.assertNotIn("sk-agent-testkey-123456", env_dump)
        self.assertNotIn("sk-or-harness-secret", env_dump)
        self.assertNotIn("OPENROUTER_API_KEY=", env_dump)

    def test_declared_env_value_redacted_from_diff(self):
        result = agentexec.run_attempt(_adapter(), prompt="echoenv")
        self.assertIn("[REDACTED_ENV_ORCHESTRAL_AGENT_API_KEY]", result.diff)
        self.assertNotIn("sk-agent-testkey-123456", result.diff)
        self.assertGreaterEqual(result.redactions, 1)

    def test_preflight_probe_is_contained(self):
        """The version probe executes an arbitrary CLI — it must run in a
        scratch dir, not the caller's cwd. A version_argv that writes a
        file proves the containment: `out.py` must not land here."""
        adapter = _adapter(version_argv=("write",))
        try:
            agentexec.preflight(adapter)
            self.assertFalse(
                Path("out.py").exists(),
                "preflight probe ran the CLI in the caller's cwd",
            )
        finally:
            Path("out.py").unlink(missing_ok=True)

    def test_harvest_excludes(self):
        result = agentexec.run_attempt(_adapter(), prompt="excludes")
        self.assertNotIn(".git", result.diff)
        self.assertNotIn("node_modules", result.diff)
        self.assertNotIn("__pycache__", result.diff)
        self.assertIn("real.py", result.changed_paths)

    def test_symlink_fails_closed(self):
        with self.assertRaises(WorkspaceError):
            agentexec.run_attempt(_adapter(), prompt="symlink")

    def test_fifo_fails_closed(self):
        with self.assertRaises(WorkspaceError):
            agentexec.run_attempt(_adapter(), prompt="fifo")

    def test_binary_fails_closed(self):
        with self.assertRaises(WorkspaceError):
            agentexec.run_attempt(_adapter(), prompt="binary")

    def test_harvest_file_cap(self):
        with self.assertRaises(WorkspaceError):
            agentexec.run_attempt(_adapter(), prompt="manyfiles")

    def test_transcript_cap_kills_and_marks(self):
        evidence = Path(self.tmp.name) / "evidence"
        with self.assertRaises(WorkspaceError):
            agentexec.run_attempt(
                _adapter(), prompt="flood",
                transcript_cap=1024, evidence_dir=evidence)
        transcript = (evidence / "transcript.log").read_text()
        self.assertIn("transcript truncated", transcript)

    def test_dotfile_rejected_without_allow_hidden(self):
        with self.assertRaises(WorkspaceError):
            agentexec.run_attempt(_adapter(), prompt="dotfile")

    def test_dotfile_allowed_with_flag(self):
        result = agentexec.run_attempt(
            _adapter(), prompt="dotfile", allow_hidden=True)
        self.assertIn(".env", result.changed_paths)

    def test_seed_traversal_rejected(self):
        for bad in ("../escape.py", "/etc/abs.py", "a/../../b.py"):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                agentexec.seed_workspace({bad: "x"}, Path(self.tmp.name) / "ws")

    def test_missing_binary_is_preflight(self):
        adapter = _adapter(binary="definitely-not-a-real-binary-xyz")
        with self.assertRaises(ExecutorPreflightError) as ctx:
            agentexec.preflight(adapter)
        self.assertIn("definitely-not-a-real-binary-xyz", str(ctx.exception))

    def test_missing_env_key_is_preflight(self):
        adapter = _adapter(env_keys=("DEFINITELY_UNSET_VAR_XYZ",))
        with self.assertRaises(ExecutorPreflightError) as ctx:
            agentexec.preflight(adapter)
        self.assertIn("DEFINITELY_UNSET_VAR_XYZ", str(ctx.exception))

    def test_preflight_version_probe(self):
        version = agentexec.preflight(_adapter())
        self.assertIsInstance(version, str)

    def test_adapter_registry_shape(self):
        adapter = ADAPTERS["opencode"]
        self.assertIn("ORCHESTRAL_AGENT_API_KEY", adapter.env_keys)
        self.assertIn("OPENCODE_DISABLE_PROJECT_CONFIG", adapter.config_env)


if __name__ == "__main__":
    unittest.main()
