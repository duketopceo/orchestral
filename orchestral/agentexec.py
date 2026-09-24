"""External coding-agent CLI executor — containment, not a security sandbox.

Runs a coding-agent CLI (e.g. opencode) as a worker: seeds a workspace
OUTSIDE the repository, spawns the CLI under a minimal environment, streams
its output to a transcript, harvests the workspace diff in pure Python, and
terminates the whole process group on timeout, cancel, or transcript-cap
breach.

Honesty notes:

- The child runs with the user's OS privileges and can read the open
  filesystem — including task oracles and this repo — if it looks. What is
  contained is the *environment*: scratch HOME/XDG_*/TMPDIR, a dedicated
  ORCHESTRAL_AGENT_API_KEY, pinned CLI config discovery, and no harness
  secrets. Absolute-path oracle reads are detected downstream (transcript
  scan), not prevented.
- Workspaces live outside the repo tree on purpose: a workspace under
  `runs/` would give the agent read reach of `tasks/*.yaml` oracles,
  `orchestral/` source, sibling-run evidence, and ancestor CLI config.
- Diff harvest never invokes `git` inside the workspace. Agent-controlled
  `.git/hooks`, `.git/config` (core.fsmonitor, core.pager, diff.external)
  and `.gitattributes` textconv drivers would execute in the *runner's*
  process with the full env — strictly less contained than the agent.
- A descendant that calls `setsid()` escapes the process group and cannot
  be signalled or detected by pgid — a documented containment limit.
- `raw/` evidence is agent-writable in principle: the transcript hash in
  the manifest makes later modification detectable, not impossible.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from orchestral.config import ModelConfig, TaskSpec
from orchestral.privacy import scrub_text

# ---------------------------------------------------------------------------
# Failure types — class names are the taxonomy contract (see taxonomy.py)
# ---------------------------------------------------------------------------


class ExecutorPreflightError(Exception):
    """Binary missing, version probe failed, or declared env key absent."""


class ExecutorExitError(Exception):
    """The CLI exited non-zero."""


class ExecutorTimeoutError(Exception):
    """The attempt exceeded its deadline and the group was killed."""


class ExecutorNoOutputError(Exception):
    """The CLI exited 0 but produced no workspace diff."""


class SpawnFailedError(Exception):
    """Popen itself failed (binary vanished after preflight, fd exhaustion)."""


class WorkspaceError(Exception):
    """Seed traversal, unsafe filesystem object, or evidence-cap breach."""


class ExecutorCancelled(Exception):
    """cancel_event fired mid-attempt — runner maps this to RunCancelled."""


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentAdapter:
    """How to run one coding-agent CLI under containment.

    `env_keys` is the allowlist of parent env vars injected into the child —
    the dedicated executor credential (`ORCHESTRAL_AGENT_API_KEY`), never the
    harness's OPENROUTER_API_KEY. `config_env` pins the CLI's config surface
    (e.g. disabling project-config auto-load, which walks cwd -> fs root).
    `prompt_via` is "stdin" (prompt written to the child's stdin — argv is
    visible in `ps` and lands in error echoes), "file" (prompt written to
    `_orchestral_prompt.md` in the workspace, path passed to run_argv), or
    "argv" (prompt appended to the run command, documented exposure).
    `install_hint` is documentation for manual setup only — adapters are
    never installed host-side (KTD3).
    """

    name: str
    binary: str
    env_keys: tuple[str, ...] = ()
    config_env: dict[str, str] = field(default_factory=dict)
    prompt_via: str = "argv"
    version_argv: tuple[str, ...] = ("--version",)
    install_hint: str = ""
    # the CLI reports no usage and no flat estimate applies (e.g. a local
    # free tool) — calls land as pricing_source="unmetered", cost $0, and
    # the pairing gets no cost_per_pass advantage downstream
    unmetered: bool = False

    def run_argv(self, binary_path: str, prompt: str) -> list[str]:
        """Argv for one attempt. `prompt` is empty when prompt_via is stdin."""
        raise NotImplementedError

    def parse_usage(self, transcript: str) -> dict[str, Any] | None:
        """Extract cost/token usage from the transcript, or None if unmetered.

        The base implementation sums JSONL `tokens`/`total_tokens` fields —
        the shape opencode's `--format json` stream emits. Adapters with a
        different transcript format override this."""
        tokens = 0
        for m in re.finditer(r'"(?:tokens|total_tokens)":\s*(\d+)', transcript):
            tokens += int(m.group(1))
        return {"tokens": tokens} if tokens else None


class _OpencodeAdapter(AgentAdapter):
    def run_argv(self, binary_path: str, prompt: str) -> list[str]:
        argv = [binary_path, "run", "--format", "json"]
        if self.prompt_via == "argv":
            argv.append(prompt)
        return argv


ADAPTERS: dict[str, AgentAdapter] = {
    "opencode": _OpencodeAdapter(
        name="opencode",
        binary="opencode",
        env_keys=("ORCHESTRAL_AGENT_API_KEY",),
        config_env={
            # pin config discovery off — otherwise the CLI walks cwd -> fs
            # root and loads every opencode.json it finds (including ours)
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        },
        prompt_via="argv",
        install_hint="npm i -g opencode-ai  (see https://opencode.ai)",
    ),
}

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 900.0          # 15 min — agentic work is slow
KILL_GRACE_SECONDS = 5.0
TRANSCRIPT_CAP_BYTES = 8 * 1024 * 1024   # every untrusted byte flow is capped
MAX_SEED_BYTES = 4 * 1024 * 1024
MAX_HARVEST_FILES = 50
MAX_PATH_LENGTH = 512
PROMPT_FILENAME = "_orchestral_prompt.md"

# harness-owned subtree inside the workspace — scratch HOME/XDG_*/TMPDIR and
# the transcript live here so fixture paths can never collide with them
SCRATCH_DIR = "_orchestral"

# never harvested — agent-made or harness-made, these are not artifact
HARVEST_EXCLUDES = frozenset({
    ".git", "node_modules", "__pycache__", ".hg", ".svn",
    SCRATCH_DIR, PROMPT_FILENAME,
})

_ILLEGAL_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"{base}{i}" for base in ("com", "lpt") for i in range(1, 10)),
}


# ---------------------------------------------------------------------------
# Path validation — case-preserving variant of fileset.sanitize_path
# ---------------------------------------------------------------------------


def check_artifact_path(path: str, *, allow_hidden: bool = False) -> str:
    """Validate one workspace-relative path; return it unchanged.

    Same traversal rules as `fileset.sanitize_path` (percent-decode to a
    fixed point, no absolute/drive/../illegal segments) but preserves case —
    `Main.java` must keep its name — and dotfile segments are allowed only
    when the task declares `metadata.allow_hidden`.
    """
    raw = str(path).strip()
    normalized = raw
    for _ in range(3):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    normalized = normalized.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or len(normalized) > MAX_PATH_LENGTH:
        raise WorkspaceError(f"Path {raw!r} is empty or over {MAX_PATH_LENGTH} chars")
    if normalized.startswith("/"):
        raise WorkspaceError(f"Path {raw!r} is absolute")
    if re.match(r"^[A-Za-z]:", normalized):
        raise WorkspaceError(f"Path {raw!r} has a drive letter")
    segments = [s for s in normalized.split("/") if s]
    if not segments:
        raise WorkspaceError(f"Path {raw!r} is empty")
    for segment in segments:
        if segment in (".", ".."):
            raise WorkspaceError(f"Path {raw!r} contains a {segment!r} segment")
        if _ILLEGAL_CHARS.search(segment):
            raise WorkspaceError(f"Path {raw!r} contains an illegal character")
        if not segment.isascii():
            raise WorkspaceError(f"Path {raw!r} contains a non-ASCII character")
        if not segment.strip("."):
            raise WorkspaceError(f"Path {raw!r} is a dot-only segment")
        if segment.endswith((" ", ".")):
            raise WorkspaceError(f"Path {raw!r} has a segment ending with a dot or space")
        if segment.startswith(".") and not allow_hidden:
            raise WorkspaceError(f"Path {raw!r} is hidden (declare metadata.allow_hidden)")
        if segment.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise WorkspaceError(f"Path {raw!r} uses the reserved name {segment!r}")
    return normalized


# ---------------------------------------------------------------------------
# Preflight + environment
# ---------------------------------------------------------------------------


def check_ready(adapter: AgentAdapter) -> None:
    """Binary + env-key check without spawning the CLI — the launch-time
    half of preflight. Web registries and the TUI use it for fail-fast
    errors on the request path; the full preflight adds a version probe."""
    binary = shutil.which(adapter.binary)
    if binary is None:
        raise ExecutorPreflightError(
            f"{adapter.name} CLI binary {adapter.binary!r} not found on PATH"
        )
    missing = [k for k in adapter.env_keys if not os.environ.get(k)]
    if missing:
        raise ExecutorPreflightError(
            f"{adapter.name} adapter needs env var(s) not set: {', '.join(missing)}"
        )


def preflight(adapter: AgentAdapter) -> str:
    """Verify the CLI is runnable; return its version-probe output.

    Raises ExecutorPreflightError naming the missing binary or env key —
    a failure category that never retries (the environment won't fix itself
    between attempts).
    """
    check_ready(adapter)
    binary = shutil.which(adapter.binary) or adapter.binary
    try:
        # the probe is still an arbitrary binary with user privileges — run
        # it in a scratch dir so a side-effecting CLI can't touch the repo
        with tempfile.TemporaryDirectory(prefix="orchestral-probe-") as probe_dir:
            probe = subprocess.run(
                [binary, *adapter.version_argv],
                capture_output=True, text=True, timeout=15,
                cwd=probe_dir,
                env={"PATH": os.environ.get("PATH", os.defpath), **adapter.config_env},
            )
        version = (probe.stdout or probe.stderr or "").strip().splitlines()
        return version[0] if version else "unknown"
    except Exception:
        # a version probe failure is not fatal — the binary exists and env is
        # set; the spawn will surface any real breakage
        return "unknown"


def launch_gate(
    worker: ModelConfig,
    task: TaskSpec | None = None,
    *,
    allow_agent_exec: bool,
    probe: bool = True,
) -> AgentAdapter | None:
    """The launch-surface executor check shared by CLI, web, and TUI.

    Mirrors the runner's dispatch conjunction — an executor worker requires
    a task declaring ``metadata.requires_executor`` and the launch-context
    opt-in (a server-start/CLI flag, never a per-request field); a
    ``requires_executor`` task requires an executor worker. ``probe`` adds
    the binary/env readiness check — launch surfaces use it for fail-fast
    errors while the runner skips it (its own preflight captures the CLI
    version for the manifest). Returns the adapter for executor workers,
    ``None`` for ordinary chat workers. Every half-state raises
    ExecutorPreflightError naming the blocker.
    """
    name = (worker.metadata or {}).get("executor")
    requires = bool(task is not None and (task.metadata or {}).get("requires_executor"))
    if not name:
        if requires:
            assert task is not None  # requires implies it
            raise ExecutorPreflightError(
                f"task {task.id} declares metadata.requires_executor but "
                f"worker {worker.slug} is not an executor worker"
            )
        return None
    if task is not None and not requires:
        raise ExecutorPreflightError(
            f"worker {worker.slug} is executor-routed ({name}) but task "
            f"{task.id} lacks metadata.requires_executor"
        )
    if not allow_agent_exec:
        raise ExecutorPreflightError(
            "executor dispatch requires the launch opt-in "
            "(--allow-agent-exec / ORCHESTRAL_ALLOW_AGENT_EXEC; for the "
            "observatory, restart with `serve --allow-agent-exec` — it is "
            "never a per-request field)"
        )
    adapter = ADAPTERS.get(str(name))
    if adapter is None:
        raise ExecutorPreflightError(
            f"worker {worker.slug} declares unknown executor adapter {name!r} "
            f"(registered: {', '.join(sorted(ADAPTERS))})"
        )
    if probe:
        check_ready(adapter)
    return adapter


def _minimal_env(adapter: AgentAdapter, workspace: Path) -> dict[str, str]:
    """The KTD13 spawn env — scratch dirs under `_orchestral/` so the CLI's
    file-based config/auth discovery finds nothing of the user's."""
    scratch = workspace / SCRATCH_DIR
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(scratch / "home"),
        "XDG_CONFIG_HOME": str(scratch / "config"),
        "XDG_DATA_HOME": str(scratch / "data"),
        "TMPDIR": str(scratch / "tmp"),
        "TERM": "dumb",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        **adapter.config_env,
    }
    for key in adapter.env_keys:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


