"""Test-local stand-in for `orchestral.runner.run_unittest_suite`.

Production code execution is fail-closed until an isolated runtime exists
(`orchestral.codeexec`), so tests that exercise the runner end-to-end patch
the seam with this helper. It materializes the merged fileset plus the suite
into a temp dir and runs `python -m unittest` there — exactly the behavior an
isolated adapter will own, scoped to trusted test fixtures.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def real_unittest_suite(
    files: dict[str, str],
    tests_source: str,
    *,
    timeout_seconds: float = 30,
    **_kw: Any,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="orchestral-test-exec-") as tmp:
        root = Path(tmp)
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(body), encoding="utf-8")
        (root / "test_submitted.py").write_text(tests_source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "test_submitted"],
                cwd=tmp, capture_output=True, text=True, timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {
                "executed": True, "tests_run": 0, "failures": 0, "errors": 0,
                "skipped": 0, "ok": False, "timed_out": True, "returncode": None,
                "output_tail": "", "runtime": "test-subprocess",
                "error": f"suite exceeded {timeout_seconds}s",
            }
        tail = proc.stderr.strip().splitlines()
        # unittest prints "Ran N tests" and OK/FAILED tallies to stderr
        ran = 0
        bad = 0
        for line in tail:
            if line.startswith("Ran "):
                ran = int(line.split()[1])
            elif line.startswith("FAILED"):
                bad = sum(int(tok.split("=")[1]) for tok in line.split()[1:-1]
                          if "=" in tok)
        return {
            "executed": True, "tests_run": ran, "failures": bad, "errors": 0,
            "skipped": 0, "ok": proc.returncode == 0 and ran > 0,
            "timed_out": False, "returncode": proc.returncode,
            "output_tail": proc.stderr[-2000:], "runtime": "test-subprocess",
        }
