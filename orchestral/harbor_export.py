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
      test.sh           setup_commands → oracle overlay → protected-path
                        check → verify.command → reward JSON
      oracle/           metadata.test_files verbatim, repo-relative — the
                        answer key, which is why export needs --publish-keys
      protected.sha256  fixture tasks only — sha256 of every test-owned
                        fixture member + verifier-toolchain names, mirroring
                        run_repo_suite's denylist so agent-edited or
                        agent-created test files fail verification
      checks.json       mechanical checks for non-fixture types, gated on
                        spec.validation the same way Runner._validate is
      checks.py         non-fixture verifier — mirrors the harness's check
                        semantics (per-path content, exact answers, patterns)

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
- The whole package is composed, scrubbed, and scanned before the first
  write — a refused export leaves nothing on disk, and a re-export
  replaces the previous package directory wholesale so stale oracle or
  checks files can't persist into a new package.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from orchestral.config import TaskSpec
from orchestral.fileset import expected_paths, required_content
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
    """scrub_text over prose content, preserving the guest-root paths."""
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

# spec.id feeds the package directory name and the task.toml name field —
# restrict it to a safe slug so `..`, `/`, quotes, or newlines in a spec
# can't escape out_root or inject TOML.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


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
        f'name = {_toml_value(f"orchestral/{spec.id}")}',
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
        "You are working inside a container with no network access. "
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


# Verifier-toolchain + config files a repo agent must not write — mirrors
# cubeexec's _VERIFY_TOOLCHAIN_SHADOW/_VERIFY_CONFIG_FILES/conftest rule.
_TOOLCHAIN_NAMES = frozenset({
    "conftest.py", "pytest.py", "pytest.ini", "tox.ini",
    "setup.cfg", "pyproject.toml", "_pytest",
})


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
            "# protected paths — mirror of run_repo_suite's denylist: every",
            "# test-owned fixture member must survive byte-for-byte, and no",
            "# agent-created test/toolchain file may exist outside it",
            'if [ -f "$tests_dir/protected.sha256" ]; then',
            '  while IFS= read -r line; do',
            '    sha="${line%%  *}"; rel="${line#*  }"',
            '    f="$repo_dir/$rel"',
            '    [ -f "$f" ] || fail "protected test file missing: $rel" 1',
            '    [ "$(sha256sum "$f" | cut -d\' \' -f1)" = "$sha" ] '
            '|| fail "protected test file modified: $rel" 1',
            '  done < "$tests_dir/protected.sha256"',
            '  while IFS= read -r f; do',
            '    rel="${f#"$repo_dir"/}"',
            '    grep -qF "  $rel" "$tests_dir/protected.sha256" '
            '|| fail "unauthorized test/toolchain file: $rel" 1',
            '  done < <(find "$repo_dir" -type f \\( -path "*/tests/*" '
            '-o -name "test_*.py" -o -name "*_test.py" '
            '-o -name "conftest.py" -o -name "pytest.py" '
            '-o -name "pytest.ini" -o -name "tox.ini" '
            '-o -name "setup.cfg" -o -name "pyproject.toml" \\) | sort)',
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
        # the requested checks ship in checks.json
        lines += [
            'python3 "$tests_dir/checks.py" "$work_dir" '
            '"$tests_dir/checks.json"',
            "rc=$?",
            'if [ "$rc" -eq 0 ]; then reward 1.0 0; exit 0; fi',
            'reward 0.0 "$rc"; exit "$rc"',
        ]
    return "\n".join(lines) + "\n"


