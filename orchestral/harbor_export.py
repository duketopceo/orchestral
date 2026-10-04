"""Harbor task-package export — spec → instruction + environment + verifier.

Emits a self-contained Harbor package under ``<out_dir>/<task_id>/``::

    task.toml             schema 1.1 — name, keywords, allowlisted metadata,
                          difficulty labels, verifier timeout/network_mode
    instruction.md      spec.prompt + an environment section adapting the
                        fileset-return contract (Harbor agents edit the
                        checkout directly)
    environment/
      Dockerfile        stages the registered fixture tarball + wheelhouse
      fixture.tar.gz    fixture tasks only — copied from fixtures/ so the
                        package builds without this repo
    tests/
      test.sh           setup_commands → oracle overlay → verify.command →
                        reward JSON (verdict = exit code)
      oracle/           metadata.test_files verbatim, repo-relative — the
                        answer key, which is why export needs --publish-keys
      checks.json       mechanical required/forbidden keys for non-fixture
                        types (LLM judges don't fit Harbor's verifier model)

Export publishes the answer key — by design. A runnable package embeds
expected outputs the same way wandr's own packages do. The guards:

- ``holdout.is_holdout`` refuses outright (the marker survives
  materialization, so a ``--tasks-dir`` pointed at a holdout arm still
  fails here).
- ``--publish-keys`` must be passed explicitly — no accidental key
  publication.
- Every emitted text file is scanned for ``holdout_secrets(spec)`` after
  generation. Vacuous for non-holdout specs (the set is empty) — it
  catches guard-bypass bugs, not normal input.
- task.toml serializes from a field allowlist, never spec.metadata
  wholesale (metadata can carry ``calls``, seeded haystacks, internal
  notes).
"""

from __future__ import annotations

import json
import shlex
import stat
from pathlib import Path
from typing import Any

from orchestral.config import TaskSpec
from orchestral.fileset import required_content
from orchestral.fixtures import (
    FixtureError,
    screen_members,
    verified_fixture_bytes,
)
from orchestral.holdout import holdout_secrets, is_holdout
from orchestral.privacy import scrub_text

_GUEST_ROOT = "/home/user"
_REPO_DIR = f"{_GUEST_ROOT}/repo"
# `/home/user` is the guest root inside the container — it is not a host
# path. scrub_text's home_path rule would mangle it, so guest paths are
# substituted out before scrubbing and restored after.
_GUEST_PLACEHOLDER = "@GUEST_ROOT@"


def _guest_scrub(text: str) -> str:
    """scrub_text over emitted content, preserving the guest-root paths."""
    scrubbed = scrub_text(text.replace(_GUEST_ROOT, _GUEST_PLACEHOLDER))
    return scrubbed.replace(_GUEST_PLACEHOLDER, _GUEST_ROOT)


# task.toml [metadata] allowlist — everything else in spec.metadata stays
# out of the published package
_METADATA_ALLOWLIST = (
    "difficulty",
    "archetype",
    "contamination_risk",
    "expected_paths",
    "workdir",
)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # json.dumps is a valid TOML basic-string serializer — handles
    # newlines, tabs, quotes and backslashes uniformly
    return json.dumps(str(value))


def _task_toml(spec: TaskSpec, *, verifier_mode: str,
               timeout_seconds: float) -> str:
    meta = spec.metadata or {}
    keywords = ["orchestral", spec.type]
    if meta.get("archetype"):
        keywords.append(str(meta["archetype"]))
    lines = [
        'schema_version = "1.1"',
        "",
        "[task]",
        f'name = "orchestral/{spec.id}"',
        f'description = {_toml_value(spec.title or spec.id)}',
        'authors = [{ name = "orchestral" }]',
        f"keywords = {_toml_value(keywords)}",
        "",
        "[metadata]",
        f'task_id = {_toml_value(spec.id)}',
        f'task_type = {_toml_value(spec.type)}',
        f'verifier_mode = {_toml_value(verifier_mode)}',
        'source = "orchestral harbor export"',
    ]
    if spec.blurb:
        lines.append(f"blurb = {_toml_value(spec.blurb)}")
    for key in _METADATA_ALLOWLIST:
        if meta.get(key) not in (None, ""):
            lines.append(f"{key} = {_toml_value(meta[key])}")
    verify = meta.get("verify")
    if isinstance(verify, dict) and verify.get("fail_to_pass"):
        lines.append(f"fail_to_pass = {_toml_value(verify['fail_to_pass'])}")
    lines += [
        "",
        "[verifier]",
        f"timeout_sec = {_toml_value(timeout_seconds)}",
        # the fixture contract forbids guest network (wheelhouse installs
        # are --no-index); mechanical checks never need it either
        'network_mode = "none"',
        "",
        "[agent]",
        f"timeout_sec = {_toml_value(max(timeout_seconds * 4, 1200.0))}",
        'network_mode = "none"',
        "",
        "[environment]",
        "build_timeout_sec = 600.0",
        "cpus = 2",
        "memory_mb = 4096",
        'network_mode = "none"',
        "",
    ]
    return "\n".join(lines)


