"""Code task type: execution validator, dry-run path, runner integration."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from orchestral.codeexec import run_unittest_suite, score_from_report
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


if __name__ == "__main__":
    unittest.main()