_CHECKS_PY = '''"""Mechanical verifier for a non-fixture Harbor export.

Mirrors orchestral's own Runner._validate semantics: the agent's workspace
files are the artifact (bodies concatenated), every check family runs only
because the spec's validation list requested it, has_content tokens stay
bound to their declared path, and exact_answer is exact full-artifact
equality — not a substring anywhere in the workspace. Fail-closed like
the harness: a requested check whose metadata payload is missing fails.
"""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

work = Path(sys.argv[1])
checks = json.loads(Path(sys.argv[2]).read_text())
files = {
    str(p.relative_to(work)): p.read_text(errors="replace")
    for p in sorted(work.rglob("*")) if p.is_file()
}
artifact = "\\n".join(files.values())
lowered = artifact.lower()
errors = []


class _Validator(HTMLParser):
    def __init__(self):
        super().__init__()
        self.errors = []

    def error(self, message):
        self.errors.append(message)


if "html_parses" in checks:
    parser = _Validator()
    try:
        parser.feed(artifact)
    except Exception as exc:
        parser.errors.append(str(exc))
    errors.extend(f"HTML parse error: {e}" for e in parser.errors)

if "non_empty" in checks and not artifact.strip():
    errors.append("Artifact is empty.")

if "has_title" in checks and "<title>" not in lowered:
    errors.append("Missing <title>.")

if "has_cta" in checks and not any(
    token in lowered
    for token in (
        "cta", "sign up", "signup", "subscribe", "get started",
        "buy now", "learn more",
    )
):
    errors.append("Missing call-to-action.")

if "has_form" in checks and "<form" not in lowered:
    errors.append("Missing <form>.")

if ("has_viewport" in checks
        and 'name="viewport"' not in lowered
        and "name='viewport'" not in lowered):
    errors.append("Missing viewport meta tag.")

if "no_placeholder" in checks and any(
    token in lowered
    for token in (
        "lorem ipsum", "placeholder text", "todo:",
        "your text here", "[insert",
    )
):
    errors.append("Artifact contains placeholder text.")

if "within_budget" in checks:
    bounds = checks["within_budget"]
    if not bounds:
        errors.append("within_budget requested but no bounds declared.")
    actual = {
        "min_chars": len(artifact), "max_chars": len(artifact),
        "min_words": len(artifact.split()), "max_words": len(artifact.split()),
    }
    for key, limit in bounds.items():
        if (actual[key] < int(limit)) if key.startswith("min") \
                else (actual[key] > int(limit)):
            errors.append(f"{key} violated: {actual[key]} vs limit {limit}.")

if "has_required" in checks:
    required = checks["has_required"]
    missing = [t for t in required if t.lower() not in lowered]
    if not required:
        errors.append("has_required requested but metadata.required is empty.")
    elif missing:
        errors.append(f"Missing required token(s): {', '.join(missing)}.")

if "no_forbidden" in checks:
    forbidden = checks["no_forbidden"]
    hits = [t for t in forbidden if t.lower() in lowered]
    if not forbidden:
        errors.append("no_forbidden requested but metadata.forbidden is empty.")
    elif hits:
        errors.append(f"Forbidden token(s) present: {', '.join(hits)}.")

if "exact_answer" in checks:
    expected = checks["exact_answer"]
    if expected is None:
        errors.append("exact_answer requested but metadata.expected_answer is missing.")
    elif artifact.strip() != str(expected).strip():
        errors.append("Artifact is not exactly the expected answer.")

if "matches_pattern" in checks:
    pattern = checks["matches_pattern"]
    if not pattern:
        errors.append("matches_pattern requested but metadata.pattern is empty.")
    else:
        try:
            if re.search(str(pattern), artifact, re.DOTALL) is None:
                errors.append(f"Artifact does not match pattern {pattern!r}.")
        except re.error as exc:
            errors.append(f"metadata.pattern is not a valid regex: {exc}.")

if "no_pattern" in checks:
    patterns = checks["no_pattern"]
    if not patterns:
        errors.append("no_pattern requested but metadata.forbidden_pattern is empty.")
    for pattern in patterns:
        try:
            if re.search(str(pattern), artifact, re.DOTALL) is not None:
                errors.append(f"Artifact matches forbidden pattern {pattern!r}.")
        except re.error as exc:
            errors.append(f"metadata.forbidden_pattern is not a valid regex: {exc}.")

if "has_paths" in checks:
    declared = checks["has_paths"]
    missing = [p for p in declared if not files.get(p)]
    if not declared:
        errors.append("has_paths requested but metadata.expected_paths is empty.")
    elif missing:
        errors.append(f"Missing or empty expected files: {', '.join(missing)}.")

if "has_content" in checks:
    declared = checks["has_content"]
    absent, unmatched = [], []
    for path, tokens in sorted(declared.items()):
        body = files.get(path)
        if body is None:
            absent.append(path)
            continue
        haystack = body.lower()
        unmatched.extend(
            f"{path}:{token}" for token in tokens if token.lower() not in haystack
        )
    if not declared:
        errors.append("has_content requested but metadata.required_content is empty.")
    if absent:
        errors.append(f"No file body to read for: {', '.join(absent)}.")
    if unmatched:
        errors.append(f"Required token(s) missing from file bodies: {', '.join(unmatched)}.")

for e in errors:
    print(e)
sys.exit(1 if errors else 0)
'''