def _instruction(spec: TaskSpec, *, fixture: bool) -> str:
    body = spec.prompt.rstrip()
    env = (
        "\n\n## Environment\n\n"
        "You are working inside a container. "
        + (
            f"The repository checkout is at `{_REPO_DIR}` — make your "
            "changes directly in that directory."
            if fixture
            else f"Write your output files under `{_GUEST_ROOT}`."
        )
        + " Do not return a JSON fileset — modify files in place; the "
        "verifier reads the filesystem.\n"
    )
    return body + env


def _dockerfile(*, fixture: bool) -> str:
    if not fixture:
        return (
            "FROM python:3.12-slim\n"
            f"RUN mkdir -p {_GUEST_ROOT}\n"
            f"WORKDIR {_GUEST_ROOT}\n"
        )
    return (
        "FROM python:3.12-slim\n"
        "COPY fixture.tar.gz /tmp/fixture.tar.gz\n"
        f"RUN mkdir -p {_GUEST_ROOT} "
        f"&& tar xzf /tmp/fixture.tar.gz -C {_GUEST_ROOT} "
        "&& rm /tmp/fixture.tar.gz\n"
        f"WORKDIR {_REPO_DIR}\n"
    )


def _test_sh(spec: TaskSpec, *, fixture: bool,
             timeout_seconds: float) -> str:
    meta = spec.metadata or {}
    setup = [str(c) for c in meta.get("setup_commands") or []]
    verify = meta.get("verify") or {}
    command = [str(c) for c in verify.get("command") or []]
    lines = [
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        "",
        'tests_dir="${TESTS_DIR:-/tests}"',
        'logs_dir="${LOGS_DIR:-/logs/verifier}"',
        f'repo_dir="{_REPO_DIR}"' if fixture else f'work_dir="{_GUEST_ROOT}"',
        "",
        'mkdir -p "$logs_dir"',
        "reward() {",
        '  printf \'{"reward": %s, "exit_code": %s}\\n\' "$1" "$2" '
        '> "$logs_dir/reward.json"',
        "}",
        'fail() { rc="${2:-1}"; echo "$1"; reward 0.0 "$rc"; exit "$rc"; }',
        "",
    ]
    if fixture:
        lines += [
            "# oracle overlay — the graded tests land after agent edits so",
            "# the agent cannot rewrite them",
            'if [ -d "$tests_dir/oracle" ]; then',
            '  cp -a "$tests_dir/oracle/." "$repo_dir/" || fail "oracle overlay failed" 1',
            "fi",
            "",
            'cd "$repo_dir" || fail "repo dir missing" 1',
        ]
    else:
        lines.append('cd "$work_dir" || fail "workspace missing" 1')
    for i, cmd in enumerate(setup, 1):
        # the command string itself must stay raw to run — quoting it
        # into the message would corrupt test.sh on " or $ characters
        lines.append(f"{cmd} || fail \"setup failed (step {i})\" $?")
    if setup:
        lines.append("")
    if fixture:
        # export_task refuses fixture specs without verify.command —
        # checks.py/checks.json exist only on the non-fixture path, and
        # the fixture branch defines repo_dir, not work_dir
        lines.append(f"timeout {int(timeout_seconds)} {shlex.join(command)}")
        lines += [
            "rc=$?",
            'if [ "$rc" -eq 0 ]; then reward 1.0 0; exit 0; fi',
            'reward 0.0 "$rc"; exit "$rc"',
        ]
    else:
        # mechanical content checks — the package carries no LLM judge;
        # required/forbidden keys ship in checks.json
        lines += [
            'python3 "$tests_dir/checks.py" "$work_dir" '
            '"$tests_dir/checks.json"',
            "rc=$?",
            'if [ "$rc" -eq 0 ]; then reward 1.0 0; exit 0; fi',
            'reward 0.0 "$rc"; exit "$rc"',
        ]
    return "\n".join(lines) + "\n"


_CHECKS_PY = '''"""Mechanical content checks for a non-fixture Harbor export.

required[] strings must each appear in some workspace file; forbidden[]
strings must appear in none — both matched case-insensitively, the same
rule as the harness's own has_required/no_forbidden checks. This is the
export's whole verifier — judge-evaluated specs lose semantic grading
outside orchestral.
"""
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
checks = json.loads(Path(sys.argv[2]).read_text())
bodies = [p.read_text(errors="replace").lower() for p in work.rglob("*") if p.is_file()]
missing = [s for s in checks.get("required", []) if not any(s.lower() in b for b in bodies)]
present = [s for s in checks.get("forbidden", []) if any(s.lower() in b for b in bodies)]
for s in missing:
    print(f"required content missing: {s[:80]}")
for s in present:
    print(f"forbidden content present: {s[:80]}")
sys.exit(1 if (missing or present) else 0)
'''

