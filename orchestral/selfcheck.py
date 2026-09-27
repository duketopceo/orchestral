"""Spec self-verification — does each spec's own reference pass its own grading?

Three layers, one command:

- default: replay the spec's declared reference material through the same
  validators a run applies (the dry-run candidate seam) — no model calls, no
  subprocesses, no RunStore writes;
- ``--execute``: run ``metadata.tests`` against the spec's reference fileset
  in a host subprocess with a scrubbed environment and no network need.
  It executes spec-authored content — repo-authored in normal use, but
  contributor-authored when CI runs it on a pull-request checkout — and
  exists to prove our tests accept our reference. It is never reachable
  from ``Runner`` and never touches model artifacts, so it does not
  conflict with the fail-closed code-execution posture (which governs
  untrusted worker output);
- ``--runs``: empirical discrimination report over stored runs — which checks
  discriminate models and which are walls or rubber stamps. Advisory only:
  findings depend on whatever data happens to be in ``runs/`` and never gate.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .audit import ERROR, INFO, WARN, Finding, load_specs
from .codeexec import (
    DEFAULT_TIMEOUT_SECONDS,
    materialize,
    shadowing_members,
    summarize_unittest_output,
)
from .config import TaskSpec
from .fileset import FilesetError, sanitize_path
from .patch import PatchError, apply_unified_diff, extract_patch
from .runner import Runner
from .storage import RunStore

MIN_SAMPLE = 5
CEILING_RATE = 0.95
# Spec-declared suite timeouts are honored up to this ceiling — the execute
# layer is a CI gate, not a license to block a job indefinitely.
MAX_TIMEOUT_SECONDS = 300.0



class _Probe(Runner):
    """Runner-shaped validator host with no store, clients, or run state.

    ``Runner._validate*`` methods read only ``self.dry_run``, so a bare
    subclass gets the real grading code paths without constructing a
    RunStore (and therefore without writing an index.db anywhere).
    """

    def __init__(self) -> None:
        self.dry_run = True

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"_Probe has no {name!r}: a _validate* method now reads Runner "
            "state beyond self.dry_run — extend _Probe for the new dependency"
        )


# ---------------------------------------------------------------------------
# Layer A — replay the spec's reference through its own validators
# ---------------------------------------------------------------------------

# Types whose spec ships reference material the default layer can replay.
# (html/multi-file/image/video have no replayable reference; run_selfcheck
# rolls them up as a single no_replayable_reference info finding.)
_REPLAYABLE = {"sql", "terminal", "api", "extract", "needle", "constraint", "pipeline",
               "code", "bugfix", "swe-patch"}

# The metadata key carrying each replayable type's reference candidate —
# the same keys the planners' dry-run oracle reads.
_REFERENCE_KEY = {
    "sql": "reference_sql",
    "terminal": "commands",
    "api": "calls",
    "extract": "expected",
    "needle": "expected_answer",
    "constraint": "reference_text",
    "pipeline": "reference_text",
}


def _reference_files(spec: TaskSpec) -> dict[str, str] | None:
    """The fileset the hidden suite should accept, or None.

    `code`/`bugfix` carry it under ``metadata.reference``; `swe-patch` derives
    it by applying ``metadata.patch`` to ``metadata.files``. Keys are run
    through ``sanitize_path`` because ``materialize`` writes them verbatim —
    spec files are repo-authored but ``selfcheck --execute`` runs in CI on
    PR-supplied content.
    """
    md = spec.metadata
    if spec.type == "swe-patch":
        repo = {str(k): str(v) for k, v in (md.get("files") or {}).items()}
        patch = extract_patch(str(md.get("patch") or ""))
        if patch is None:
            return None
        try:
            files = apply_unified_diff(repo, patch)
        except PatchError:
            return None
    else:
        ref = md.get("reference")
        if not isinstance(ref, dict) or not ref:
            return None
        files = {str(k): str(v) for k, v in ref.items()}
    return {sanitize_path(k): v for k, v in files.items()}


def _candidate(spec: TaskSpec) -> Any:
    """Reference candidate in the shape the validator expects — mirrors the
    dry-run candidate each planner emits."""
    md = spec.metadata
    t = spec.type
    if t in ("terminal", "api", "extract"):
        return json.dumps(md.get(_REFERENCE_KEY[t]) or ([] if t != "extract" else {}))
    if t in ("code", "bugfix"):
        return _reference_files(spec)
    if t == "swe-patch":
        return str(md.get("patch") or "")
    # sql / needle / constraint / pipeline — a single string key
    return str(md.get(_REFERENCE_KEY[t]) or "")


def _has_reference(spec: TaskSpec) -> bool:
    md = spec.metadata
    t = spec.type
    if t in ("code", "bugfix"):
        return _reference_files(spec) is not None
    if t == "swe-patch":
        return bool(str(md.get("patch") or "").strip())
    return bool(md.get(_REFERENCE_KEY[t]))


def check_spec(spec: TaskSpec, path: Path | None, probe: Runner) -> list[Finding]:
    """Replay one spec's reference material through its validators."""
    t = spec.type
    if t not in _REPLAYABLE:
        return []

    try:
        has_ref = _has_reference(spec)
    except FilesetError as exc:
        return [Finding(
            rule="invalid_reference",
            severity=ERROR,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=f"reference fileset has unsafe paths: {exc}",
        )]
    if not has_ref:
        return [Finding(
            rule="missing_replayable_reference",
            severity=ERROR,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                f"{t} spec carries no reference material to self-verify against, "
                "so its grading cannot be checked without a model attempt."
            ),
        )]

    candidate = _candidate(spec)
    if t == "sql":
        passed, report = probe._validate_sql(spec, candidate)
    elif t == "terminal":
        passed, report = probe._validate_terminal(spec, candidate)
    elif t == "api":
        passed, report = probe._validate_api(spec, candidate)
    elif t == "extract":
        passed, report = probe._validate_extract(spec, candidate)
    elif t in ("code", "bugfix"):
        passed, report = probe._validate_code(spec, candidate)
    elif t == "swe-patch":
        passed, report = probe._validate_patch(spec, candidate)
    else:
        passed, report = probe._validate(spec, candidate)

    if passed:
        return []
    failures = (report.get("errors") or report.get("command_errors")
                or report.get("unexpected") or report.get("missing") or [])
    errors = "; ".join(str(e) for e in failures[:5]) or "no detail"
    return [Finding(
        rule="reference_fails_validation",
        severity=ERROR,
        task_id=spec.id,
        path=str(path) if path else None,
        detail=(
            f"the spec's own reference fails its own grading: {errors}. "
            "Either the reference or the checks are wrong — a spec-conforming "
            "model cannot distinguish which."
        ),
    )]