# Validation checks checks.py can faithfully reproduce against a plain
# workspace. Signature/container checks (zip/png/mp4) can't — a spec that
# requests one refuses to export rather than shipping a weaker verifier.
_EXPRESSIBLE_CHECKS = frozenset({
    "html_parses", "non_empty", "has_title", "has_cta", "has_form",
    "has_viewport", "no_placeholder", "within_budget",
    "has_required", "no_forbidden", "exact_answer",
    "matches_pattern", "no_pattern", "has_paths", "has_content",
})
# runner defaults when a spec declares no validation list, keyed where the
# type overrides the text default
_TYPE_VALIDATION_DEFAULTS = {
    "image": {"non_empty", "png_signature"},
    "video": {"non_empty", "mp4_signature"},
    "multi-file": {"non_empty", "zip_signature"},
}
_DEFAULT_TEXT_VALIDATION = {"html_parses", "non_empty", "has_title"}


def _requested_checks(spec: TaskSpec) -> set[str]:
    """The check names Runner._validate would apply for this spec."""
    if spec.validation:
        requested = {str(v) for v in spec.validation}
    else:
        requested = set(
            _TYPE_VALIDATION_DEFAULTS.get(spec.type, _DEFAULT_TEXT_VALIDATION)
        )
    # "html" is the generated-batch shorthand for html_parses + non_empty
    if "html" in requested:
        requested.discard("html")
        requested |= {"html_parses", "non_empty"}
    return requested


def _mechanical_checks(spec: TaskSpec) -> dict[str, Any]:
    """checks.json payload — one entry per requested check name, shaped
    like the check results Runner._validate produces."""
    meta = spec.metadata or {}
    requested = _requested_checks(spec)
    unexportable = sorted(requested - _EXPRESSIBLE_CHECKS)
    if unexportable:
        raise ValueError(
            f"{spec.id!r} requests validation checks with no Harbor "
            f"equivalent ({', '.join(unexportable)}) — refusing to ship "
            "a verifier weaker than the spec's own contract"
        )
    checks: dict[str, Any] = {}
    for name in sorted(requested):
        if name in ("non_empty", "html_parses", "has_title", "has_cta",
                    "has_form", "has_viewport", "no_placeholder"):
            checks[name] = True
        elif name == "within_budget":
            checks[name] = {
                k: meta[k]
                for k in ("min_chars", "max_chars", "min_words", "max_words")
                if meta.get(k) is not None
            }
        elif name == "has_required":
            checks[name] = [str(t) for t in meta.get("required") or []]
        elif name == "no_forbidden":
            forb = meta.get("forbidden")
            checks[name] = [
                str(t)
                for t in ([forb] if isinstance(forb, str) else (forb or []))
            ]
        elif name == "exact_answer":
            expected = meta.get("expected_answer")
            checks[name] = str(expected) if expected is not None else None
        elif name == "matches_pattern":
            checks[name] = str(meta.get("pattern") or "")
        elif name == "no_pattern":
            checks[name] = [
                str(p)
                for p in (
                    [meta.get("forbidden_pattern")]
                    if meta.get("forbidden_pattern") else []
                ) + list(meta.get("forbidden_patterns") or [])
                if p
            ]
        elif name == "has_paths":
            checks[name] = expected_paths(meta)
        elif name == "has_content":
            checks[name] = required_content(meta)
    return checks