# mechanical graded keys for non-fixture exports — expected_answer and
# required/forbidden lists are the checkable part of the v1 contract.
# required_content is a {path: [tokens]} mapping — the dict shape the
# canonical fileset parser returns — so it flattens in separately.
_REQUIRED_KEYS = ("required", "expected_answer")
_FORBIDDEN_KEYS = ("forbidden",)


def _mechanical_checks(spec: TaskSpec) -> dict[str, list[str]]:
    meta = spec.metadata or {}
    required: list[str] = []
    for key in _REQUIRED_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            required.append(value)
        elif isinstance(value, list):
            required.extend(v for v in value if isinstance(v, str) and v.strip())
    for tokens in required_content(meta).values():
        required.extend(tokens)
    forbidden: list[str] = []
    for key in _FORBIDDEN_KEYS:
        value = meta.get(key)
        if isinstance(value, list):
            forbidden.extend(v for v in value if isinstance(v, str) and v.strip())
        elif isinstance(value, str) and value.strip():
            forbidden.append(value)
    return {"required": list(dict.fromkeys(required)),
            "forbidden": list(dict.fromkeys(forbidden))}


def export_task(
    spec: TaskSpec,
    out_root: Path | str,
    *,
    publish_keys: bool,
    fixtures_dir: Path | str = "fixtures",
) -> Path:
    """Write a Harbor package for ``spec`` under ``out_root/<spec.id>/``.

    Returns the package directory. Raises ``ValueError`` on guard
    violations — holdout specs, missing ``--publish-keys``, a fixture
    blob that isn't fetched, or a non-fixture spec with no mechanical
    checks to export (there would be nothing to verify).
    """
    if is_holdout(spec):
        raise ValueError(
            f"refusing to export holdout spec {spec.id!r} — a Harbor "
            "package embeds the answer key, and holdout keys never leave "
            "this machine"
        )
    if not publish_keys:
        raise ValueError(
            "harbor export publishes answer keys by design — re-run with "
            "--publish-keys to acknowledge that this spec's expected "
            "outputs become public"
        )
    meta = spec.metadata or {}
    fixture_id = str(meta.get("fixture") or "")
    fixture = bool(fixture_id)
    timeout_seconds = float(meta.get("timeout_seconds") or 600)

    # all validation before the first mkdir — a refused export must not
    # leave a half-written package skeleton in dist/
    fixture_blob: bytes | None = None
    if fixture:
        verify = meta.get("verify") or {}
        if not verify.get("command"):
            raise ValueError(
                f"{spec.id!r} declares a fixture but no verify.command — "
                "the Harbor verifier would have nothing to run"
            )
        # verified_fixture_bytes is the grading-time contract: id format,
        # lock sha256, member screen — export can't bypass it by taking
        # the tarball path directly
        try:
            fixture_blob = verified_fixture_bytes(fixture_id, fixtures_dir)
        except FixtureError as exc:
            raise ValueError(str(exc)) from exc
        oracle = {str(k): str(v) for k, v in (meta.get("test_files") or {}).items()}
        bad = screen_members(oracle)
        if bad:
            raise ValueError(
                f"{spec.id!r} test_files carry unsafe member names "
                f"({', '.join(bad[:5])}) — refusing to export"
            )
    checks = None
    if not fixture:
        checks = _mechanical_checks(spec)
        if not checks["required"] and not checks["forbidden"]:
            raise ValueError(
                f"{spec.id!r} has no fixture and no mechanical "
                "required/forbidden keys — there is nothing a Harbor "
                "verifier could check (LLM judges don't export)"
            )

    package = Path(out_root) / spec.id
    package.mkdir(parents=True, exist_ok=True)
    env_dir = package / "environment"
    tests_dir = package / "tests"
    env_dir.mkdir(exist_ok=True)
    tests_dir.mkdir(exist_ok=True)

    emitted: dict[Path, str] = {
        package / "task.toml": _task_toml(
            spec, verifier_mode="suite" if fixture else "mechanical",
            timeout_seconds=timeout_seconds,
        ),
        package / "instruction.md": _instruction(spec, fixture=fixture),
        env_dir / "Dockerfile": _dockerfile(fixture=fixture),
        tests_dir / "test.sh": _test_sh(
            spec, fixture=fixture, timeout_seconds=timeout_seconds,
        ),
    }
    if fixture:
        for rel, body in oracle.items():
            target = tests_dir / "oracle" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            emitted[target] = body
        (env_dir / "fixture.tar.gz").write_bytes(fixture_blob or b"")
    else:
        emitted[tests_dir / "checks.py"] = _CHECKS_PY
        emitted[tests_dir / "checks.json"] = json.dumps(checks, indent=2) + "\n"

    secrets = {s for s in holdout_secrets(spec) if s}
    for path, text in emitted.items():
        scrubbed = _guest_scrub(text)
        for secret in secrets:
            if secret in scrubbed:
                raise ValueError(
                    f"holdout secret present in emitted {path.name} — "
                    "the is_holdout guard should have caught this; "
                    "refusing to write"
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scrubbed)
    mode = (tests_dir / "test.sh").stat().st_mode
    (tests_dir / "test.sh").chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return package
