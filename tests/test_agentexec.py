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

def _swap(path, make):
    # build the replacement beside the target, then rename over it. rename is
    # atomic and replaces whatever is already there, so the swap cannot fail
    # because the harness created the transcript after we looked for it.
    tmp = path + ".swap"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    make(tmp)
    os.rename(tmp, path)

def _victim():
    # the child runs under a minimal env, so the host path cannot arrive in
    # an env var — it comes in as a seeded fixture, like any other task input
    return open("target_path.txt").read().strip()

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
elif behavior == "envprint":
    # the declared env value on *stdout*, so it reaches the transcript
    open("real.py", "w").write("x = 1\\n")
    print("key=" + os.environ.get("ORCHESTRAL_AGENT_API_KEY", "unset"))
elif behavior == "scrubme":
    # emits a line scrub_text must rewrite. A fixture with nothing scrubbable
    # would make the scrub assertion pass with the control removed.
    open("out.py", "w").write("x = 1\\n")
    print("ping me at agent@example.com about /Users/someone/private/notes.md")
elif behavior == "steal-transcript":
    # point the harness-owned transcript at a host file. Swapping the path
    # mid-run is the TOCTOU the fd-anchored capture has to survive.
    secret = open("host_secret.txt").read()
    _swap("_orchestral/transcript.log", lambda t: os.symlink(_victim(), t))
    open("out.py", "w").write("x = 1\\n")
    print("wrote out.py; secret was " + secret[:3])
elif behavior == "steal-transcript-mkfile":
    # the other shape: leave a decoy regular file, then make the link
    _swap("_orchestral/transcript.log", lambda t: open(t, "w").write("decoy"))
    _swap("_orchestral/transcript.log", lambda t: os.symlink(_victim(), t))
    open("out.py", "w").write("x = 1\\n")
    print("done")
elif behavior == "fifo-transcript":
    # replace the transcript with a FIFO nothing ever writes to. A path-based
    # read blocks here forever, hanging the attempt past its own deadline.
    _swap("_orchestral/transcript.log", os.mkfifo)
    open("out.py", "w").write("x = 1\\n")
    print("replaced transcript with a fifo")
    time.sleep(0.5)
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


def _open_fd_count() -> int:
    """Descriptors this process holds open — proves a refused open does not
    leak one per attempt. `/proc/self/fd` on Linux, `/dev/fd` on macOS."""
    for root in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(root):
            return len(os.listdir(root))
    raise unittest.SkipTest("no fd enumeration available on this platform")


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

    def test_transcript_read_cap_leaves_room_for_the_truncation_marker(self):
        # Writes stop at `transcript_cap` and the marker is appended *after*
        # that, so reading back with the bare cap cuts the marker off the end
        # whenever the last accepted chunk lands close to the cap. The
        # flood test above does not catch that: 20MB trips the cap on the
        # first chunk, so `written` is 0 and the marker lands in an otherwise
        # empty file. This asserts the cap itself leaves the room.
        seen: list[int] = []
        real = agentexec._read_transcript_fd

        def _capture(fd, *, cap, timeout):
            seen.append(cap)
            return real(fd, cap=cap, timeout=timeout)

        with (
            patch.object(agentexec, "_read_transcript_fd", _capture),
            self.assertRaises(WorkspaceError),
        ):
            agentexec.run_attempt(
                _adapter(), prompt="flood", transcript_cap=1024)
        self.assertEqual(len(seen), 1, "expected exactly one transcript read")
        self.assertGreaterEqual(
            seen[0], 1024 + len(agentexec.TRANSCRIPT_TRUNCATION_MARKER),
            "read cap must accommodate the marker appended past the byte cap")

    def test_marker_survives_a_read_capped_exactly_at_the_file(self):
        # the shape the fix protects: a transcript sitting at cap, plus the
        # marker, read with cap + len(marker)
        target = Path(self.tmp.name) / "capped.log"
        cap = 1024
        target.write_bytes(b"x" * cap + agentexec.TRANSCRIPT_TRUNCATION_MARKER)
        fd = agentexec._open_transcript_fd(target)
        self.addCleanup(os.close, fd)
        data, complete = agentexec._read_transcript_fd(
            fd, cap=cap + len(agentexec.TRANSCRIPT_TRUNCATION_MARKER), timeout=5.0)
        self.assertTrue(complete)
        self.assertTrue(data.endswith(agentexec.TRANSCRIPT_TRUNCATION_MARKER))

    def test_transcript_is_scrubbed_not_just_redacted(self):
        # the diff and the fileset both pass scrub_text; the transcript used
        # to be the one artifact published with only env redaction applied.
        # The fixture emits a real email and a real mac path, so removing the
        # scrub call turns this red instead of leaving it vacuous.
        evidence = Path(self.tmp.name) / "evidence"
        result = agentexec.run_attempt(
            _adapter(), prompt="scrubme", evidence_dir=evidence)
        for label, blob in (("result", result.transcript_text),
                            ("evidence", (evidence / "transcript.log").read_text())):
            with self.subTest(where=label):
                self.assertNotIn("agent@example.com", blob)
                self.assertNotIn("/Users/someone/private/notes.md", blob)
                self.assertIn("[REDACTED_email]", blob)
                self.assertIn("[REDACTED_mac_path]", blob)

    def test_declared_env_redaction_still_runs_before_scrub(self):
        # The two stages are ordered, not exclusive. The stub's key would match
        # scrub_text's api_key pattern, so seeing the *env* marker and not the
        # *api_key* one proves redact ran and got there first.
        evidence = Path(self.tmp.name) / "evidence"
        result = agentexec.run_attempt(
            _adapter(), prompt="envprint", evidence_dir=evidence)
        self.assertIn("[REDACTED_ENV_ORCHESTRAL_AGENT_API_KEY]", result.transcript_text)
        self.assertNotIn("[REDACTED_api_key]", result.transcript_text)
        self.assertNotIn("sk-agent-testkey-123456", result.transcript_text)
        self.assertNotIn(
            "sk-agent-testkey-123456", (evidence / "transcript.log").read_text())

    def test_transcript_hash_covers_the_published_text(self):
        evidence = Path(self.tmp.name) / "evidence"
        result = agentexec.run_attempt(
            _adapter(), prompt="usage", evidence_dir=evidence)
        published = (evidence / "transcript.log").read_text()
        self.assertEqual(
            result.transcript_sha256, hashlib.sha256(published.encode()).hexdigest())


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


