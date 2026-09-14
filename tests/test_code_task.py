"""Code task type: execution validator, dry-run path, runner integration."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from orchestral.codeexec import check_code_quality, run_unittest_suite, score_from_report
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore

TESTS = """\
import unittest
from fizzbuzz import fizzbuzz

class TestFizzBuzz(unittest.TestCase):
    def test_fizz(self):
        self.assertEqual(fizzbuzz(3), "Fizz")

    def test_buzz(self):
        self.assertEqual(fizzbuzz(5), "Buzz")

    def test_plain(self):
        self.assertEqual(fizzbuzz(1), 1)
"""

GOOD_IMPL = """\
def fizzbuzz(n):
    if n % 15 == 0:
        return "FizzBuzz"
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return n
"""

BAD_IMPL = "def fizzbuzz(n):\n    return 'always wrong'\n"
SYNTAX_ERROR_IMPL = "def fizzbuzz(n\n"


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role, input_price_per_mtok=0.03, output_price_per_mtok=0.10)


class TestRunUnittestSuite(unittest.TestCase):
    def test_passing_suite(self):
        report = run_unittest_suite({"fizzbuzz.py": GOOD_IMPL}, TESTS)
        self.assertTrue(report["executed"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["tests_run"], 3)
        self.assertEqual(report["failures"], 0)
        self.assertEqual(score_from_report(report), 1.0)

    def test_failing_suite(self):
        report = run_unittest_suite({"fizzbuzz.py": BAD_IMPL}, TESTS)
        self.assertTrue(report["executed"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["tests_run"], 3)
        self.assertEqual(report["failures"], 3)
        self.assertEqual(score_from_report(report), 0.0)

    def test_import_error_fails_with_zero_score(self):
        report = run_unittest_suite({"fizzbuzz.py": SYNTAX_ERROR_IMPL}, TESTS)
        self.assertTrue(report["executed"])
        self.assertFalse(report["ok"])
        # loader dies before the runner starts: no "Ran N tests" line, rc=1
        self.assertEqual(report["tests_run"], 0)
        self.assertEqual(score_from_report(report), 0.0)

    def test_timeout(self):
        sleepy = TESTS.replace(
            "def test_fizz(self):",
            "def test_fizz(self):\n        import time; time.sleep(60);",
        )
        report = run_unittest_suite({"fizzbuzz.py": GOOD_IMPL}, sleepy, timeout_seconds=1)
        self.assertTrue(report["timed_out"])
        self.assertFalse(report["ok"])

    def test_no_tests_source(self):
        report = run_unittest_suite({"fizzbuzz.py": GOOD_IMPL}, "")
        self.assertFalse(report["executed"])
        self.assertIsNone(score_from_report(report))


LEAN_IMPL = """\
def fizzbuzz(n):
    if n % 15 == 0:
        return "FizzBuzz"
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return n
"""

BLOATED_IMPL = LEAN_IMPL + "\n" + "\n".join(
    f"def _unused_{i}(x):\n    return x + {i}" for i in range(12)
)

UNSAFE_IMPL = """\
import os

def fizzbuzz(n):
    if n <= 0:
        os.system("echo hi")
    return eval(str(n))
