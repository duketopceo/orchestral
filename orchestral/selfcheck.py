"""Spec self-verification — does each spec's own reference pass its own grading?

Three layers, one command:

- default: replay the spec's declared reference material through the same
  validators a run applies (the dry-run candidate seam) — no model calls, no
  subprocesses, no RunStore writes;
- ``--execute``: run ``metadata.tests`` against the spec's reference fileset
  in a host subprocess. This executes repo-authored content only — spec tests
  against spec references — and exists to prove our tests accept our
  reference. It is never reachable from ``Runner`` and never touches model
  artifacts, so it does not conflict with the fail-closed code-execution
  posture (which governs untrusted worker output);
- ``--runs``: empirical discrimination report over stored runs — which checks
  discriminate models and which are walls or rubber stamps. Advisory only:
  findings depend on whatever data happens to be in ``runs/`` and never gate.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .audit import ERROR, INFO, WARN, Finding, load_specs
from .codeexec import materialize
from .config import TaskSpec
from .patch import PatchError, apply_unified_diff, extract_patch
from .runner import Runner
from .storage import RunStore

MIN_SAMPLE = 5
CEILING_RATE = 0.95


class _Probe(Runner):
    """Runner-shaped validator host with no store, clients, or run state.

    ``Runner._validate*`` methods read only ``self.dry_run``, so a bare
    subclass gets the real grading code paths without constructing a
    RunStore (and therefore without writing an index.db anywhere).
    """

    def __init__(self) -> None:
        self.dry_run = True


# ---------------------------------------------------------------------------
# Layer A — replay the spec's reference through its own validators
# ---------------------------------------------------------------------------

# Types whose spec ships reference material the default layer can replay.
# (html/multi-file/image/video have no replayable reference; see
# _unverifiable_families for the suite-level rollup.)
_REPLAYABLE = {"sql", "terminal", "api", "extract", "needle", "constraint", "pipeline",
               "code", "bugfix", "swe-patch"}


def _reference_files(spec: TaskSpec) -> dict[str, str] | None:
    """The fileset the hidden suite should accept, or None.

    `code`/`bugfix` carry it under ``metadata.reference``; `swe-patch` derives
    it by applying ``metadata.patch`` to ``metadata.files``.
    """
    md = spec.metadata
    if spec.type == "swe-patch":
        repo = {str(k): str(v) for k, v in (md.get("files") or {}).items()}
        patch = extract_patch(str(md.get("patch") or ""))
        if patch is None:
            return None
        try:
            return apply_unified_diff(repo, patch)
        except PatchError:
            return None
    ref = md.get("reference")
    if isinstance(ref, dict) and ref:
        return {str(k): str(v) for k, v in ref.items()}
    return None


def _candidate(spec: TaskSpec) -> Any:
    """Reference candidate in the shape the validator expects — mirrors the
    dry-run candidate each planner emits."""
    md = spec.metadata
    t = spec.type
    if t == "sql":
        return str(md.get("reference_sql") or "")
    if t in ("terminal", "api", "extract"):
        key = {"terminal": "commands", "api": "calls", "extract": "expected"}[t]
        return json.dumps(md.get(key) or ([] if t != "extract" else {}))
    if t == "needle":
        return str(md.get("expected_answer") or "")
    if t in ("code", "bugfix"):
        return _reference_files(spec)
    if t == "swe-patch":
        return str(md.get("patch") or "")
    # constraint / pipeline / html
    return str(md.get("reference_text") or "")


def _has_reference(spec: TaskSpec) -> bool:
    md = spec.metadata
    t = spec.type
    if t in ("code", "bugfix"):
        return _reference_files(spec) is not None
    if t == "swe-patch":
        return bool(str(md.get("patch") or "").strip())
    key = {
        "sql": "reference_sql", "terminal": "commands", "api": "calls",
        "extract": "expected", "needle": "expected_answer",
        "constraint": "reference_text", "pipeline": "reference_text",
    }[t]
    return bool(md.get(key))


def check_spec(spec: TaskSpec, path: Path | None, probe: Runner) -> list[Finding]:
    """Replay one spec's reference material through its validators."""
    t = spec.type
    if t not in _REPLAYABLE:
        return []

    if not _has_reference(spec):
        return [Finding(
            rule="missing_reference",
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
    errors = "; ".join(str(e) for e in (report.get("errors") or [])[:5]) or "no detail"
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
    """
    report: dict[str, Any] = {
        "executed": False, "tests_run": 0, "ok": False,
        "timed_out": False, "error": None, "output_tail": "",
    }
    with tempfile.TemporaryDirectory(prefix="orchestral-selfcheck-") as tmp:
        root = Path(tmp)
        materialize(files, root)
        (root / "test_submitted.py").write_text(tests_source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "test_submitted"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            report.update(executed=True, timed_out=True,
                          error=f"suite exceeded {timeout_seconds}s")
            return report
        report["executed"] = True
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        report["output_tail"] = "\n".join(tail[-15:])
        ran = bad = 0
        for line in tail:
            if line.startswith("Ran "):
                with contextlib.suppress(IndexError, ValueError):
                    ran = int(line.split()[1])
            for token in ("failures=", "errors="):
                if token in line:
                    with contextlib.suppress(IndexError, ValueError):
                        bad += int(line.split(token)[1].rstrip(")").split(",")[0])
        report["tests_run"] = ran
        report["ok"] = proc.returncode == 0 and ran > 0 and bad == 0
        if ran == 0 and proc.returncode == 0:
            report["error"] = "suite ran zero tests"
        elif proc.returncode != 0:
            report["error"] = f"unittest exited {proc.returncode}"
    return report


def check_execution(
    spec: TaskSpec,
    path: Path | None,
    *,
    default_timeout: float = 30.0,
) -> list[Finding]:
    """Run ``metadata.tests`` against the spec's reference fileset."""
    tests = str(spec.metadata.get("tests") or "").strip()
    if not tests:
        return []
    fileset = _reference_files(spec)
    if fileset is None:
        # `missing_reference`/`reference_fails_validation` already fired in
        # layer A — note the skip rather than double-reporting.
        return [Finding(
            rule="execute_skipped_no_reference",
            severity=INFO,
            task_id=spec.id,
            path=str(path) if path else None,
            detail="hidden tests could not be executed: no reference fileset.",
        )]
    timeout = float(spec.metadata.get("timeout_seconds") or default_timeout)
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
        executed = (report.get("execution") or {}).get("executed")
        for name, ok in (report.get("checks") or {}).items():
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
    default_timeout: float = 30.0,
) -> tuple[list[Finding], int]:
    """Audit the suite against itself. Returns (findings, exit_code)."""
    loaded = load_specs(tasks_dir)
    probe = _Probe()
    findings: list[Finding] = []
    unverifiable: dict[str, int] = {}
    for path, spec in loaded:
        if task_id and spec.id != task_id:
            continue
        if spec.type not in _REPLAYABLE:
            unverifiable[spec.type] = unverifiable.get(spec.type, 0) + 1
        findings.extend(check_spec(spec, path, probe))
        if execute:
            findings.extend(
                check_execution(spec, path, default_timeout=default_timeout)
            )
    if unverifiable:
        detail = ", ".join(f"{t}×{n}" for t, n in sorted(unverifiable.items()))
        findings.append(Finding(
            rule="no_replayable_reference",
            severity=INFO,
            detail=(f"{sum(unverifiable.values())} specs have no replayable reference "
                    f"({detail}); grading sanity for these is judge/mechanical-config only."),
        ))
    if runs_dir is not None:
        findings.extend(check_runs(RunStore(runs_dir)))
    return findings, 1 if any(f.severity == ERROR for f in findings) else 0