class TestTranscriptPathHostileInput(AgentexecTestBase):
    """The transcript path lives inside the agent-writable workspace, so the
    agent can unlink it and leave a symlink or a FIFO behind. The harness must
    neither publish the target's bytes as its own evidence nor block on it."""

    def setUp(self):
        super().setUp()
        self.secret_path = Path(self.tmp.name) / "host-secret.txt"
        self.secret_body = "HOST_SECRET_CANARY_9f2b7c1e\n"
        self.secret_path.write_text(self.secret_body)
        # the child runs under a minimal env, so the host path reaches it as
        # a seeded fixture rather than an env var
        self.fixtures = {
            "target_path.txt": f"{self.secret_path}\n",
            "host_secret.txt": self.secret_body,
        }

    def _assert_no_host_bytes(self, evidence: Path) -> None:
        for path in evidence.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            self.assertNotIn(
                self.secret_body, path.read_text(errors="replace"),
                f"host secret leaked into {path}")

    def test_symlink_swap_does_not_exfiltrate_into_evidence(self):
        evidence = Path(self.tmp.name) / "evidence"
        result = agentexec.run_attempt(
            _adapter(), prompt="steal-transcript", files=self.fixtures,
            evidence_dir=evidence, keep_workspace=True)
        self._assert_no_host_bytes(evidence)
        # the captured transcript is the real stream, not the symlink target
        self.assertIn("wrote out.py", result.transcript_text)
        self.assertNotIn("HOST_SECRET_CANARY", result.transcript_text)

    def test_symlink_swap_via_regular_file_first_does_not_exfiltrate(self):
        evidence = Path(self.tmp.name) / "evidence"
        result = agentexec.run_attempt(
            _adapter(), prompt="steal-transcript-mkfile", files=self.fixtures,
            evidence_dir=evidence, keep_workspace=True)
        self._assert_no_host_bytes(evidence)
        self.assertNotIn("HOST_SECRET_CANARY", result.transcript_text)
        self.assertNotEqual(result.transcript_text, "decoy")

    def test_fifo_swap_does_not_hang_the_attempt(self):
        # a path-based read of the FIFO blocks forever; the fd-anchored read
        # never resolves the path again, so the attempt finishes on its own
        evidence = Path(self.tmp.name) / "evidence"
        started = time.monotonic()
        result = agentexec.run_attempt(
            _adapter(), prompt="fifo-transcript", files=self.fixtures,
            evidence_dir=evidence, timeout=60, keep_workspace=True)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 45, "run_attempt hung on the FIFO transcript")
        self._assert_no_host_bytes(evidence)
        self.assertIn("replaced transcript with a fifo", result.transcript_text)

    def test_pre_planted_symlink_is_refused_at_open(self):
        ws = Path(self.tmp.name) / "ws"
        scratch = ws / agentexec.SCRATCH_DIR
        scratch.mkdir(parents=True)
        target = scratch / "transcript.log"
        target.symlink_to(self.secret_path)
        with self.assertRaises(WorkspaceError) as ctx:
            agentexec._open_transcript_fd(target)
        self.assertNotIn("CANARY", str(ctx.exception))
        self.assertEqual(self.secret_path.read_text(), self.secret_body)

    def test_pre_planted_fifo_is_refused_at_open(self):
        # O_NONBLOCK is what makes this return at all. Linux opens a FIFO
        # O_RDWR|O_NONBLOCK and hands back a descriptor that fstat then
        # rejects; macOS returns ENXIO from open itself. Both are the same
        # refusal, so the assertion is the refusal, not the errno.
        scratch = Path(self.tmp.name) / "ws" / agentexec.SCRATCH_DIR
        scratch.mkdir(parents=True)
        target = scratch / "transcript.log"
        os.mkfifo(target)
        started = time.monotonic()
        with self.assertRaises(WorkspaceError) as ctx:
            agentexec._open_transcript_fd(target)
        self.assertLess(time.monotonic() - started, 5.0, "open blocked on the FIFO")
        self.assertRegex(
            str(ctx.exception),
            r"not a regular file|Refusing to open transcript")

    def test_non_regular_file_is_refused_at_open(self):
        # a character device opens fine and is only caught by the fstat check
        if not os.path.exists("/dev/null"):
            self.skipTest("no character device available")
        with self.assertRaises(WorkspaceError) as ctx:
            agentexec._open_transcript_fd(Path("/dev/null"))
        self.assertIn("not a regular file", str(ctx.exception))

    def test_refused_open_releases_the_descriptor(self):
        # a leaked descriptor on every attempt would exhaust the process
        scratch = Path(self.tmp.name) / "ws" / agentexec.SCRATCH_DIR
        scratch.mkdir(parents=True)
        target = scratch / "transcript.log"
        os.mkfifo(target)
        before = _open_fd_count()
        for _ in range(64):
            with self.assertRaises(WorkspaceError):
                agentexec._open_transcript_fd(target)
        self.assertLessEqual(_open_fd_count() - before, 8)

    def test_read_is_capped_and_bounded(self):
        target = Path(self.tmp.name) / "capped.log"
        target.write_bytes(b"x" * 5000)
        fd = agentexec._open_transcript_fd(target)
        self.addCleanup(os.close, fd)
        data, complete = agentexec._read_transcript_fd(
            fd, cap=1000, timeout=5.0)
        self.assertFalse(complete, "an over-cap read must not report complete")
        self.assertEqual(len(data), 1000)

    def test_read_times_out_instead_of_blocking(self):
        target = Path(self.tmp.name) / "slow.log"
        target.write_bytes(b"y" * 4096)
        fd = agentexec._open_transcript_fd(target)
        self.addCleanup(os.close, fd)
        started = time.monotonic()
        # a zero budget must return promptly rather than start reading
        _, complete = agentexec._read_transcript_fd(fd, cap=10 ** 6, timeout=0.0)
        self.assertFalse(complete)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_incomplete_read_does_not_pass_as_a_clean_attempt(self):
        # the transcript feeds the oracle tripwire, so a partial one must not
        # reach the success path where it would read as "checked, clean"
        with (
            patch.object(agentexec, "_read_transcript_fd",
                         return_value=(b"partial", False)),
            self.assertRaises(WorkspaceError) as ctx,
        ):
            agentexec.run_attempt(_adapter(), prompt="write")
        self.assertIn("did not complete", str(ctx.exception))

    def test_cap_breach_keeps_its_own_diagnostic(self):
        # the outcome-specific message must win over the read backstop
        evidence = Path(self.tmp.name) / "evidence"
        with self.assertRaises(WorkspaceError) as ctx:
            agentexec.run_attempt(
                _adapter(), prompt="flood",
                transcript_cap=1024, evidence_dir=evidence)
        self.assertIn("exceeded 1024 bytes", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