"""


class TestCheckCodeQuality(unittest.TestCase):
    def test_lean_metrics_measured(self):
        q = check_code_quality({"fizzbuzz.py": LEAN_IMPL}, {})
        self.assertEqual(q["files"], 1)
        self.assertGreater(q["code_lines"], 0)
        self.assertEqual(q["functions"], 1)
        self.assertGreater(q["complexity_lite"], 0)
        self.assertEqual(q["unsafe_hits"], [])
        self.assertEqual(q["violations"], [])
        self.assertEqual(q["unparseable"], [])

    def test_bloat_violation_gates(self):
        q = check_code_quality({"fizzbuzz.py": BLOATED_IMPL}, {"max_functions": 5})
        self.assertEqual(q["functions"], 13)
        self.assertTrue(any(v.startswith("max_functions") for v in q["violations"]))
        q = check_code_quality({"fizzbuzz.py": BLOATED_IMPL}, {"max_code_lines": 10})
        self.assertTrue(any(v.startswith("max_code_lines") for v in q["violations"]))

    def test_unsafe_measured_without_gating(self):
        q = check_code_quality({"fizzbuzz.py": UNSAFE_IMPL}, {})
        patterns = {h["pattern"] for h in q["unsafe_hits"]}
        self.assertIn("eval", patterns)
        self.assertIn("os_system", patterns)
        self.assertEqual(q["violations"], [])  # measured, not gated

    def test_no_unsafe_gates(self):
        q = check_code_quality({"fizzbuzz.py": UNSAFE_IMPL}, {"no_unsafe": True})
        self.assertTrue(any(v.startswith("no_unsafe") for v in q["violations"]))

    def test_no_external_deps_gates(self):
        files = {"m.py": "import requests\n\ndef f():\n    return requests.get('x')\n"}
        q = check_code_quality(files, {})
        self.assertEqual(q["external_imports"], ["requests"])
        self.assertEqual(q["violations"], [])
        q = check_code_quality(files, {"no_external_deps": True})
        self.assertTrue(any("requests" in v for v in q["violations"]))
        q = check_code_quality({"m.py": "import os, json\n"}, {"no_external_deps": True})
        self.assertEqual(q["violations"], [])

    def test_forbidden_patterns_gate(self):
        q = check_code_quality(
            {"fizzbuzz.py": LEAN_IMPL + "\nx = eval('1')\n"},
            {"forbidden_patterns": [r"\beval\s*\("]},
        )
        hits = [h for h in q["unsafe_hits"] if h["pattern"].startswith("forbidden[")]
        self.assertEqual(len(hits), 1)
        self.assertTrue(any(v.startswith("forbidden_patterns") for v in q["violations"]))

    def test_unparseable_still_measured(self):
        q = check_code_quality({"m.py": "def broken(:\n  import os\n"}, {})
        self.assertEqual(q["unparseable"], ["m.py"])
        self.assertIn("os", q["imports"])

    def test_non_py_files_counted_not_parsed(self):
        q = check_code_quality({"data.txt": "hello\nworld\n", "m.py": LEAN_IMPL}, {})
        self.assertEqual(q["files"], 2)
        self.assertEqual(q["unparseable"], [])


class TestCodeTaskRunner(unittest.TestCase):
    def test_dry_run_passes_with_compile_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(dry_run=True, runs_dir=tmp, store=RunStore(tmp)).run(
                TaskSpec(
                    id="code-t", type="code", prompt="implement fizzbuzz",
                    metadata={"module": "fizzbuzz.py", "tests": TESTS},
                ),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            self.assertEqual(meta.status, "finished")
            self.assertTrue(meta.passes)
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertFalse(report["executed"])  # dry-run compiles, never executes
            self.assertTrue(report["checks"]["compiles"])
            # artifact is a zip of the merged file set
            with zipfile.ZipFile(Path(meta.run_dir) / "artifact.zip") as zf:
                self.assertIn("fizzbuzz.py", zf.namelist())

    def test_validate_code_missing_module_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=True, runs_dir=tmp, store=RunStore(tmp))
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={"module": "fizzbuzz.py", "tests": TESTS},
            )
            passes, report = runner._validate_code(task, {"other.py": "x"})
            self.assertFalse(passes)
            self.assertFalse(report["checks"]["expected_paths"])
            self.assertIn("fizzbuzz.py", report["errors"][0])

    def test_validate_code_live_runs_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=False, runs_dir=tmp, store=RunStore(tmp))
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={"module": "fizzbuzz.py", "tests": TESTS},
            )
            passes, report = runner._validate_code(task, {"fizzbuzz.py": GOOD_IMPL})
            self.assertTrue(passes)
            self.assertEqual(report["score"], 1.0)
            passes, report = runner._validate_code(task, {"fizzbuzz.py": BAD_IMPL})
            self.assertFalse(passes)
            self.assertEqual(report["score"], 0.0)

    def test_validate_code_quality_measured_not_gated(self):
        """Unsafe hits land in report['quality'] but don't fail a task that
        didn't declare a bound."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=False, runs_dir=tmp, store=RunStore(tmp))
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={"module": "fizzbuzz.py", "tests": TESTS},
            )
            # passing impl that happens to contain an unsafe call — measured
            # but not gated since the task declares no bound
            impl = GOOD_IMPL + "\n_secret = eval('1')\n"
            passes, report = runner._validate_code(task, {"fizzbuzz.py": impl})
            self.assertIn("quality", report)
            self.assertTrue(report["quality"]["unsafe_hits"])
            self.assertTrue(report["checks"]["quality_ok"])
            self.assertTrue(passes)

    def test_validate_code_declared_quality_gates(self):
        """A declared bound fails the run even when tests all pass."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=False, runs_dir=tmp, store=RunStore(tmp))
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={
                    "module": "fizzbuzz.py", "tests": TESTS, "no_unsafe": True,
                },
            )
            impl = GOOD_IMPL + "\n_secret = eval('1')\n"
            passes, report = runner._validate_code(task, {"fizzbuzz.py": impl})
            self.assertFalse(passes)
            self.assertFalse(report["checks"]["quality_ok"])
            self.assertEqual(report["score"], 1.0)  # tests passed; quality gate failed
            self.assertTrue(any(v.startswith("no_unsafe") for v in report["errors"]))


if __name__ == "__main__":
    unittest.main()