# ---------------------------------------------------------------------------
# Layer B — execute hidden tests against the reference fileset
# ---------------------------------------------------------------------------

def _run_suite_locally(
    files: dict[str, str],
    tests_source: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run ``python -m unittest`` on spec-authored content in a tmpdir.

    Dev-tooling executor for repo-authored test code and reference
    implementations only — never invoked on model artifacts (the eval path
    stays fail-closed in ``codeexec`` until an isolated runtime exists).
    Kept in sync with ``tests/execstub.py::real_unittest_suite``.
    """
    report: dict[str, Any] = {
        "executed": False, "tests_run": 0, "ok": False,
        "timed_out": False, "error": None, "output_tail": "",
    }
    # Specs arrive via PR — the suite subprocess gets a scrubbed environment
    # (no CI tokens, no PYTHON* overrides) and its own process group so a
    # timeout can reap spawned children rather than orphaning them.
    with tempfile.TemporaryDirectory(prefix="orchestral-selfcheck-") as tmp:
        root = Path(tmp)
        materialize(files, root)
        (root / "test_submitted.py").write_text(tests_source, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(root),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONHASHSEED": "0",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "unittest", "test_submitted"],
            cwd=tmp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            proc.kill()
            proc.communicate()
            report.update(executed=True, timed_out=True,
                          error=f"suite exceeded {timeout_seconds}s")
            return report
        report["executed"] = True
        tail = (err or out or "").strip().splitlines()
        report["output_tail"] = "\n".join(tail[-15:])
        ran, error = summarize_unittest_output("\n".join(tail), proc.returncode)
        report["tests_run"] = ran
        report["ok"] = proc.returncode == 0 and ran > 0
        if error:
            report["error"] = error
    return report


def check_execution(
    spec: TaskSpec,
    path: Path | None,
    *,
    default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[Finding]:
    """Run ``metadata.tests`` against the spec's reference fileset."""
    tests = str(spec.metadata.get("tests") or "").strip()
    if not tests:
        return []
    try:
        fileset = _reference_files(spec)
    except FilesetError:
        fileset = None
    if fileset is None:
        # `missing_replayable_reference`/`invalid_reference` already fired in
        # layer A — note the skip rather than double-reporting.
        return [Finding(
            rule="execute_skipped_no_reference",
            severity=INFO,
            task_id=spec.id,
            path=str(path) if path else None,
            detail="hidden tests could not be executed: no reference fileset.",
        )]
    shadowed = shadowing_members(fileset)
    if shadowed:
        return [Finding(
            rule="invalid_reference",
            severity=ERROR,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                "reference fileset members shadow the suite runtime: "
                + ", ".join(shadowed[:5])
            ),
        )]
    try:
        timeout = float(spec.metadata.get("timeout_seconds") or default_timeout)
    except (TypeError, ValueError):
        timeout = default_timeout
    if timeout <= 0:
        timeout = default_timeout
    timeout = min(timeout, MAX_TIMEOUT_SECONDS)
    suite = _run_suite_locally(fileset, tests, timeout_seconds=timeout)
    if suite["ok"]:
        return []
    detail = suite.get("error") or "hidden tests fail against the spec's reference"
    tail = str(suite.get("output_tail") or "").strip()
    if tail:
        detail = f"{detail} — {tail.splitlines()[-1][:200]}"
    return [Finding(
        rule="reference_fails_tests",
        severity=ERROR,
        task_id=spec.id,
        path=str(path) if path else None,
        detail=(
            f"{detail}. The fanout-records failure mode: spec-conforming "
            "implementations cannot pass, so the task measures nothing."
        ),
    )]