# ---------------------------------------------------------------------------
# Workspace seeding
# ---------------------------------------------------------------------------


def seed_workspace(
    files: dict[str, str],
    workspace: Path,
    *,
    allow_hidden: bool = False,
) -> dict[str, bytes]:
    """Write the task fixture into the workspace; return the path->bytes
    snapshot the harvester diffs against.

    Every key passes the case-preserving traversal check before any write —
    `metadata.files`/`metadata.fs` seed paths can otherwise escape via
    absolute or `..` segments.
    """
    snapshot: dict[str, bytes] = {}
    total = 0
    for rel, body in files.items():
        check_artifact_path(rel, allow_hidden=allow_hidden)
        total += len(body.encode("utf-8"))
        if total > MAX_SEED_BYTES:
            raise WorkspaceError(f"Fixture exceeds {MAX_SEED_BYTES} bytes")
    for rel, body in files.items():
        rel = check_artifact_path(rel, allow_hidden=allow_hidden)
        data = body.encode("utf-8")
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        snapshot[rel] = data
    return snapshot


def _new_workspace(work_dir: Path | None = None) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="orchestral-agent-", dir=work_dir))
    scratch = ws / SCRATCH_DIR
    for name in ("home", "config", "data", "tmp"):
        (scratch / name).mkdir(parents=True)
    return ws


