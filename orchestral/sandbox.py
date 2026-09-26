"""Disposable execution backends for generated verifier code.

The local backend is retained for trusted tests and backwards compatibility.
The Docker backend is the safer default for live code tasks: it copies only
the submitted files and the verifier into a fresh container, disables the
network, applies resource limits, and removes the container in every exit
path. It intentionally uses the Docker CLI rather than adding an SDK
dependency.

Docker is still a container boundary, not a VM boundary. A future remote
backend can implement the same command/result contract with Firecracker,
Kata, Harbor, or another microVM provider.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_DOCKER_IMAGE = "python:3.11-slim"
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = "1"
DEFAULT_PIDS_LIMIT = 128
_CONTROL_TIMEOUT_SECONDS = 30
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_WORKSPACE_BYTES = 32 * 1024 * 1024


class SandboxError(RuntimeError):
    """A sandbox could not be created or its command could not be controlled."""


class SandboxUnavailable(SandboxError):
    """The requested sandbox backend is not available on this host."""


@dataclass(frozen=True)
class SandboxResult:
    """Result of one disposable verifier command."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    image: str = ""
    container_name: str = ""

    @property
    def output(self) -> bytes:
        return self.stdout + self.stderr


def _safe_relative_path(raw: str) -> Path:
    """Validate one workspace-relative path before copying it into a sandbox."""
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or not path.parts:
        raise SandboxError(f"unsafe sandbox path: {raw!r}")
    if any(part in ("", ".", "\\") for part in path.parts):
        raise SandboxError(f"unsafe sandbox path: {raw!r}")
    return Path(*path.parts)


def _build_archive(
    files: dict[str, str],
    tests_source: str,
) -> bytes:
    """Build an in-memory archive containing only task-local files."""
    total = 0
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        for raw, body in files.items():
            rel = _safe_relative_path(raw)
            encoded = body.encode("utf-8")
            if len(encoded) > _MAX_FILE_BYTES:
                raise SandboxError(f"sandbox file exceeds {_MAX_FILE_BYTES} bytes: {raw}")
            total += len(encoded)
            if total > _MAX_WORKSPACE_BYTES:
                raise SandboxError("sandbox workspace exceeds the byte limit")
            info = tarfile.TarInfo(str(rel))
            info.size = len(encoded)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(encoded))

        tests = tests_source.encode("utf-8")
        if len(tests) > _MAX_FILE_BYTES:
            raise SandboxError("sandbox test source exceeds the byte limit")
        info = tarfile.TarInfo("task_tests.py")
        info.size = len(tests)
        info.mode = 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(tests))
    return archive_bytes.getvalue()


_BOOTSTRAP = r"""
import io
import os
from pathlib import Path, PurePosixPath
import sys
import tarfile
import unittest

root = Path("/workspace")
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:") as archive:
    for member in archive.getmembers():
        rel = PurePosixPath(member.name)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise RuntimeError("unsafe archive path")
        if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
            raise RuntimeError("unsupported archive member")
        if member.isdir():
            (root / Path(*rel.parts)).mkdir(parents=True, exist_ok=True)
            continue
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError("missing archive member")
        destination = root / Path(*rel.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read())

os.chdir(root)
suite = unittest.defaultTestLoader.loadTestsFromName("task_tests")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


def _docker_binary() -> str:
    binary = shutil.which("docker")
    if binary is None:
        raise SandboxUnavailable("Docker is not installed or not on PATH")
    return binary


def _bounded_error(proc: subprocess.CompletedProcess[bytes]) -> str:
    raw = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
    return raw.strip()[-1000:] or f"docker exited with status {proc.returncode}"


def _control(
    binary: str,
    args: list[str],
    *,
    timeout: float = _CONTROL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailable("Docker is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"Docker control command timed out: {args[0]}") from exc
    if proc.returncode != 0:
        raise SandboxError(_bounded_error(proc))
    return proc


def _best_effort_cleanup(binary: str, container_name: str) -> None:
    # Cleanup must not hide the test result or the original failure.
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [binary, "rm", "-f", container_name],
            capture_output=True,
            timeout=_CONTROL_TIMEOUT_SECONDS,
            check=False,
        )


def _docker_run_args(
    image: str,
    container_name: str,
    *,
    memory: str,
    cpus: str,
    pids_limit: int,
) -> list[str]:
    """Build the closed Docker argument list used for verifier containers."""
    if not image or any(ch.isspace() for ch in image):
        raise SandboxError("Docker image must be a non-empty value without whitespace")
    if pids_limit < 1:
        raise SandboxError("pids_limit must be positive")
    return [
        "run",
        "--name", container_name,
        "--rm",
        "-i",
        "--pull=never",
        "--label", "orchestral.sandbox=code-verifier",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,size=64m,uid=65532,gid=65532,mode=0700",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(pids_limit),
        "--memory", memory,
        "--cpus", cpus,
        "--user", "65532:65532",
        "--workdir", "/workspace",
        "--env", "HOME=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTHONUNBUFFERED=1",
        "--env", "PYTHONPATH=/workspace",
        "--init",
        "--stop-timeout", "1",
        image,
        "python", "-c", _BOOTSTRAP,
    ]


def run_docker_unittest(
    files: dict[str, str],
    tests_source: str,
    *,
    timeout_seconds: float,
    image: str | None = None,
    memory: str = DEFAULT_MEMORY,
    cpus: str = DEFAULT_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
) -> SandboxResult:
    """Run a unittest source in a disposable, network-disabled container.

    The submitted file map and verifier are copied into a temporary Docker
    container. No host directory, socket, device, or bind mount is passed to
    Docker. The container is removed in a ``finally`` block, including after
    timeouts and copy/start failures.
    """
    binary = _docker_binary()
    image = image or os.environ.get("ORCHESTRAL_DOCKER_IMAGE") or DEFAULT_DOCKER_IMAGE
    container_name = f"orchestral-code-{uuid.uuid4().hex[:12]}"
    try:
        archive = _build_archive(files, tests_source)
    except SandboxError:
        raise
    except (OSError, UnicodeError, tarfile.TarError) as exc:
        raise SandboxError(f"could not build sandbox input archive: {exc}") from exc

    try:
        # Refuse to pull implicitly. Image acquisition is an explicit,
        # reviewable setup step and cannot become task-triggered network access.
        _control(binary, ["image", "inspect", image])
        try:
            proc = subprocess.run(
                [
                    binary,
                    *_docker_run_args(
                        image,
                        container_name,
                        memory=memory,
                        cpus=cpus,
                        pids_limit=pids_limit,
                    ),
                ],
                input=archive,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                returncode=124,
                stdout=exc.stdout or b"",
                stderr=exc.stderr or b"",
                timed_out=True,
                image=image,
                container_name=container_name,
            )
        # Docker uses 125 for daemon failures, but a verifier process can also
        # exit with 125. There is no reliable sentinel in the CLI result, so
        # preserve every non-zero verifier status as a normal test result.
        return SandboxResult(
            returncode=proc.returncode,
            stdout=proc.stdout or b"",
            stderr=proc.stderr or b"",
            image=image,
            container_name=container_name,
        )
    finally:
        _best_effort_cleanup(binary, container_name)