# ---------------------------------------------------------------------------
# Layer C — empirical discrimination report over stored runs
# ---------------------------------------------------------------------------

def check_runs(store: RunStore, *, min_sample: int = MIN_SAMPLE) -> list[Finding]:
    """Flag walls, ceilings, and rubber-stamp checks in stored live runs."""
    metas = [m for m in store.list_runs() if m.status == "finished" and not m.dry_run]
    if not metas:
        return [Finding(
            rule="no_run_data",
            severity=INFO,
            detail="no finished live runs in the index; nothing to measure.",
        )]

    task_outcomes: dict[str, list[bool]] = {}
    check_outcomes: dict[tuple[str, str], list[bool]] = {}
    never_executed: set[tuple[str, str]] = set()
    for meta in metas:
        if meta.passes is not None:
            task_outcomes.setdefault(meta.task_id, []).append(bool(meta.passes))
        if not meta.run_dir:
            continue
        report_path = Path(meta.run_dir) / "report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        execution = report.get("execution")
        executed = execution.get("executed") if isinstance(execution, dict) else None
        checks = report.get("checks")
        if not isinstance(checks, dict):
            continue
        for name, ok in checks.items():
            key = (meta.task_id, str(name))
            if name == "tests_pass" and executed is False:
                never_executed.add(key)
                continue
            check_outcomes.setdefault(key, []).append(bool(ok))

    findings: list[Finding] = []
    for task_id, outcomes in sorted(task_outcomes.items()):
        if len(outcomes) < min_sample:
            continue
        rate = sum(outcomes) / len(outcomes)
        if rate == 0.0:
            findings.append(Finding(
                rule="floor_task", severity=WARN, task_id=task_id,
                detail=(f"0/{len(outcomes)} live runs pass — a wall. Suspect a spec/test "
                        "defect before reading it as model difficulty."),
            ))
        elif rate >= CEILING_RATE:
            findings.append(Finding(
                rule="ceiling_task", severity=WARN, task_id=task_id,
                detail=(f"{sum(outcomes)}/{len(outcomes)} live runs pass — near-ceiling "
                        "task adds little ranking signal."),
            ))
    for (task_id, check), outcomes in sorted(check_outcomes.items()):
        if len(outcomes) < min_sample:
            continue
        rate = sum(outcomes) / len(outcomes)
        if rate == 0.0:
            findings.append(Finding(
                rule="suspect_check", severity=WARN, task_id=task_id,
                detail=(f"check '{check}' fails for all {len(outcomes)} live artifacts — "
                        "the fanout-records signature: likely contradicts the spec."),
            ))
        elif rate >= CEILING_RATE:
            findings.append(Finding(
                rule="rubber_stamp_check", severity=INFO, task_id=task_id,
                detail=(f"check '{check}' passes {sum(outcomes)}/{len(outcomes)} — "
                        "carries almost no discrimination."),
            ))
    for task_id, check in sorted(never_executed):
        findings.append(Finding(
            rule="check_never_executed", severity=INFO, task_id=task_id,
            detail=(f"check '{check}' never ran (fail-closed code execution) — "
                    "stored failures are infrastructure, not model signal."),
        ))
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_selfcheck(
    tasks_dir: str | Path = "tasks",
    *,
    execute: bool = False,
    runs_dir: str | Path | None = None,
    task_id: str | None = None,
    default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[Finding], int]:
    """Audit the suite against itself. Returns (findings, exit_code)."""
    loaded = load_specs(tasks_dir)
    probe = _Probe()
    findings: list[Finding] = []
    unverifiable: dict[str, int] = {}
    matched = False
    for path, spec in loaded:
        if task_id and spec.id != task_id:
            continue
        matched = True
        if spec.type not in _REPLAYABLE:
            unverifiable[spec.type] = unverifiable.get(spec.type, 0) + 1
        # One malformed spec must not abort the sweep — convert the crash
        # into a finding so CI reports it instead of a bare traceback.
        try:
            findings.extend(check_spec(spec, path, probe))
            if execute:
                findings.extend(
                    check_execution(spec, path, default_timeout=default_timeout)
                )
        except Exception as exc:
            findings.append(Finding(
                rule="selfcheck_error",
                severity=ERROR,
                task_id=spec.id,
                path=str(path) if path else None,
                detail=f"selfcheck crashed on this spec: {type(exc).__name__}: {exc}",
            ))
    if task_id and not matched:
        findings.append(Finding(
            rule="unknown_task",
            severity=ERROR,
            task_id=task_id,
            detail=f"no spec with id {task_id!r} under {tasks_dir}",
        ))
    if unverifiable:
        detail = ", ".join(f"{t}×{n}" for t, n in sorted(unverifiable.items()))
        findings.append(Finding(
            rule="no_replayable_reference",
            severity=INFO,
            detail=(f"{sum(unverifiable.values())} specs have no replayable reference "
                    f"({detail}); grading sanity for these is judge/mechanical-config only."),
        ))
    if runs_dir is not None:
        # Advisory read — don't let RunStore mkdir+init an empty index for a
        # wrong path; a missing index just means there is no data yet.
        if (Path(runs_dir) / "index.db").is_file():
            findings.extend(check_runs(RunStore(runs_dir)))
        else:
            findings.append(Finding(
                rule="no_run_data",
                severity=INFO,
                detail=f"no run index under {runs_dir}; nothing to measure.",
            ))
    return findings, 1 if any(f.severity == ERROR for f in findings) else 0
