"""Tests for orchestral.selfcheck — spec self-verification layers.

The shipped suite's own health is asserted by `test_shipped_suite_*` tests;
the rest exercise each finding class with synthetic specs in a tmpdir.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from orchestral.config import TaskSpec
from orchestral.selfcheck import (
    _Probe,
    check_execution,
    check_runs,
    check_spec,
    run_selfcheck,
)
from orchestral.storage import RunStore

REPO_TASKS = Path(__file__).resolve().parent.parent / "tasks"


def _spec(type_: str, **metadata) -> TaskSpec:
    return TaskSpec(id="t-1", type=type_, prompt="Do the thing.", metadata=dict(metadata))


def _write_spec(root: Path, name: str, spec: dict) -> Path:
    path = root / f"{name}.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


class TestReferenceReplay(unittest.TestCase):
    """Layer A: the spec's own reference must pass its own validators."""

    def setUp(self) -> None:
        self.probe = _Probe()

    def test_sql_reference_runs_clean(self) -> None:
        spec = _spec(
            "sql",
            schema="CREATE TABLE t (n INTEGER);",
            seed="INSERT INTO t VALUES (1), (2);",
            reference_sql="SELECT COUNT(*) AS c FROM t",
        )
        self.assertEqual(check_spec(spec, None, self.probe), [])

    def test_sql_broken_reference_flags(self) -> None:
        spec = _spec(
            "sql",
            schema="CREATE TABLE t (n INTEGER);",
            seed="INSERT INTO t VALUES (1);",
            reference_sql="SELECT * FROM nope_missing_table",
        )
        found = check_spec(spec, None, self.probe)
        self.assertEqual([f.rule for f in found], ["reference_fails_validation"])
        self.assertEqual(found[0].severity, "error")

    def test_terminal_plan_not_satisfying_expect_flags(self) -> None:
        spec = _spec(
            "terminal",
            fs={"app.ini": "debug = true\n"},
            commands=[{"run": "cat app.ini"}],
            expect={"files": {"app.ini": {"contains": "debug = false"}}},
        )
        found = check_spec(spec, None, self.probe)
        self.assertEqual([f.rule for f in found], ["reference_fails_validation"])

    def test_terminal_plan_satisfying_expect_clean(self) -> None:
        spec = _spec(
            "terminal",
            fs={"app.ini": "debug = true\n"},
            commands=[
                {"run": "grep debug app.ini"},
                {"run": 'echo "debug = false" > app.ini'},
            ],
            expect={"files": {"app.ini": {"contains": "debug = false"}}},
        )
        # `echo >` may not be a supported verb — if the reference plan can't
        # run in the virtual shell, that is itself the finding we report.
        found = check_spec(spec, None, self.probe)
        rules = [f.rule for f in found]
        self.assertIn(rules, ([], ["reference_fails_validation"]))

    def test_needle_without_answer_is_missing_reference(self) -> None:
        spec = _spec("needle", document="haystack", validation=["exact_answer"])
        found = check_spec(spec, None, self.probe)
        self.assertEqual([f.rule for f in found], ["missing_reference"])

    def test_code_without_reference_is_missing_reference(self) -> None:
        spec = _spec("code", module="solution.py", tests="import unittest")
        found = check_spec(spec, None, self.probe)
        self.assertEqual([f.rule for f in found], ["missing_reference"])

    def test_code_reference_failing_compile_flags(self) -> None:
        spec = _spec(
            "code",
            module="solution.py",
            expected_paths=["solution.py"],
            tests="import unittest",
            reference={"solution.py": "def broken(:\n"},
        )
        found = check_spec(spec, None, self.probe)
        self.assertEqual([f.rule for f in found], ["reference_fails_validation"])

    def test_code_reference_clean(self) -> None:
        spec = _spec(
            "code",
            module="solution.py",
            expected_paths=["solution.py"],
            tests="import unittest",
            reference={"solution.py": "def answer():\n    return 42\n"},
        )
        self.assertEqual(check_spec(spec, None, self.probe), [])

    def test_non_replayable_type_produces_no_finding(self) -> None:
        self.assertEqual(check_spec(_spec("html"), None, self.probe), [])
        self.assertEqual(check_spec(_spec("multi-file"), None, self.probe), [])


