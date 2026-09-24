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

import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30

# Patterns that flag unsafe generated code — always scanned into the report's
# quality section; gating happens only when the task declares `no_unsafe`.
UNSAFE_PATTERNS: dict[str, str] = {
    "eval": r"\beval\s*\(",
    "exec": r"\bexec\s*\(",
    "os_system": r"\bos\.system\b",
    "subprocess": r"\bsubprocess\b",
    "pickle_loads": r"\bpickle\.loads?\b",
    "marshal_loads": r"\bmarshal\.loads?\b",
    "dunder_import": r"__import__",
    "ctypes": r"\bctypes\b",
    "raw_socket": r"\bsocket\.socket\b",
    "shell_out": r"\bos\.popen\b|\bcommands\.getoutput\b",
    "hard_delete": r"\bshutil\.rmtree\b|\bos\.remove\b|\bos\.unlink\b|\bos\.rmdir\b",
    "hardcoded_secret": r"(?:api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{6,}",
}
_COMPILED_UNSAFE = {name: re.compile(p) for name, p in UNSAFE_PATTERNS.items()}
_BRANCH_TOKENS = {"if", "elif", "else", "for", "while", "except", "and", "or", "assert", "with"}
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


def _file_metrics(body: str) -> dict[str, Any]:
    """Static metrics for one source file — parsed when possible."""
    lines = body.splitlines()
    code_lines = sum(1 for ln in lines if ln.strip() and not ln.strip().startswith("#"))
    metrics: dict[str, Any] = {
        "lines": len(lines),
        "code_lines": code_lines,
        "imports": [],
        "external_imports": [],
        "functions": 0,
        "max_function_lines": 0,
        "complexity_lite": 0,
        "parse_ok": True,
    }
    try:
        tree = ast.parse(body)
    except SyntaxError:
        metrics["parse_ok"] = False
        # still count imports/functions textually so unparseable code reports
        metrics["imports"] = re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)", body, re.MULTILINE)
        metrics["complexity_lite"] = sum(
            1 for ln in lines for tok in ln.split() if tok.rstrip(":") in _BRANCH_TOKENS
        )
        return metrics
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            metrics["imports"].extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            metrics["imports"].append(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            metrics["functions"] += 1
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            metrics["max_function_lines"] = max(metrics["max_function_lines"], span)
        elif isinstance(
            node,
            (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.Assert, ast.BoolOp, ast.comprehension),
        ):
            metrics["complexity_lite"] += 1
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    metrics["external_imports"] = sorted(
        {name.split(".")[0] for name in metrics["imports"] if name.split(".")[0] not in stdlib}
    )
    return metrics


def check_code_quality(
    files: dict[str, str], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Measure lean-ness and safety of a generated file set.

    Everything is *measured* unconditionally — report["quality"] is the
    observability record, including `unsafe_hits` from the builtin pattern
    set. Only declared bounds produce `violations` (which gate the run):
    `max_code_lines`, `max_functions`, `max_complexity_lite`, `no_unsafe`
    (gates builtin hits), `no_external_deps`, and `forbidden_patterns`
    (task-specific regex list — hits always gate when declared).
    """
    totals: dict[str, Any] = {
        "files": len(files),
        "total_lines": 0,
        "code_lines": 0,
        "imports": [],
        "external_imports": [],
        "functions": 0,
        "max_function_lines": 0,
        "complexity_lite": 0,
        "unsafe_hits": [],
        "violations": [],
        "unparseable": [],
    }
    all_imports: set[str] = set()
    all_external: set[str] = set()
    extra_patterns = {
        f"forbidden[{i}]": re.compile(p)
        for i, p in enumerate(metadata.get("forbidden_patterns") or [])
        if isinstance(p, str)
    }
    patterns = dict(_COMPILED_UNSAFE) | extra_patterns

    for rel, body in files.items():
        if not rel.endswith(".py"):
            totals["total_lines"] += len(body.splitlines())
            continue
        m = _file_metrics(body)
        totals["total_lines"] += m["lines"]
        totals["code_lines"] += m["code_lines"]
        totals["functions"] += m["functions"]
        totals["max_function_lines"] = max(totals["max_function_lines"], m["max_function_lines"])
        totals["complexity_lite"] += m["complexity_lite"]
        all_imports.update(m["imports"])
        all_external.update(m["external_imports"])
        if not m["parse_ok"]:
            totals["unparseable"].append(rel)
        for lineno, line in enumerate(body.splitlines(), 1):
            for name, rx in patterns.items():
                if rx.search(line):
                    totals["unsafe_hits"].append({"file": rel, "line": lineno, "pattern": name})

    # imports that resolve to a sibling module in the file set are local,
    # not external — a multi-module submission isn't pulling a dependency
    local_modules = {
        Path(rel).stem for rel in files if rel.endswith(".py")
    } | {rel.split("/")[0] for rel in files if "/" in rel}
    all_external -= local_modules

    totals["imports"] = sorted(all_imports)
    totals["external_imports"] = sorted(all_external)

    bounds = {
        "max_code_lines": totals["code_lines"],
        "max_functions": totals["functions"],
        "max_complexity_lite": totals["complexity_lite"],
    }
    for key, actual in bounds.items():
        limit = metadata.get(key)
        if limit is not None and actual > int(limit):
            totals["violations"].append(f"{key}: {actual} > {limit}")
    builtin_hits = [h for h in totals["unsafe_hits"] if not h["pattern"].startswith("forbidden[")]
    forbidden_hits = [h for h in totals["unsafe_hits"] if h["pattern"].startswith("forbidden[")]
    if metadata.get("no_unsafe") and builtin_hits:
        hit = builtin_hits[0]
        totals["violations"].append(
            f"no_unsafe: {len(builtin_hits)} hit(s), first {hit['pattern']} at {hit['file']}:{hit['line']}"
        )
    if forbidden_hits:
        hit = forbidden_hits[0]
        totals["violations"].append(
            f"forbidden_patterns: {len(forbidden_hits)} hit(s), first {hit['pattern']} at {hit['file']}:{hit['line']}"
        )
    if metadata.get("no_external_deps") and totals["external_imports"]:
        totals["violations"].append(f"no_external_deps: {', '.join(totals['external_imports'])}")
    return totals
