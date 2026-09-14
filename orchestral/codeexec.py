"""Execution validator for `code` tasks — run hidden tests against a fileset.

A code task's workers return a file set (same contract as multi-file). The
harness materializes it into a temp dir, writes the task's test source, and
runs `python -Es -m unittest` in a subprocess. Workers never see the tests —
they are evaluation evidence, not part of the spec.

Honesty note: `-Es` + a fresh temp dir + a timeout + a stripped environment
is *containment*, not a security sandbox — the code still runs with the
user's OS privileges. Only use this task type with models you would let
write code you execute locally.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30
# unittest's summary lines, e.g. "FAILED (failures=2, errors=1, skipped=1)"
_RAN_RE = re.compile(r"Ran (\d+) tests? in [\d.]+s")
_FAILED_RE = re.compile(r"FAILED \(([^)]*)\)")
_COUNT_RE = re.compile(r"(\w+)=(\d+)")
_TAIL_BYTES = 2000


def materialize(files: dict[str, str], dest: Path) -> None:
    """Write a sanitized file set under `dest` (paths already canonical)."""
    for rel, body in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def run_unittest_suite(
    files: dict[str, str],
    tests_source: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the task's unittest source against `files`; return a report.

    The report carries counts and a truncated output tail — never full file
    contents. `executed=False` means the subprocess never ran (e.g. no test
    source), distinct from a suite that ran and failed.
    """
    report: dict[str, Any] = {
        "executed": False,
        "tests_run": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "ok": False,
        "timed_out": False,
        "returncode": None,
        "output_tail": "",
    }
    if not tests_source.strip():
        report["error"] = "code task has no metadata.tests"
        return report

    with tempfile.TemporaryDirectory(prefix="orchestral-code-") as tmp:
        dest = Path(tmp)
        materialize(files, dest)
        test_path = dest / "task_tests.py"
        test_path.write_text(tests_source, encoding="utf-8")
        try:
            proc = subprocess.run(
                # -Es: ignore PYTHON* env vars and user site-packages, but
                # keep cwd importable (unlike -I, which would hide task_tests)
                [sys.executable, "-Es", "-m", "unittest", "-v", "task_tests"],
                cwd=dest,
                capture_output=True,
                timeout=timeout_seconds,
                # no env passthrough — no secrets in the child's environment
                env={"PATH": "/usr/bin:/bin"},
            )
        except subprocess.TimeoutExpired:
            report["timed_out"] = True
            report["executed"] = True
            report["output_tail"] = f"tests exceeded {timeout_seconds}s"
            return report

    report["executed"] = True
    report["returncode"] = proc.returncode
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    report["output_tail"] = out[-_TAIL_BYTES:]

    ran = _RAN_RE.search(out)
    if ran:
        report["tests_run"] = int(ran.group(1))
    failed = _FAILED_RE.search(out)
    if failed:
        for name, count in _COUNT_RE.findall(failed.group(1)):
            if name in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
                report[name if name in report else "errors"] = int(count)
    # a suite that ran zero tests is not a pass — import/collection failures
    # exit nonzero with no "Ran N tests" line at all
    report["ok"] = proc.returncode == 0 and ran is not None and report["tests_run"] > 0 and "OK" in out
    return report


def score_from_report(report: dict[str, Any]) -> float | None:
    """Fraction of tests passed; None only when the suite never executed."""
    if not report.get("executed"):
        return None
    if not report.get("tests_run"):
        return 0.0 if not report.get("ok") else None
    bad = report.get("failures", 0) + report.get("errors", 0)
    return max(0.0, (report["tests_run"] - bad) / report["tests_run"])