def _protected_manifest(fixture_blob: bytes) -> str:
    """``sha256  repo/path`` lines for every test-owned fixture member —
    mirrors run_repo_suite's repo_tests + toolchain denylist so the
    exported verifier fails on agent-modified or agent-created test
    files, not just overwritten oracle files."""
    lines: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(fixture_blob), mode="r:gz") as ftf:
            for m in ftf.getmembers():
                if not (m.isfile() and m.name.startswith("repo/")):
                    continue
                rel = m.name[5:]
                name = PurePosixPath(rel).name
                if not (
                    "tests/" in rel
                    or name.startswith("test_")
                    or name.endswith("_test.py")
                    or name in _TOOLCHAIN_NAMES
                ):
                    continue
                body = ftf.extractfile(m)
                digest = hashlib.sha256(
                    body.read() if body else b""
                ).hexdigest()
                lines.append(f"{digest}  {rel}")
    except (tarfile.TarError, EOFError, OSError):
        return ""
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def export_task(
    spec: TaskSpec,
    out_root: Path | str,
    *,
    publish_keys: bool,
    fixtures_dir: Path | str = "fixtures",
) -> Path:
    """Write a Harbor package for ``spec`` under ``out_root/<spec.id>/``.

    Returns the package directory. Raises ``ValueError`` on guard
    violations — holdout specs, missing ``--publish-keys``, an unsafe
    spec id, a fixture blob that isn't fetched, unexportable requested
    checks, or a non-fixture spec with no mechanical checks to export
    (there would be nothing to verify). All validation happens before the
    first write: a refused export leaves nothing on disk, and a
    successful export replaces any previous package directory.
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
    if not _ID_RE.fullmatch(spec.id):
        raise ValueError(
            f"unsafe task id {spec.id!r} — ids must match "
            "[A-Za-z0-9][A-Za-z0-9._-]* (they name the package directory "
            "and the task.toml name field)"
        )
    meta = spec.metadata or {}
    fixture_id = str(meta.get("fixture") or "")
    fixture = bool(fixture_id)
    timeout_seconds = float(meta.get("timeout_seconds") or 600)

    # all validation before the first write — a refused export must not
    # leave a half-written package skeleton in dist/
    fixture_blob: bytes | None = None
    oracle: dict[str, str] = {}
    protected = ""
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
        oracle = {
            str(k): str(v) for k, v in (meta.get("test_files") or {}).items()
        }
        bad = screen_members(oracle)
        if bad:
            raise ValueError(
                f"{spec.id!r} test_files carry unsafe member names "
                f"({', '.join(bad[:5])}) — refusing to export"
            )
        protected = _protected_manifest(fixture_blob)
    checks = None
    if not fixture:
        checks = _mechanical_checks(spec)
        if not checks:
            raise ValueError(
                f"{spec.id!r} has no fixture and no requested mechanical "
                "checks — there is nothing a Harbor verifier could check "
                "(LLM judges don't export)"
            )

    out_root_p = Path(out_root)
    package = out_root_p / spec.id

    emitted: dict[str, str] = {}
    binary: dict[str, bytes] = {}
    emitted["task.toml"] = _task_toml(
        spec, verifier_mode="suite" if fixture else "mechanical",
        timeout_seconds=timeout_seconds,
    )
    emitted["instruction.md"] = _instruction(spec, fixture=fixture)
    emitted["environment/Dockerfile"] = _dockerfile(fixture=fixture)
    emitted["tests/test.sh"] = _test_sh(
        spec, fixture=fixture, timeout_seconds=timeout_seconds,
    )
    if fixture:
        for rel, body in oracle.items():
            emitted[f"tests/oracle/{rel}"] = body
        binary["environment/fixture.tar.gz"] = fixture_blob or b""
        if protected:
            emitted["tests/protected.sha256"] = protected
    else:
        emitted["tests/checks.py"] = _CHECKS_PY
        emitted["tests/checks.json"] = json.dumps(checks, indent=2) + "\n"

    # scrub + secret-scan everything in memory first — only the prose
    # surfaces get _guest_scrub (host-path redaction); verifier payloads
    # (test.sh, checks.*, oracle bodies) must match the bytes the real
    # harness writes verbatim, so they are scanned but never rewritten.
    secrets = {s for s in holdout_secrets(spec) if s}
    scrubbed: dict[str, str] = {}
    for rel, text in emitted.items():
        out_text = (
            _guest_scrub(text)
            if rel in ("instruction.md", "task.toml")
            else text
        )
        for secret in secrets:
            if secret in out_text:
                raise ValueError(
                    f"holdout secret present in emitted {rel} — "
                    "the is_holdout guard should have caught this; "
                    "refusing to write"
                )
        scrubbed[rel] = out_text

    # a re-export replaces the package wholesale — stale oracle or checks
    # files from a previous export can't survive into a new package
    if package.exists():
        shutil.rmtree(package)
    try:
        for rel, text in scrubbed.items():
            path = package / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for rel, blob in binary.items():
            path = package / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
    except OSError:
        shutil.rmtree(package, ignore_errors=True)
        raise
    mode = (package / "tests" / "test.sh").stat().st_mode
    (package / "tests" / "test.sh").chmod(
        mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return package