# ---------------------------------------------------------------------------
# Spawn + stream + process-group lifecycle
# ---------------------------------------------------------------------------


@dataclass
class _WaitResult:
    exit_code: int | None
    outcome: str          # "exited" | "timeout" | "cancelled" | "cap_breach"
    group_survivors: bool


def _stream_reader(proc: subprocess.Popen, chunks: queue.Queue[bytes | None]) -> None:
    """Daemon thread: drain stdout+stderr into a queue so the wait loop can
    enforce the transcript cap and poll cancel/deadline without blocking."""
    try:
        stream = proc.stdout
        if stream is not None:
            while True:
                data = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not data:
                    break
                chunks.put(data)
    except Exception:
        pass
    finally:
        chunks.put(None)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_group(proc: subprocess.Popen, pgid: int, grace: float = KILL_GRACE_SECONDS) -> bool:
    """SIGTERM -> grace -> SIGKILL on the whole group, then reap the leader.

    Returns True if group members still lived after the final grace — a
    milestone-worthy survivor. Two probes can lie and are handled on
    purpose: an unreaped zombie leader still holds group membership, so the
    leader is waited out before the final check; a `setsid()` escapee is
    invisible to the pgid probe by design (documented limit, not hidden).
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return False
        except PermissionError:
            return False
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return False
            time.sleep(0.05)
    with contextlib.suppress(Exception):
        proc.wait(timeout=grace)
    return _group_alive(pgid)


def _wait_or_kill(
    proc: subprocess.Popen,
    pgid: int,
    transcript_path: Path,
    chunks: queue.Queue[bytes | None],
    *,
    timeout: float,
    cancel_event: threading.Event | None,
    cap: int,
) -> _WaitResult:
    """Drive the attempt: stream to transcript, enforce deadline/cancel/cap.

    The pgid was captured at spawn (it equals proc.pid) and is only ever
    signalled while the group is confirmed live — after the leader reaps,
    a recycled PGID could belong to an unrelated process.
    """
    deadline = time.monotonic() + timeout
    written = 0
    reader_done = False
    outcome = "exited"

    with transcript_path.open("ab") as fh:
        while True:
            # drain queued output
            try:
                while True:
                    data = chunks.get(timeout=0.1)
                    if data is None:
                        reader_done = True
                    else:
                        if written + len(data) > cap:
                            outcome = "cap_breach"
                            break
                        fh.write(data)
                        fh.flush()
                        written += len(data)
            except queue.Empty:
                pass

            if outcome != "exited":
                break
            if cancel_event is not None and cancel_event.is_set():
                outcome = "cancelled"
                break
            if time.monotonic() > deadline:
                outcome = "timeout"
                break
            if proc.poll() is not None:
                break  # leader out — do NOT wait on EOF (see below)

        # kill the group before draining: a daemonized descendant holds the
        # stdout pipe open, so waiting on EOF before kill would stall until
        # the attempt deadline. Once the group is dead the pipe closes.
        stragglers = _group_alive(pgid)
        if outcome == "exited":
            survivors = stragglers  # daemon-detection milestone
            if stragglers:
                _kill_group(proc, pgid)
        else:
            survivors = _kill_group(proc, pgid)

        # drain whatever the reader buffered — bounded, so a reader that
        # never finishes can't hang the attempt
        drain_deadline = time.monotonic() + KILL_GRACE_SECONDS
        while not reader_done and time.monotonic() < drain_deadline:
            try:
                data = chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            if data is None:
                reader_done = True
            elif written + len(data) <= cap:
                fh.write(data)
                fh.flush()
                written += len(data)
            else:
                outcome = "cap_breach"

        if outcome == "cap_breach":
            fh.write(b"\n[orchestral: transcript truncated - byte cap reached]\n")

    code = proc.poll()
    if code is None:
        with contextlib.suppress(Exception):
            proc.wait(timeout=KILL_GRACE_SECONDS)
        code = proc.poll()
    return _WaitResult(code, outcome, group_survivors=survivors)


# ---------------------------------------------------------------------------
# Diff harvest — pure Python, never git
# ---------------------------------------------------------------------------


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def harvest_diff(
    snapshot: dict[str, bytes],
    workspace: Path,
    *,
    allow_hidden: bool = False,
) -> tuple[str, list[str], list[str], dict[str, str]]:
    """Diff the workspace against the seed snapshot without running git.

    Returns (diff_text, changed_paths, deleted_paths, files) — `files` is
    the full post-attempt workspace as {path: text} so a fileset-shaped
    artifact needs no second walk. Every filesystem object is lstat'd —
    symlinks, FIFOs, and other non-regular entries are explicit
    WorkspaceErrors, never followed or silently dropped. Binary or
    undecodable content fails the same way (the artifact contract is text).
    """
    excludes = HARVEST_EXCLUDES
    current: dict[str, bytes] = {}
    walked = 0
    for root, dirs, names in os.walk(workspace):
        root_p = Path(root)
        rel_root = root_p.relative_to(workspace)
        # prune excluded dirs in place so os.walk never descends
        dirs[:] = [
            d for d in dirs
            if str((rel_root / d).as_posix()) not in excludes
            and d not in excludes
        ]
        for name in names:
            walked += 1
            if walked > MAX_HARVEST_FILES:
                raise WorkspaceError(
                    f"Workspace exceeds {MAX_HARVEST_FILES} files — refusing harvest"
                )
            path = root_p / name
            rel = (rel_root / name).as_posix() if str(rel_root) != "." else name
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError(f"Non-regular file in workspace: {rel}")
            rel = check_artifact_path(rel, allow_hidden=allow_hidden)
            if rel in excludes or rel.split("/")[0] in excludes:
                continue
            current[rel] = path.read_bytes()

    hunks: list[str] = []
    changed: list[str] = []
    deleted: list[str] = []

    for rel in sorted(set(snapshot) | set(current)):
        old = snapshot.get(rel)
        new = current.get(rel)
        if old is not None and new is not None and old == new:
            continue
        changed.append(rel)
        if new is None:
            deleted.append(rel)
            old_text = (old or b"").decode("utf-8", errors="strict")
            old_lines = old_text.splitlines()
            hunks.append(
                "\n".join(
                    difflib.unified_diff(
                        old_lines, [],
                        fromfile=f"a/{rel}", tofile="/dev/null",
                        lineterm="",
                    )
                )
            )
            continue
        if _is_binary(new):
            raise WorkspaceError(f"Binary file in workspace: {rel}")
        try:
            new_text = new.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError(f"Undecodable (non-UTF-8) file in workspace: {rel}") from None
        old_text = old.decode("utf-8") if old is not None else ""
        if _is_binary(old or b""):
            raise WorkspaceError(f"Binary seed file: {rel}")
        hunks.append(
            "\n".join(
                difflib.unified_diff(
                    old_text.splitlines(), new_text.splitlines(),
                    fromfile=f"a/{rel}", tofile=f"b/{rel}",
                    lineterm="",
                )
            )
        )
    diff_text = "\n".join(hunks) + ("\n" if hunks else "")
    files = {rel: data.decode("utf-8") for rel, data in current.items()}
    return diff_text, changed, deleted, files


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_secrets(text: str, env_values: dict[str, str]) -> tuple[str, int]:
    """Replace declared env values with [REDACTED_ENV_<name>] markers.

    Anything visible to the child can be echoed into the diff or transcript;
    redaction happens at capture time, before bytes leave the module.
    """
    count = 0
    for name, value in env_values.items():
        if not value or len(value) < 6:
            continue
        hits = text.count(value)
        if hits:
            text = text.replace(value, f"[REDACTED_ENV_{name}]")
            count += hits
    return text, count


# ---------------------------------------------------------------------------
# Attempt orchestration
# ---------------------------------------------------------------------------


@dataclass
class ExecutorResult:
    """One agent attempt, distilled to artifact + evidence + milestones."""

    diff: str
    changed_paths: list[str]
    deleted_paths: list[str]
    hidden_paths: list[str]   # dotfile segments in the diff — milestone input
    files: dict[str, str]     # post-attempt workspace — fileset-shaped artifact
    exit_code: int | None
    usage: dict[str, Any] | None
    transcript_path: Path | None
    transcript_sha256: str
    transcript_bytes: int
    redactions: int
    group_survivors: bool
    workspace: Path


def run_attempt(
    adapter: AgentAdapter,
    *,
    prompt: str,
    files: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
    work_dir: Path | None = None,
    transcript_cap: int = TRANSCRIPT_CAP_BYTES,
    allow_hidden: bool = False,
    evidence_dir: Path | None = None,
    keep_workspace: bool = False,
) -> ExecutorResult:
    """Run one agent attempt end-to-end.

    Seeds a fresh external workspace, spawns the CLI under the minimal env,
    streams output to a capped transcript, kills the process group on
    timeout/cancel/cap-breach, harvests the diff in pure Python, redacts
    declared env values at capture time, and copies evidence under
    `evidence_dir` (the run's raw/ dir) when given.
    """
    ws = _new_workspace(work_dir)
    try:
        snapshot = seed_workspace(files or {}, ws, allow_hidden=allow_hidden)

        declared = {k: os.environ.get(k, "") for k in adapter.env_keys}
        env = _minimal_env(adapter, ws)

        prompt_text = prompt
        binary_path = shutil.which(adapter.binary) or adapter.binary
        if adapter.prompt_via == "stdin":
            argv = adapter.run_argv(binary_path, "")
        elif adapter.prompt_via == "file":
            (ws / PROMPT_FILENAME).write_text(prompt_text, encoding="utf-8")
            argv = adapter.run_argv(binary_path, PROMPT_FILENAME)
        else:
            argv = adapter.run_argv(binary_path, prompt_text)

        transcript_path = ws / SCRATCH_DIR / "transcript.log"
        try:
            proc = subprocess.Popen(
                argv,
                cwd=ws,
                env=env,
                stdin=subprocess.PIPE if adapter.prompt_via == "stdin" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group; pgid == proc.pid
            )
        except OSError as exc:
            raise SpawnFailedError(f"{adapter.name} spawn failed: {exc}") from exc

        pgid = proc.pid  # captured at spawn — never re-derived post-reap
        if adapter.prompt_via == "stdin" and proc.stdin is not None:
            # daemon feed: a prompt larger than the pipe buffer would
            # deadlock a synchronous write if the child stalls reading
            def _feed(stream: Any, data: bytes) -> None:
                try:
                    stream.write(data)
                    stream.close()
                except Exception:
                    pass

            threading.Thread(
                target=_feed,
                args=(proc.stdin, prompt_text.encode("utf-8")),
                daemon=True,
            ).start()

        chunks: queue.Queue[bytes | None] = queue.Queue()
        reader = threading.Thread(target=_stream_reader, args=(proc, chunks), daemon=True)
        reader.start()

        wait = _wait_or_kill(
            proc, pgid, transcript_path, chunks,
            timeout=timeout, cancel_event=cancel_event, cap=transcript_cap,
        )
        with contextlib.suppress(Exception):
            if proc.stdout is not None:
                proc.stdout.close()

        # transcript: redact declared env values before bytes leave the module
        raw_transcript = transcript_path.read_bytes() if transcript_path.exists() else b""
        transcript_text, transcript_redactions = redact_secrets(
            raw_transcript.decode("utf-8", errors="replace"), declared)
        transcript_sha = hashlib.sha256(transcript_text.encode()).hexdigest()

        def _copy_evidence(diff_text: str | None = None) -> None:
            """A failed attempt's transcript is still evidence — copy it
            before any error raise. Only a successful harvest adds the diff."""
            if evidence_dir is None:
                return
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "transcript.log").write_text(transcript_text, encoding="utf-8")
            if diff_text is not None:
                (evidence_dir / "artifact.diff").write_text(diff_text, encoding="utf-8")

        if wait.outcome == "cancelled":
            _copy_evidence()
            raise ExecutorCancelled("cancelled during agent execution")
        if wait.outcome == "timeout":
            _copy_evidence()
            raise ExecutorTimeoutError(
                f"{adapter.name} exceeded {timeout}s; process group killed"
            )
        if wait.outcome == "cap_breach":
            _copy_evidence()
            raise WorkspaceError(
                f"{adapter.name} transcript exceeded {transcript_cap} bytes; process group killed"
            )

        exit_code = wait.exit_code
        if exit_code not in (0, None):
            _copy_evidence()
            raise ExecutorExitError(
                f"{adapter.name} exited {exit_code} (transcript sha256:{transcript_sha[:12]})"
            )

        try:
            diff, changed, deleted, files = harvest_diff(
                snapshot, ws, allow_hidden=allow_hidden)
        except WorkspaceError:
            _copy_evidence()
            raise
        diff, diff_redactions = redact_secrets(diff, declared)
        diff = scrub_text(diff)
        file_redactions = 0
        redacted_files: dict[str, str] = {}
        for rel, body in files.items():
            body, n = redact_secrets(body, declared)
            file_redactions += n
            redacted_files[rel] = scrub_text(body)
        files = redacted_files

        if not changed:
            _copy_evidence()
            raise ExecutorNoOutputError(
                f"{adapter.name} exited 0 but produced no workspace changes"
            )

        _copy_evidence(diff)
        return ExecutorResult(
            diff=diff,
            changed_paths=changed,
            deleted_paths=deleted,
            hidden_paths=[
                p for p in changed
                if any(seg.startswith(".") for seg in p.split("/"))
            ],
            files=files,
            exit_code=exit_code,
            usage=adapter.parse_usage(transcript_text),
            transcript_path=(
                (evidence_dir / "transcript.log") if evidence_dir
                else (transcript_path if keep_workspace else None)
            ),
            transcript_sha256=transcript_sha,
            transcript_bytes=len(raw_transcript),
            redactions=transcript_redactions + diff_redactions + file_redactions,
            group_survivors=wait.group_survivors,
            workspace=ws,
        )
    finally:
        if not keep_workspace:
            shutil.rmtree(ws, ignore_errors=True)