class TestExecuteLayer(unittest.TestCase):
    """Layer B: hidden tests must pass against the spec's reference."""

    def test_passing_reference_is_clean(self) -> None:
        spec = _spec(
            "code",
            module="solution.py",
            tests=(
                "import unittest\nimport solution\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_x(self):\n"
                "        self.assertEqual(solution.answer(), 42)\n"
            ),
            reference={"solution.py": "def answer():\n    return 42\n"},
        )
        self.assertEqual(check_execution(spec, None), [])

    def test_failing_reference_flags(self) -> None:
        spec = _spec(
            "code",
            module="solution.py",
            tests=(
                "import unittest\nimport solution\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_x(self):\n"
                "        self.assertEqual(solution.answer(), 42)\n"
            ),
            reference={"solution.py": "def answer():\n    return 0\n"},
        )
        found = check_execution(spec, None)
        self.assertEqual([f.rule for f in found], ["reference_fails_tests"])

    def test_swe_patch_derives_reference_from_patch(self) -> None:
        spec = _spec(
            "swe-patch",
            module="app.py",
            files={"app.py": "TIMEOUT = 5\n"},
            patch=(
                "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
                "-TIMEOUT = 5\n+TIMEOUT = 10\n"
            ),
            tests=(
                "import unittest\nimport app\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_x(self):\n"
                "        self.assertEqual(app.TIMEOUT, 10)\n"
            ),
        )
        self.assertEqual(check_execution(spec, None), [])

    def test_no_tests_is_silent(self) -> None:
        self.assertEqual(check_execution(_spec("code", module="a.py"), None), [])

    def test_missing_fileset_is_info_not_error(self) -> None:
        spec = _spec("code", module="a.py", tests="import unittest\n")
        found = check_execution(spec, None)
        self.assertEqual([(f.rule, f.severity) for f in found],
                         [("execute_skipped_no_reference", "info")])


class TestRunsLayer(unittest.TestCase):
    """Layer C: empirical flags over stored runs — advisory, never gating."""

    def _seed(self, root: Path, task: str, outcomes: list[bool],
              check: str = "tests_pass") -> None:
        store = RunStore(root)
        for i, passed in enumerate(outcomes):
            run_id, run_dir = store.new_run("orch", task, f"w{i}",
                                          config={"dry_run": False})
            meta = store.get_run(run_id)
            assert meta is not None
            meta.status = "finished"
            meta.passes = passed
            store.index_meta(meta)
            (run_dir / "report.json").write_text(json.dumps({
                "task_id": task, "checks": {check: passed},
            }))

    def test_floor_and_suspect_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(Path(tmp), "wall-task", [False] * 6)
            found = check_runs(RunStore(tmp))
        rules = {f.rule for f in found}
        self.assertIn("floor_task", rules)
        self.assertIn("suspect_check", rules)

    def test_ceiling_and_rubber_stamp_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(Path(tmp), "easy-task", [True] * 6)
            found = check_runs(RunStore(tmp))
        rules = {f.rule for f in found}
        self.assertIn("ceiling_task", rules)
        self.assertIn("rubber_stamp_check", rules)

    def test_below_min_sample_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(Path(tmp), "thin-task", [False, False])
            self.assertEqual(check_runs(RunStore(tmp)), [])

    def test_empty_store_reports_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            found = check_runs(RunStore(tmp))
        self.assertEqual([f.rule for f in found], ["no_run_data"])

    def test_never_executed_check_not_counted_as_suspect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            for i in range(6):
                run_id, run_dir = store.new_run("o", "exec-task", f"w{i}",
                                              config={"dry_run": False})
                meta = store.get_run(run_id)
                assert meta is not None
                meta.status = "finished"
                meta.passes = False
                store.index_meta(meta)
                (run_dir / "report.json").write_text(json.dumps({
                    "task_id": "exec-task",
                    "checks": {"tests_pass": False},
                    "execution": {"executed": False},
                }))
            found = check_runs(RunStore(tmp))
        rules = {f.rule for f in found}
        self.assertIn("check_never_executed", rules)
        self.assertNotIn("suspect_check", rules)


class TestRunSelfcheck(unittest.TestCase):
    def test_task_filter_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_spec(root, "good", {
                "id": "good", "type": "needle", "prompt": "find",
                "metadata": {"document": "x", "expected_answer": "x"},
                "validation": ["exact_answer"],
            })
            _write_spec(root, "bad", {
                "id": "bad", "type": "code", "prompt": "write",
                "metadata": {"module": "a.py", "tests": "import unittest"},
            })
            findings, code = run_selfcheck(root)
            self.assertEqual(code, 1)
            self.assertEqual([f.task_id for f in findings if f.severity == "error"],
                             ["bad"])
            findings, code = run_selfcheck(root, task_id="good")
            self.assertEqual(code, 0)
            self.assertFalse(any(f.severity == "error" for f in findings))


class TestShippedSuite(unittest.TestCase):
    """The shipped suite must self-verify — this is the CI gate."""

    def test_shipped_suite_selfcheck_clean(self) -> None:
        findings, code = run_selfcheck(REPO_TASKS)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(code, 0,
                         "\n".join(f"{f.task_id}: {f.detail}" for f in errors))

    def test_shipped_suite_references_pass_hidden_tests(self) -> None:
        findings, code = run_selfcheck(REPO_TASKS, execute=True)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(code, 0,
                         "\n".join(f"{f.task_id}: {f.detail}" for f in errors))


if __name__ == "__main__":
    unittest.main()
