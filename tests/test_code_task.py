"""Code task type: execution validator, dry-run path, runner integration."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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



def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role, input_price_per_mtok=0.03, output_price_per_mtok=0.10)


class TestRunUnittestSuite(unittest.TestCase):
    def test_live_execution_is_disabled_before_materialization(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "orchestral.codeexec.materialize"
        ) as materialize, patch("subprocess.run") as host_run:
            report = run_unittest_suite({"fizzbuzz.py": GOOD_IMPL}, TESTS)
        materialize.assert_not_called()
        host_run.assert_not_called()
        self.assertFalse(report["executed"])
        self.assertEqual(report["runtime"], "disabled")
        self.assertIn("no isolated runtime", report["error"])
        self.assertIsNone(score_from_report(report))

    def test_host_runtime_setting_cannot_fall_back_to_a_subprocess(self):
        for runtime in ("host", "enabled", "unknown"):
            with self.subTest(runtime=runtime), patch.dict(
                os.environ, {"ORCHESTRAL_CODE_RUNTIME": runtime}, clear=True
            ), patch("subprocess.run") as host_run:
                report = run_unittest_suite({"fizzbuzz.py": GOOD_IMPL}, TESTS)
            host_run.assert_not_called()
            self.assertFalse(report["executed"])
            self.assertIn("host subprocess", report["error"])

    def test_isolated_runtime_setting_still_requires_an_adapter(self):
        with patch.dict(os.environ, {"ORCHESTRAL_CODE_RUNTIME": "isolated"}, clear=True), patch("subprocess.run") as host_run:
            report = run_unittest_suite({"fizzbuzz.py": GOOD_IMPL}, TESTS)
        host_run.assert_not_called()
        self.assertFalse(report["executed"])
        self.assertIn("isolated runtime adapter", report["error"])

    def test_no_tests_source_is_still_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            report = run_unittest_suite({"fizzbuzz.py": GOOD_IMPL}, "")
        self.assertFalse(report["executed"])
        self.assertIn("no isolated runtime", report["error"])
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

    def test_validate_code_live_rejects_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=False, runs_dir=tmp, store=RunStore(tmp))
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={"module": "fizzbuzz.py", "tests": TESTS},
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "orchestral.runner.materialize"
            ) as materialize, patch("subprocess.run") as host_run:
                passes, report = runner._validate_code(task, {"fizzbuzz.py": GOOD_IMPL})
        materialize.assert_not_called()
        host_run.assert_not_called()
        self.assertFalse(passes)
        self.assertFalse(report["execution"]["executed"])
        self.assertEqual(report["execution"]["runtime"], "disabled")
        self.assertIsNone(report["score"])
        self.assertTrue(any("no isolated runtime" in error for error in report["errors"]))

    def test_code_execution_requires_a_completed_nonempty_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=False, runs_dir=tmp, store=RunStore(tmp))
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={"module": "fizzbuzz.py", "tests": TESTS},
            )
            faulty_suite = {
                "executed": True,
                "tests_run": 0,
                "ok": True,
                "error": None,
            }
            with patch("orchestral.runner.run_unittest_suite", return_value=faulty_suite):
                passes, report = runner._validate_code(task, {"fizzbuzz.py": GOOD_IMPL})
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["tests_pass"])
        self.assertTrue(any("non-empty test suite" in error for error in report["errors"]))

    def test_live_code_judge_does_not_assign_unexecuted_score(self):
        plan = json.dumps({"subtasks": [{"id": 0, "description": "write the module"}]})
        orchestrator = MagicMock()
        orchestrator.chat.return_value = {
            "content": plan,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1,
            "id": "plan",
        }
        worker = MagicMock()
        worker.chat.return_value = {
            "content": json.dumps({"files": [{"path": "fizzbuzz.py", "content": GOOD_IMPL}]}),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1,
            "id": "worker",
        }
        judge = MagicMock()
        judge.chat.return_value = {
            "content": json.dumps({"score": 1.0, "passed": True, "reasoning": "looks good"}),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1,
            "id": "judge",
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(
                runs_dir=tmp,
                store=RunStore(tmp),
                clients={"orchestrator": orchestrator, "worker": worker, "judge": judge},
            )
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={"module": "fizzbuzz.py", "tests": TESTS},
            )
            with patch.dict(os.environ, {}, clear=True):
                meta = runner.run(task, _model("o/m", "orchestrator"), _model("w/m", "worker"), _model("j/m", "judge"))
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
        self.assertFalse(meta.passes)
        self.assertIsNone(meta.score)
        self.assertIsNone(report["score"])
        self.assertEqual(report["judge"]["score"], 1.0)

    def test_validate_code_quality_measured_not_gated(self):
        """Unsafe hits are reported, while the live execution gate stays closed."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=True, runs_dir=tmp, store=RunStore(tmp))
            task = TaskSpec(
                id="code-t", type="code", prompt="p",
                metadata={"module": "fizzbuzz.py", "tests": TESTS},
            )
            impl = GOOD_IMPL + "\n_secret = eval('1')\n"
            passes, report = runner._validate_code(task, {"fizzbuzz.py": impl})
            self.assertIn("quality", report)
            self.assertTrue(report["quality"]["unsafe_hits"])
            self.assertTrue(report["checks"]["quality_ok"])
            self.assertTrue(passes)

    def test_validate_code_declared_quality_gates(self):
        """A declared bound fails the compile-only path without executing code."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(dry_run=True, runs_dir=tmp, store=RunStore(tmp))
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
            self.assertIsNone(report["score"])
            self.assertTrue(any(v.startswith("no_unsafe") for v in report["errors"]))


if __name__ == "__main__":
    unittest.main()
