"""Repo fixtures for v3 real-repo tasks.

A *fixture* is a tarball staged into a CubeSandbox microVM at grade time:
``repo/`` holds the target repository at a pinned commit and ``wheelhouse/``
holds every wheel the verify path needs, so the guest never touches the
network. Fixtures are never committed — they are fetched on demand from a
pinned codeload URL recorded in ``fixtures/registry.yaml``, the same
model SWE-bench uses, which keeps third-party code (and its licenses) out of
this repository.

``registry.yaml`` is the committed contract::

    fixtures:
      boltons-25.1:
        repo: mahmoud/boltons                 # GitHub org/name
        commit: <full 40-hex sha>
        license: BSD-3-Clause                 # allowlist, see below
        license_url: https://github.com/mahmoud/boltons/blob/<sha>/LICENSE
        verify_deps: [pytest]                 # wheels staged into wheelhouse/
        notes: why this repo, contamination notes

Fetch lifecycle::

    harness.py fixtures fetch boltons-25.1   # build fixtures/boltons-25.1.tar.gz
    harness.py fixtures check                # registry vs on-disk drift
    harness.py fixtures list                 # registry state

The tarball carries a sha256 lock (``<id>.lock.json``) so a rebuilt fixture
that no longer matches its lock is a loud failure, not silent drift.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import yaml

FIXTURES_DIR = Path("fixtures")
REGISTRY_NAME = "registry.yaml"

# Licenses whose code we are comfortable redistributing inside a fetched
# (never committed) fixture tarball. Anything else fails the registry gate.
LICENSE_ALLOWLIST = frozenset(
    {
        "mit",
        "bsd-2-clause",
        "bsd-3-clause",
        "apache-2.0",
        "isc",
        "psf-2.0",
        "python-2.0",
    }
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Network-shaped tokens that must never appear in a fixture's guest commands —
# egress is a deployment accident, not a contract. Checked per shell segment:
# one `pip install --no-index` may not cover a different install in the same
# command line.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_NETWORK_TOKEN = re.compile(
    r"\b(curl|wget|git\s+(?:clone|fetch|pull)|pip3?\s+download|pip3?\s+install)\b"
)


def network_offense(command: str) -> str | None:
    """First shell segment that assumes guest network, else None."""
    for seg in _SEGMENT_SPLIT.split(command):
        m = _NETWORK_TOKEN.search(seg)
        if not m:
            continue
        if "install" in m.group(0) and "--no-index" in seg:
            continue
        return seg.strip()[:120]
    return None


class FixtureError(Exception):
    """Registry, fetch, or staging contract violation — always loud."""


@dataclass
class FixtureSpec:
    """One registry entry: the pinned repo + what its verify path needs."""

    id: str
    repo: str
    commit: str
    license: str
    license_url: str
    verify_deps: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def codeload_url(self) -> str:
        return f"https://codeload.github.com/{self.repo}/tar.gz/{self.commit}"


@dataclass
class FetchResult:
    fixture_id: str
    tarball: Path
    lockfile: Path
    sha256: str
    repo_members: int
    wheelhouse_members: int
    previous_sha256: str | None = None


def registry_path(root: Path | str = FIXTURES_DIR) -> Path:
    return Path(root) / REGISTRY_NAME


def _validate_entry(fixture_id: str, raw: Any) -> FixtureSpec:
    if not isinstance(raw, dict):
        raise FixtureError(f"registry entry {fixture_id!r} must be a mapping")
    if not _ID_RE.match(fixture_id):
        raise FixtureError(
            f"fixture id {fixture_id!r} must match {_ID_RE.pattern} — it becomes a filename"
        )
    repo = str(raw.get("repo") or "")
    if not _REPO_RE.match(repo):
        raise FixtureError(
            f"{fixture_id}: repo must be 'org/name' for codeload fetch, got {repo!r}"
        )
    commit = str(raw.get("commit") or "")
    if not _COMMIT_RE.match(commit):
        raise FixtureError(
            f"{fixture_id}: commit must be a full 40-hex sha (immutable pin), got {commit!r}"
        )
    license_ = str(raw.get("license") or "")
    if license_.lower() not in LICENSE_ALLOWLIST:
        raise FixtureError(
            f"{fixture_id}: license {license_ or '(missing)'!r} is not in the "
            f"redistribution allowlist {sorted(LICENSE_ALLOWLIST)} — do not vendor "
            "this repo's code"
        )
    license_url = str(raw.get("license_url") or "")
    if not license_url.startswith("https://"):
        raise FixtureError(
            f"{fixture_id}: license_url must be a public https URL a reader can check"
        )
    deps = raw.get("verify_deps") or []
    if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
        raise FixtureError(f"{fixture_id}: verify_deps must be a list of requirement strings")
    unpinned = [d for d in deps if "==" not in d]
    if unpinned:
        raise FixtureError(
            f"{fixture_id}: verify_deps must pin every requirement "
            f"(pkg==version) — unpinned: {', '.join(unpinned)}. Unpinned deps "
            "resolve to different wheels on different days, so the lock's "
            "sha256 stops meaning anything."
        )
    return FixtureSpec(
        id=fixture_id,
        repo=repo,
        commit=commit,
        license=license_,
        license_url=license_url,
        verify_deps=list(deps),
        notes=str(raw.get("notes") or ""),
    )


def load_registry(root: Path | str = FIXTURES_DIR) -> dict[str, FixtureSpec]:
    """Load + validate the registry. Empty/absent file ⇒ empty registry."""
    path = registry_path(root)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("fixtures"), dict):
        raise FixtureError(f"{path}: top-level 'fixtures:' mapping required")
    return {
        fid: _validate_entry(fid, entry)
        for fid, entry in sorted(raw["fixtures"].items())
    }


def tarball_path(fixture_id: str, root: Path | str = FIXTURES_DIR) -> Path:
    return Path(root) / f"{fixture_id}.tar.gz"


def lockfile_path(fixture_id: str, root: Path | str = FIXTURES_DIR) -> Path:
    return Path(root) / f"{fixture_id}.lock.json"


def screen_members(names: Iterable[str]) -> list[str]:
    """Reject tarball members that could write outside the workdir.

    Returns the offending member names. Applied host-side before a fixture is
    ever staged — absolute paths, ``..`` escapes, and symlinks/hardlinks are
    not representable in the screened-name list (links carry their own member
    type; callers filter by type separately), so this list is the name-level
    half of the contract.
    """
    bad: list[str] = []
    for name in names:
        p = PurePosixPath(name)
        if name.startswith("/") or ".." in p.parts:
            bad.append(name)
    return bad


def _screen_tar(tf: tarfile.TarFile) -> list[str]:
    """Member-level screen: bad names + non-regular-file member types."""
    bad = screen_members(m.name for m in tf.getmembers())
    for m in tf.getmembers():
        if m.islnk() or m.issym():
            bad.append(f"{m.name} (link → {m.linkname})")
        elif m.isdev() or m.isfifo():
            bad.append(f"{m.name} (device/fifo)")
    return bad


def _download(url: str, *, timeout: float = 120.0) -> bytes:
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _pip_download(deps: list[str], dest: Path) -> None:
    """Resolve wheels for the guest: cp312 on aarch64 manylinux or pure-any.

    Host python version/arch may differ from the guest, so the wheels must be
    selected for the guest (Debian aarch64, Python 3.12), not whatever pip
    would pick for this machine.
    """
    if not deps:
        return
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--only-binary", ":all:",
        "--platform", "manylinux_2_36_aarch64",
        "--platform", "manylinux_2_17_aarch64",
        "--platform", "any",
        "--python-version", "3.12",
        "--implementation", "cp",
        "--abi", "cp312",
        "--abi", "abi3",
        "--abi", "none",
        "--dest", str(dest),
        *deps,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise FixtureError(
            "pip download failed for wheelhouse — a verify dep may have no "
            f"pure-python or aarch64 manylinux wheel:\n{proc.stderr.strip()[-2000:]}"
        )


def fetch_fixture(
    fixture_id: str,
    root: Path | str = FIXTURES_DIR,
    *,
    downloader: Any | None = None,
) -> FetchResult:
    """Build ``<id>.tar.gz`` = ``repo/`` + ``wheelhouse/`` + sha256 lock."""
    registry = load_registry(root)
    spec = registry.get(fixture_id)
    if spec is None:
        raise FixtureError(
            f"{fixture_id}: not in {registry_path(root)} — register repo+commit+license first"
        )
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    get = downloader or _download

    raw = get(spec.codeload_url)
    try:
        src = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except tarfile.TarError as exc:
        raise FixtureError(f"{fixture_id}: codeload payload is not a .tar.gz: {exc}") from exc

    bad = _screen_tar(src)
    if bad:
        raise FixtureError(
            f"{fixture_id}: upstream tarball has {len(bad)} unsafe member(s) "
            f"({', '.join(bad[:5])}) — refusing to stage"
        )

    out = tarball_path(fixture_id, root)
    with tempfile.TemporaryDirectory(prefix="fixture-wh-") as wh_tmp:
        wh = Path(wh_tmp)
        if spec.verify_deps:
            _pip_download(spec.verify_deps, wh)
        wheels = sorted(wh.iterdir())
        n_members = 0
        # Deterministic bytes: zeroed gzip+member mtimes mean the same commit +
        # the same pinned wheels always produce the same sha256, so the lock
        # actually detects content changes instead of recording noise.
        with (
            open(out, "wb") as raw_out,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w") as dst,
        ):
            for m in src.getmembers():
                if not (m.isfile() or m.isdir()):
                    continue
                # codeload wraps the tree in <repo>-<sha>/ — strip one level;
                # the lone top-dir member (len==1) is dropped, not re-rooted.
                parts = PurePosixPath(m.name).parts
                if len(parts) <= 1:
                    continue
                rel = PurePosixPath(*parts[1:])
                if rel.name == "pax_global_header":
                    continue
                m2 = tarfile.TarInfo(str(PurePosixPath("repo") / rel))
                m2.mode, m2.mtime, m2.uid, m2.gid = m.mode, 0, 0, 0
                if m.isdir():
                    m2.type = tarfile.DIRTYPE
                    dst.addfile(m2)
                else:
                    data = src.extractfile(m)
                    m2.size = m.size
                    m2.type = tarfile.REGTYPE
                    dst.addfile(m2, data)
                n_members += 1
            for w in wheels:
                wi = tarfile.TarInfo(str(PurePosixPath("wheelhouse") / w.name))
                wi.size = w.stat().st_size
                wi.mtime, wi.uid, wi.gid = 0, 0, 0
                wi.type = tarfile.REGTYPE
                with open(w, "rb") as fh:
                    dst.addfile(wi, fh)

    previous_sha256: str | None = None
    old_lockfile = lockfile_path(fixture_id, root)
    if old_lockfile.exists():
        try:
            previous_sha256 = str(json.loads(old_lockfile.read_text()).get("sha256") or "") or None
        except (OSError, json.JSONDecodeError):
            previous_sha256 = None

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    lock = {
        "fixture_id": fixture_id,
        "repo": spec.repo,
        "commit": spec.commit,
        "sha256": digest,
        "repo_members": n_members,
        "wheelhouse_members": len(wheels),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "guest": {"python": "3.12", "platform": "manylinux_aarch64"},
    }
    lockfile = lockfile_path(fixture_id, root)
    lockfile.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return FetchResult(
        fixture_id=fixture_id,
        tarball=out,
        lockfile=lockfile,
        sha256=digest,
        repo_members=n_members,
        wheelhouse_members=len(wheels),
        previous_sha256=previous_sha256,
    )


def verified_fixture_bytes(fixture_id: str, root: Path | str = FIXTURES_DIR) -> bytes:
    """Grading-time contract: the lock must exist and match the tarball bytes.

    Raises FixtureError on a missing tarball/lock, a hash mismatch (edited or
    stale fetch), or a tarball that fails the member screen — a replaced
    tarball can otherwise bring links into the guest `tar xzf`.
    """
    if not _ID_RE.match(fixture_id):
        raise FixtureError(f"fixture id {fixture_id!r} must match {_ID_RE.pattern}")
    tb = tarball_path(fixture_id, root)
    if not tb.exists():
        raise FixtureError(
            f"fixture {fixture_id!r} not fetched — run `harness.py fixtures fetch {fixture_id}`"
        )
    lock_path = lockfile_path(fixture_id, root)
    if not lock_path.exists():
        raise FixtureError(
            f"fixture {fixture_id!r} has no lock file — re-run `fixtures fetch` to re-pin"
        )
    try:
        tarball = tb.read_bytes()
    except OSError as exc:
        raise FixtureError(f"fixture {fixture_id!r} unreadable: {exc}") from exc
    digest = hashlib.sha256(tarball).hexdigest()
    try:
        locked = str(json.loads(lock_path.read_text()).get("sha256") or "")
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"fixture {fixture_id!r} lock unreadable: {exc}") from exc
    if not locked or locked != digest:
        raise FixtureError(
            f"fixture {fixture_id!r} tarball sha256 {digest[:12]}… != lock "
            f"{locked[:12] or '(missing)'}… — edited or stale fetch; re-run "
            "`fixtures fetch` and re-verify"
        )
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            bad = _screen_tar(tf)
    except tarfile.TarError as exc:
        raise FixtureError(f"fixture {fixture_id!r} is not a .tar.gz: {exc}") from exc
    if bad:
        raise FixtureError(
            f"fixture {fixture_id!r} has {len(bad)} unsafe member(s) "
            f"({', '.join(bad[:5])}) — refusing to stage"
        )
    return tarball


def check_fixtures(root: Path | str = FIXTURES_DIR) -> list[str]:
    """Drift report: missing tarballs, stale locks, hash mismatches."""
    problems: list[str] = []
    registry = load_registry(root)
    for fid in sorted(registry):
        tb, lk = tarball_path(fid, root), lockfile_path(fid, root)
        if not tb.exists():
            problems.append(f"{fid}: tarball missing — run `fixtures fetch {fid}`")
            continue
        if not lk.exists():
            problems.append(f"{fid}: lock file missing — re-run `fixtures fetch {fid}`")
            continue
        lock = json.loads(lk.read_text(encoding="utf-8"))
        actual = hashlib.sha256(tb.read_bytes()).hexdigest()
        if lock.get("sha256") != actual:
            problems.append(
                f"{fid}: tarball hash drifted from lock ({actual[:16]}… ≠ {lock.get('sha256', '')[:16]}…)"
            )
        if lock.get("commit") != registry[fid].commit:
            problems.append(
                f"{fid}: lock commit {lock.get('commit', '')[:12]}… ≠ registry pin "
                f"{registry[fid].commit[:12]}… — re-fetch after the pin change"
            )
    return problems
