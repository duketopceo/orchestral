"""Tests for the static task-spec audit and the fail-closed check-name guard."""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import TestCase

from orchestral import audit as orchestral_audit
from orchestral import config as orchestral_config
from orchestral.audit import (
    _CONSTANT_ASSERTIONS_FAILS,
    _CONSTANT_ASSERTIONS_NEUTRAL,
    _CONSTANT_ASSERTIONS_PASSES,
    _NO_COLLECTABLE_ASSERTION,
    _UNREADABLE_SUITE,
    ERROR,
    INFO,
    WARN,
    audit_spec,
    audit_suite,
    audit_tree,
    check_holdout_arm,
    effective_checks,
    find_duplicate_families,
)
from orchestral.config import TASK_TYPES, TaskSpec
from orchestral.runner import Runner

REPO_TASKS = Path(__file__).resolve().parent.parent / "tasks"

# minimal well-formed media: PNG magic + IEND, and an ISO-BMFF leading ftyp box
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"body" + b"IEND\xaeB`\x82"
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8

# A suite that runs one test and asserts nothing: it passes for any artifact.
NO_OP_SUITE = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"
# A suite whose assertion reads the artifact, so it can discriminate.
ASSERTING_SUITE = (
    "import unittest\n\nfrom solution import solve\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_x(self):\n"
    "        self.assertEqual(solve('a b'), 'a-b')\n"
)
# Suites that carry an assertion unittest will never run, or one that cannot fail.
UNCOLLECTED_SUITE = "def test_x():\n    assert True\n"
NOT_A_TEST_CASE = "class T:\n    def test_x(self):\n        assert True\n"
WRONG_NAME_SUITE = "import unittest\n\n\nclass T(unittest.TestCase):\n    def check_x(self):\n        assert True\n"
# Every assertion reads no value, so every one is provable and none discriminates.
# The rule is method-agnostic, so it must hold for all of them, not a list.
CONSTANT_SUITES = (
    "assert True",
    "self.assertTrue(True)",
    "self.assertEqual(1, 1)",
    "self.assertNotEqual(1, 2)",
    "self.assertIsNone(None)",
    "self.assertIn(1, [1, 2])",
    "self.assertGreater(1, 0)",
    "assert 1 == 1",
    "assert [] == []",
    "assert not None",
    "assert not []",
    "assert len([]) == 0",
    "assert bool([]) is False",
    "assert True and True",
    "assert 1 + 1 == 2",
)

# Which `Runner` method implements each registry entry. The text-producing
# types share `_validate`; the media and fileset types compute their own sets.
VALIDATOR_FOR_TYPE = {
    "html": "_validate",
    "constraint": "_validate",
    "needle": "_validate",
    "image": "_validate_image",
    "video": "_validate_video",
    "multi-file": "_validate_multi",
}


def assigned_check_names(source: str) -> set[str]:
    """`checks["<name>"] = ...` keys a function body really assigns.

    Parsed rather than substring-matched, so a phantom cannot hide behind a
    comment or a string in the body — which is how the first version of this
    guard was bypassed.
    """
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return set()
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            if not isinstance(target.value, ast.Name) or target.value.id != "checks":
                continue
            key = target.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def unimplemented_names(registry: dict[str, frozenset[str]], shorthands: dict[str, frozenset[str]],
                        body_of) -> list[tuple[str, str]]:
    """Registered names the implementing validator body never assigns.

    A name is implemented when the body assigns `checks["<name>"]`, or when the
    body carries it as a shorthand it expands. The shorthand exemption is
    looked up per type, so a name cannot hide behind another type's shorthand.
    """
    missing: list[tuple[str, str]] = []
    for task_type, names in registry.items():
        source = body_of(task_type)
        assigned = assigned_check_names(source)
        for name in sorted(names):
            if name in shorthands.get(task_type, frozenset()):
                if name not in source:
                    missing.append((task_type, name))
            elif name not in assigned:
                missing.append((task_type, name))
    return missing


def _task(**kwargs) -> TaskSpec:
    base = {"id": "t-1", "type": "html", "prompt": "Write a page about kites."}
    base.update(kwargs)
    return TaskSpec(**base)


# The audit is a whole-suite gate: a rule that never returns takes the gate
# down for every spec, not just the one it was reading. These two run the real
# entry point in a subprocess under a wall clock, because the failure mode is a
# hang or an escaping exception — a plain in-process call would wedge the test
# run instead of reporting.
SUITE_RUNNER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from orchestral.audit import audit_tree
    report = audit_tree(Path(sys.argv[1]))
    print(len(report.findings))
    """
)


def audit_tree_under_clock(directory: Path, seconds: float = 30.0) -> str:
    """`audit_tree` over `directory` in a subprocess, or raise on the clock.

    Returns the child's stdout. Raises `TimeoutExpired` if the audit does not
    finish, and propagates a non-zero exit as a failed assertion.
    """
    result = subprocess.run(
        [sys.executable, "-c", SUITE_RUNNER, str(directory)],
        capture_output=True,
        text=True,
        timeout=seconds,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    if result.returncode != 0:
        raise AssertionError(f"audit_tree exited {result.returncode}\n{result.stderr[-2000:]}")
    return result.stdout


def write_suite_task(directory: Path, assertion: str) -> Path:
    """A `code` spec whose suite's only test body is `assertion`."""
    path = directory / "hostile.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            id: hostile-1
            type: code
            prompt: Implement it.
            validation: [tests_pass]
            metadata:
              difficulty: hard
              module: solution.py
              tests: |
                import unittest

                class T(unittest.TestCase):
                    def test_x(self):
                        {assertion}
            """
        ),
        encoding="utf-8",
    )
    return path


def _rules(spec: TaskSpec) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for finding in audit_spec(spec):
        grouped.setdefault(finding.rule, []).append(finding)
    return grouped


class TestCheckNamesFailClosed(unittest.TestCase):
    """A misspelled check name must not silently drop a gate."""

    def test_unknown_check_name_is_an_error(self):
        found = _rules(_task(validation=["html", "has_requried"]))
        self.assertIn("unknown_validation_check", found)
        finding = found["unknown_validation_check"][0]
        self.assertEqual(finding.severity, ERROR)
        self.assertIn("has_requried", finding.detail)
        self.assertIn("dropped without comment", finding.detail)

    def test_known_check_names_produce_no_error(self):
        found = _rules(_task(validation=["html", "has_title", "has_required"], metadata={"required": ["kite"]}))
        self.assertNotIn("unknown_validation_check", found)

    def test_validation_list_on_a_type_that_ignores_it_is_an_error(self):
        found = _rules(TaskSpec(id="s-1", type="sql", prompt="Summarize revenue.", validation=["has_title"]))
        self.assertIn("ignored_validation_list", found)
        self.assertIn("executed, matches_reference", found["ignored_validation_list"][0].detail)

    def test_runner_fails_closed_on_an_unknown_check_name(self):
        """Regression: `_validate` used to drop the name and pass the run."""
        spec = _task(validation=["html", "has_requried"], metadata={"required": ["kite"]})
        runner = Runner(dry_run=True, runs_dir=tempfile.mkdtemp())
        passes, report = runner._validate(spec, "<html><title>kites</title>kite</html>")
        self.assertFalse(passes)
        self.assertTrue(any("Unknown validation check" in e for e in report["errors"]), report["errors"])
        # the checks that did run are still reported
        self.assertTrue(report["checks"]["html_parses"])

    def test_media_validators_also_fail_closed(self):
        runner = Runner(dry_run=True, runs_dir=tempfile.mkdtemp())
        png = TaskSpec(id="i-1", type="image", prompt="draw", validation=["non_empty", "png_sighnature"])
        passes, report = runner._validate_image(png, PNG_BYTES)
        self.assertFalse(passes)
        self.assertTrue(any("Unknown validation check" in e for e in report["errors"]), report["errors"])

        video = TaskSpec(id="v-1", type="video", prompt="shoot", validation=["non_empty", "mp4_sighnature"])
        passes, report = runner._validate_video(video, MP4_BYTES)
        self.assertFalse(passes)
        self.assertTrue(any("Unknown validation check" in e for e in report["errors"]), report["errors"])

    def test_media_validators_pass_known_names(self):
        runner = Runner(dry_run=True, runs_dir=tempfile.mkdtemp())
        png = TaskSpec(id="i-2", type="image", prompt="draw", validation=["non_empty", "png_signature"])
        passes, _ = runner._validate_image(png, PNG_BYTES)
        self.assertTrue(passes)
        video = TaskSpec(id="v-2", type="video", prompt="shoot", validation=["mp4_signature"])
        passes, _ = runner._validate_video(video, MP4_BYTES)
        self.assertTrue(passes)

    def test_registry_covers_every_task_type_exactly_once(self):
        from orchestral.audit import IGNORES_VALIDATION, VALIDATION_CHECKS

        self.assertEqual(set(VALIDATION_CHECKS) | set(IGNORES_VALIDATION), set(TASK_TYPES))
        self.assertEqual(set(VALIDATION_CHECKS) & set(IGNORES_VALIDATION), set())

    def test_every_registered_name_is_assigned_by_its_validator(self):
        """A registered name no validator assigns is a phantom gate.

        The runner imports this table, so a name added here without a matching
        `checks["<name>"] = ...` in the validator would be accepted by the audit
        and silently never run.
        """
        from orchestral import runner
        from orchestral.audit import VALIDATION_CHECKS, VALIDATION_SHORTHANDS

        self.assertEqual(set(VALIDATION_CHECKS), set(VALIDATOR_FOR_TYPE))
        bodies = {
            task_type: inspect.getsource(getattr(runner.Runner, method))
            for task_type, method in VALIDATOR_FOR_TYPE.items()
        }
        self.assertEqual(unimplemented_names(VALIDATION_CHECKS, VALIDATION_SHORTHANDS, bodies.get), [])

    def test_the_drift_guard_flags_a_registered_name_with_no_assignment(self):
        """Test the guard, not just the registry: a guard that only ever sees a
        clean registry cannot detect its own blind spot."""
        from orchestral.audit import VALIDATION_SHORTHANDS

        bodies = {"image": 'checks["non_empty"] = True\n'}
        phantom = {"image": frozenset({"non_empty", "has_alpha"})}
        self.assertEqual(unimplemented_names(phantom, VALIDATION_SHORTHANDS, bodies.get), [("image", "has_alpha")])

    def test_the_drift_guard_does_not_let_a_name_hide_behind_another_types_shorthand(self):
        from orchestral.audit import VALIDATION_SHORTHANDS

        bodies = {"image": 'checks["non_empty"] = True\n'}
        self.assertNotIn("image", VALIDATION_SHORTHANDS)
        smuggled = {"image": frozenset({"non_empty", "html"})}
        self.assertEqual(unimplemented_names(smuggled, VALIDATION_SHORTHANDS, bodies.get), [("image", "html")])

    def test_the_drift_guard_does_not_accept_a_commented_out_assignment(self):
        """A substring search over the body was satisfied by a comment."""
        bodies = {"image": '    # checks["has_alpha"] = False\n    checks["non_empty"] = True\n'}
        phantom = {"image": frozenset({"non_empty", "has_alpha"})}
        self.assertEqual(unimplemented_names(phantom, {}, bodies.get), [("image", "has_alpha")])


class TestAbsentGradingContract(unittest.TestCase):
    """A self-anchored type with no anchor grades anything as correct."""

    def test_code_without_tests_is_an_error(self):
        found = _rules(TaskSpec(id="c-1", type="code", prompt="Write it.", metadata={"module": "a.py"}))
        self.assertIn("absent_grading_contract", found)
        self.assertEqual(found["absent_grading_contract"][0].severity, ERROR)

    def test_code_suite_that_asserts_nothing_is_an_error(self):
        """A suite of `pass` bodies passes for any artifact, including a stub."""
        spec = TaskSpec(id="c-2", type="code", prompt="Write it.", metadata={"tests": NO_OP_SUITE})
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("assert", found["absent_grading_contract"][0].detail)

    def test_code_suite_with_an_assertion_is_clean_of_that_error(self):
        spec = TaskSpec(
            id="c-3", type="code", prompt="Write it.", metadata={"module": "a.py", "tests": ASSERTING_SUITE}
        )
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_word_assert_in_a_docstring_is_not_an_assertion(self):
        spec = TaskSpec(
            id="c-4",
            type="code",
            prompt="Write it.",
            metadata={"tests": '"""Assert the slug is lowercase."""\n\n\ndef test_x():\n    pass\n'},
        )
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_constant_assertion_is_an_error(self):
        """An assertion that reads no value is provable, so it gates nothing.

        The rule is method-agnostic on purpose. An earlier version folded with
        `ast.literal_eval`, which cannot fold a `Compare` or a `UnaryOp`, and
        handled four of the `assert*` methods unittest provides — so
        `assert 1 == 1` and `assertNotEqual(1, 2)` audited clean while scoring
        1.0 against a stub. The count is deliberately not written down: it
        differs between 3.11 and 3.14.
        """
        for expression in CONSTANT_SUITES:
            with self.subTest(assertion=expression):
                suite = (
                    "import unittest\n\n"
                    "class T(unittest.TestCase):\n"
                    f"    def test_x(self):\n        {expression}\n"
                )
                spec = TaskSpec(id="c-5", type="code", prompt="Write it.", metadata={"tests": suite})
                found = _rules(spec)
                self.assertIn("absent_grading_contract", found)
                self.assertIn("is a constant", found["absent_grading_contract"][0].detail)

    def test_assertion_outside_a_collected_test_is_an_error(self):
        """unittest collects only TestCase methods, so this assertion never runs."""
        for suite in (UNCOLLECTED_SUITE, NOT_A_TEST_CASE, WRONG_NAME_SUITE):
            with self.subTest(suite=suite.splitlines()[0]):
                spec = TaskSpec(id="c-8", type="code", prompt="Write it.", metadata={"tests": suite})
                found = _rules(spec)
                self.assertIn("absent_grading_contract", found)
                self.assertIn("collect", found["absent_grading_contract"][0].detail)

    def test_async_test_on_a_plain_test_case_is_an_error(self):
        """unittest never awaits a coroutine test on a plain TestCase.

        It reports the suite as OK with a RuntimeWarning, so a `test*` that
        must fail still scores 1.0.
        """
        suite = (
            "import unittest\n\nfrom solution import solve\n\n\n"
            "class T(unittest.TestCase):\n"
            "    async def test_x(self):\n"
            "        assert solve('a') == 'WRONG'\n"
        )
        spec = TaskSpec(id="c-12", type="code", prompt="Write it.", metadata={"tests": suite})
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_async_test_on_an_async_test_case_is_accepted(self):
        suite = (
            "import unittest\n\nfrom solution import solve\n\n\n"
            "class T(unittest.IsolatedAsyncioTestCase):\n"
            "    async def test_x(self):\n"
            "        self.assertEqual(await solve('a b'), 'a-b')\n"
        )
        spec = TaskSpec(id="c-13", type="code", prompt="Write it.", metadata={"tests": suite})
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_aliased_test_case_base_is_accepted(self):
        """`from unittest import TestCase as TC` is the common idiom."""
        suite = (
            "from unittest import TestCase as TC\n\nfrom solution import solve\n\n\n"
            "class T(TC):\n"
            "    def test_x(self):\n"
            "        self.assertEqual(solve('a b'), 'a-b')\n"
        )
        spec = TaskSpec(id="c-14", type="code", prompt="Write it.", metadata={"tests": suite})
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_aliased_unittest_module_is_accepted(self):
        suite = (
            "import unittest as ut\n\nfrom solution import solve\n\n\n"
            "class T(ut.TestCase):\n"
            "    def test_x(self):\n"
            "        self.assertEqual(solve('a b'), 'a-b')\n"
        )
        spec = TaskSpec(id="c-15", type="code", prompt="Write it.", metadata={"tests": suite})
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_smoke_assertion_against_a_real_value_is_not_flagged(self):
        """`assert result is not None` is a legitimate smoke check, not a constant."""
        suite = (
            "import unittest\n\nfrom solution import solve\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        result = solve('a b')\n"
            "        assert result is not None\n"
            "        self.assertEqual(solve(''), '')\n"
        )
        spec = TaskSpec(id="c-9", type="code", prompt="Write it.", metadata={"tests": suite})
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_inherited_test_case_base_is_accepted(self):
        suite = (
            "import unittest\n\nfrom solution import solve\n\n\n"
            "class Base(unittest.TestCase):\n    pass\n\n\n"
            "class T(Base):\n"
            "    def test_x(self):\n"
            "        self.assertEqual(solve('a b'), 'a-b')\n"
        )
        spec = TaskSpec(id="c-10", type="code", prompt="Write it.", metadata={"tests": suite})
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_unparseable_suite_is_an_error(self):
        spec = TaskSpec(id="c-11", type="code", prompt="Write it.", metadata={"tests": "def test_x(:\n"})
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_extract_without_fields_or_expected_is_an_error(self):
        """`check_extraction` scores an empty contract as 1.0, so no rule saw it."""
        spec = TaskSpec(id="x-1", type="extract", prompt="Pull the totals from this invoice.", metadata={})
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("fields", found["absent_grading_contract"][0].detail)

    def test_extract_with_only_optional_fields_is_an_error(self):
        """A field that is neither required nor paired with expected grades nothing."""
        spec = TaskSpec(
            id="x-3",
            type="extract",
            prompt="Pull the total.",
            metadata={"fields": {"total": {"type": "number"}}},
        )
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("required", found["absent_grading_contract"][0].detail)

    def test_extract_with_one_required_field_is_clean_of_that_error(self):
        spec = TaskSpec(
            id="x-4",
            type="extract",
            prompt="Pull the total.",
            metadata={"fields": {"total": {"type": "number", "required": True}}},
        )
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_extract_with_expected_only_is_clean_of_that_error(self):
        spec = TaskSpec(id="x-5", type="extract", prompt="Pull the total.", metadata={"expected": {"total": 249.0}})
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_sql_without_reference_sql_is_an_error(self):
        spec = TaskSpec(id="q-1", type="sql", prompt="Total revenue per month.", metadata={"schema": "create table t(a)"})
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("reference_sql", found["absent_grading_contract"][0].detail)

    def test_api_without_calls_is_an_error(self):
        spec = TaskSpec(id="p-1", type="api", prompt="Look up the order.", metadata={"stub": []})
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("calls", found["absent_grading_contract"][0].detail)

    def test_text_types_have_no_metadata_contract_to_declare(self):
        for task_type in ("html", "constraint", "needle", "image", "video", "multi-file"):
            with self.subTest(type=task_type):
                self.assertNotIn("absent_grading_contract", _rules(_task(id="t-x", type=task_type)))


class TestAuditAlwaysAnswers(unittest.TestCase):
    """The gate must return a verdict for every spec it is handed.

    These are availability regressions, not accuracy ones. An over-eager rule
    costs one spec author an edit; a rule that never returns, or raises, takes
    down `harness.py audit --strict` for all 124 specs.
    """

    def test_pow_chain_does_not_wedge_the_audit(self):
        """`2**2**2**2**2**2` doubles in bit-length per level, so eager folding
        never terminates. `ast.literal_eval` refused it instantly, so this
        capability came in with the constant folder.
        """
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_suite_task(directory, "assert " + "2**" * 6 + "2 == 1")
            self.assertTrue(audit_tree_under_clock(directory).strip())

    def test_repeated_multiply_does_not_wedge_the_audit(self):
        """The same unbounded-work shape through `*` and a growing string."""
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_suite_task(directory, "assert 'ab' * 10**9 * 10**9 == 'x'")
            self.assertTrue(audit_tree_under_clock(directory).strip())

    def test_deep_constant_expression_does_not_raise(self):
        """A flat unary chain is a legal parse at any depth, and recursing the
        folder over it overflows the stack. `RecursionError` escaping
        `audit_tree` crashes the gate.
        """
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_suite_task(directory, "assert " + "not " * 2000 + "None")
            self.assertTrue(audit_tree_under_clock(directory).strip())

    def test_a_suite_the_parser_refuses_is_an_error_not_a_crash(self):
        """The unreadable-suite path, in-process, so the verdict is asserted.

        The construction is an unbalanced bracket, which every supported
        interpreter refuses. The previous version of this test used a 3000-deep
        `not` chain and assumed the parser's nesting limit is the same
        everywhere; CPython raised that limit in 3.12, so the suite parsed, the
        rule stayed silent for the documented cap reason, and the test failed on
        3 of the 4 versions `requires-python = ">=3.11"` claims to support. A
        test whose premise is an implementation detail of one release is a test
        that lies on the other three.
        """
        spec = TaskSpec(
            id="c-rec",
            type="code",
            prompt="Write it.",
            metadata={"tests": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                               "    def test_x(self):\n        assert " + "(" * 3000 + "None\n"},
        )
        found = _rules(spec)  # must not raise
        self.assertIn("absent_grading_contract", found)
        self.assertIn("does not parse", found["absent_grading_contract"][0].detail)

    def test_a_deeply_nested_suite_is_answered_on_every_interpreter(self):
        """Whatever the parser does with this shape, the gate answers.

        3.11 refuses to parse it and the suite is reported unreadable; 3.12 and
        later parse it, folding stops at the depth cap, and the assertion is
        treated as possibly reading the artifact. Both are acceptable outcomes.
        The one that is not acceptable is a raise, so that is the assertion — and
        it holds on every version instead of one.
        """
        source = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                  "    def test_x(self):\n        assert " + "not " * 3000 + "None\n")
        reason = orchestral_audit._suite_gate_reason(source)  # must not raise
        self.assertIn(
            reason,
            (
                None,  # 3.12+: the depth cap treats it as possibly reading the artifact
                _UNREADABLE_SUITE,  # 3.11: the parser's nesting limit
                "metadata.tests does not parse, so it cannot be a suite the runner can import.",
                _CONSTANT_ASSERTIONS_NEUTRAL,
                _CONSTANT_ASSERTIONS_PASSES,
                _CONSTANT_ASSERTIONS_FAILS,
            ),
        )

    def test_one_hostile_suite_does_not_cost_the_tree_its_verdict(self):
        """A single unreadable spec must not cost the others their findings."""
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_suite_task(directory, "assert " + "2**" * 6 + "2 == 1")
            (directory / "kite.yaml").write_text(
                textwrap.dedent(
                    """\
                    id: kite-1
                    type: html
                    prompt: Write a page about kites.
                    validation: [html_parses, non_empty]
                    metadata:
                      difficulty: hard
                    """
                ),
                encoding="utf-8",
            )
            printed = audit_tree_under_clock(directory).strip()
            self.assertGreater(int(printed), 0, "the tree must still report findings")


class TestAnchorNeedsBothTheCheckAndTheDeclaration(unittest.TestCase):
    """`metadata.required` only means something if the grader is asked to read it.

    Requesting `has_required` without declaring `required` is a misconfiguration
    the runner itself treats as an error, so the audit must not read it as
    anchored. The earlier type-keyed exemption had the mirror-image hole.
    """

    def test_requested_anchor_without_a_declaration_does_not_clear_the_finding(self):
        for validation, metadata in (
            (["html", "has_required"], {}),
            (["html", "has_required"], {"required": []}),
            (["html", "matches_pattern"], {}),
            (["html", "matches_pattern"], {"pattern": ""}),
        ):
            with self.subTest(validation=validation, metadata=metadata):
                found = _rules(_task(validation=validation, metadata=metadata))
                self.assertIn("structural_only", found)

    def test_requested_anchor_with_a_declaration_clears_the_finding(self):
        """Both halves together pin the mechanism. Deleting the `metadata` key
        from this case must fail the previous test, which is what a single-sided
        test cannot do.
        """
        self.assertNotIn(
            "structural_only", _rules(_task(validation=["html", "has_required"], metadata={"required": ["kite"]}))
        )
        self.assertNotIn(
            "structural_only", _rules(_task(validation=["html", "matches_pattern"], metadata={"pattern": "kite"}))
        )

    def test_degenerate_declaration_is_not_an_anchor(self):
        """`required: [""]` is satisfied by every artifact, so it anchors nothing.

        `bool([""])` is True and `"" in text` is True for any string, so this
        silences the finding while proving nothing.

        `pattern: "."` is a match-everything regex and is *not* caught here.
        Deciding how strong a regex has to be is a separate analysis from
        whether one was declared, and it is disclosed in docs/task-audit.md
        rather than half-done in code.
        """
        for metadata in ({"required": [""]}, {"required": ""}, {"required": []}, {"required": ["  ", ""]}):
            with self.subTest(metadata=metadata):
                self.assertIn(
                    "structural_only", _rules(_task(validation=["html", "has_required"], metadata=metadata))
                )

    def test_advice_names_the_missing_declaration(self):
        found = _rules(_task(validation=["html", "has_required"]))
        detail = found["structural_only"][0].detail
        self.assertIn("has_required", detail)
        self.assertIn("metadata.required", detail)


class TestAssertionMessagesAreTrue(unittest.TestCase):
    """A rule that reports a reason has to be right about the reason.

    Both earlier versions collapsed distinct diagnoses into one message, and
    the message claimed a property ("none of them can fail") that is false for
    an always-failing assertion.
    """

    def suite(self, body: str) -> TaskSpec:
        return TaskSpec(
            id="c-msg",
            type="code",
            prompt="Write it.",
            metadata={"tests": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                               f"    def test_x(self):\n        {body}\n"},
        )

    def test_no_visible_assertion_and_constant_assertion_report_differently(self):
        """`pass` and `assert 1 == 1` are different defects."""
        no_assertion = _rules(self.suite("pass"))["absent_grading_contract"][0].detail
        constant = _rules(self.suite("assert 1 == 1"))["absent_grading_contract"][0].detail
        self.assertNotEqual(no_assertion, constant)
        self.assertIn("no assertion", no_assertion)
        self.assertIn("constant", constant)

    def test_keyword_only_assertion_is_not_mistaken_for_an_empty_one(self):
        """`node.args` is empty whenever every argument is a keyword, so the
        zero-argument branch was catching any keyword-only assert call.
        """
        spec = self.suite("self.assertEqual(first=solve('a b'), second='a-b')")
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_zero_argument_assertion_still_reads_nothing(self):
        """The branch is real, just narrower: a bare `self.assertTrue()`."""
        self.assertIn("absent_grading_contract", _rules(self.suite("self.assertTrue()")))

    def test_negated_provably_false_comparison_is_a_constant(self):
        """`assert not (1 == 2)` is a tautology. The inner `Compare` reported
        "not constant" for a provably-false branch and `not` inherited it.
        """
        for expression in ("assert not (1 == 2)", "assert not (1 > 2)", "assert not (1 in (2,))"):
            with self.subTest(assertion=expression):
                self.assertIn("absent_grading_contract", _rules(self.suite(expression)))

    def test_short_circuiting_boolop_folds_to_a_constant(self):
        for expression in ("assert False and solve(1)", "assert True or solve(1)"):
            with self.subTest(assertion=expression):
                self.assertIn("absent_grading_contract", _rules(self.suite(expression)))

    def test_alternating_comparison_still_reads_the_artifact(self):
        """The `Compare` fix must not turn `1 < len(out) <= 3` into a constant."""
        self.assertNotIn("absent_grading_contract", _rules(self.suite("assert 1 < len(out) <= 3")))

    def test_always_failing_constant_is_reported_as_a_constant(self):
        """`assert 1 == 2` is a constant too. The message must not claim it
        cannot fail, because it always does.
        """
        detail = _rules(self.suite("assert 1 == 2"))["absent_grading_contract"][0].detail
        self.assertIn("constant", detail)
        self.assertNotIn("cannot fail", detail)

    def test_assertion_the_audit_cannot_see_is_not_reported_as_a_tautology(self):
        """An assertion assembled at runtime discriminates — verified by running
        it against a wrong solution. Calling it a constant is false in both
        halves, so the rule stays silent rather than guessing.
        """
        spec = TaskSpec(
            id="c-exec",
            type="code",
            prompt="Write it.",
            metadata={"tests": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                               "    def test_x(self):\n"
                               "        exec('assert solve(\"a b\") == \"a-b\"')\n"},
        )
        self.assertNotIn("absent_grading_contract", _rules(spec))


class TestUnittestBaseIdioms(unittest.TestCase):
    """`from unittest import case` is a documented idiom, and a suite that uses
    it does discriminate. Reporting it as "passes for any artifact" was a false
    positive on an error-severity rule.
    """

    def suite(self, imports: str, base: str) -> TaskSpec:
        return TaskSpec(
            id="c-base",
            type="code",
            prompt="Write it.",
            metadata={"tests": f"{imports}\n\nfrom solution import solve\n\n\n"
                               f"class T({base}):\n"
                               "    def test_x(self):\n"
                               "        self.assertEqual(solve('a b'), 'a-b')\n"},
        )

    def test_documented_from_import_idioms_are_accepted(self):
        for imports, base in (
            ("from unittest import case", "case.TestCase"),
            ("from unittest import case as u", "u.TestCase"),
            ("from unittest import async_case", "async_case.IsolatedAsyncioTestCase"),
            ("import unittest.case", "unittest.case.TestCase"),
        ):
            with self.subTest(imports=imports, base=base):
                self.assertNotIn("absent_grading_contract", _rules(self.suite(imports, base)))

    def test_a_local_class_named_case_is_not_unittest(self):
        """The fix must not resolve a same-named local class to unittest."""
        spec = TaskSpec(
            id="c-base-local",
            type="code",
            prompt="Write it.",
            metadata={"tests": "import unittest\n\nfrom solution import solve\n\n\n"
                               "class case:\n    pass\n\n\n"
                               "class T(case):\n"
                               "    def test_x(self):\n"
                               "        self.assertEqual(solve('a b'), 'a-b')\n"},
        )
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_an_unrelated_package_is_not_unittest(self):
        spec = TaskSpec(
            id="c-base-other",
            type="code",
            prompt="Write it.",
            metadata={"tests": "import notunittest as case\n\nfrom solution import solve\n\n\n"
                               "class T(case.TestCase):\n"
                               "    def test_x(self):\n"
                               "        self.assertEqual(solve('a b'), 'a-b')\n"},
        )
        self.assertIn("absent_grading_contract", _rules(spec))


class TestGamingSurface(unittest.TestCase):
    def test_shape_only_html_spec_is_flagged(self):
        found = _rules(_task(validation=["html"]))
        self.assertIn("structural_only", found)
        self.assertEqual(found["structural_only"][0].severity, WARN)
        self.assertIn("has_required", found["structural_only"][0].detail)

    def test_topic_anchor_clears_structural_only(self):
        spec = _task(validation=["html", "has_required"], metadata={"required": ["kite"]})
        self.assertNotIn("structural_only", _rules(spec))

    def test_pattern_anchor_clears_structural_only(self):
        spec = _task(validation=["html_parses", "matches_pattern"], metadata={"pattern": r"kite"})
        self.assertNotIn("structural_only", _rules(spec))

    def test_title_check_alone_does_not_anchor_the_topic(self):
        """`has_title` proves an element exists, not that the page is on-topic."""
        found = _rules(_task(validation=["html_parses", "has_title"]))
        self.assertIn("structural_only", found)

    def test_prompt_quoting_the_expected_value_is_flagged(self):
        spec = _task(prompt="The deploy token is FALCON-4417. Report the token.", metadata={"expected_answer": "FALCON-4417"})
        found = _rules(spec)
        self.assertIn("prompt_states_the_answer", found)
        self.assertIn("FALCON-4417", found["prompt_states_the_answer"][0].detail)

    def test_api_spec_whose_prompt_lists_the_expected_calls_is_flagged(self):
        spec = TaskSpec(
            id="api-1",
            type="api",
            prompt="Call GET /users/42, then POST /orders, then GET /orders/9, then GET /orders/9/items.",
            metadata={"calls": [{"method": "GET", "path": "/users/42"}, {"method": "POST", "path": "/orders"}]},
        )
        found = _rules(spec)
        self.assertIn("prompt_states_the_answer", found)
        self.assertIn("POST /orders", found["prompt_states_the_answer"][0].detail)

    def test_inflected_prose_form_is_matched(self):
        """Prose writes "GETs /health", not "GET /health"."""
        spec = TaskSpec(
            id="api-2",
            type="api",
            prompt="1. GETs /health to confirm. 2. GETs /users/42 to look up the customer.",
            metadata={"calls": [{"method": "GET", "path": "/health"}, {"method": "GET", "path": "/users/42"}]},
        )
        found = _rules(spec)
        self.assertIn("prompt_states_the_answer", found)
        self.assertIn("GET /health", found["prompt_states_the_answer"][0].detail)

    def test_extract_is_exempt_from_the_prompt_leak_rule(self):
        """By design the extract prompt carries the source document."""
        spec = TaskSpec(
            id="x-1",
            type="extract",
            prompt="Invoice INV-2025-0417 total 249.00. Return invoice_id and total.",
            metadata={"fields": {"invoice_id": {"type": "str"}}, "expected": {"invoice_id": "INV-2025-0417"}},
        )
        self.assertNotIn("prompt_states_the_answer", _rules(spec))

    def test_extract_whose_answer_is_in_the_prompt_is_flagged_as_derivable(self):
        spec = TaskSpec(
            id="x-2",
            type="extract",
            prompt="Invoice INV-2025-0417 total 249.00. Return invoice_id and total.",
            metadata={"fields": {"invoice_id": {"type": "str"}}, "expected": {"invoice_id": "INV-2025-0417"}},
        )
        found = _rules(spec)
        self.assertIn("answer_derivable_from_prompt", found)
        self.assertIn("transcription", found["answer_derivable_from_prompt"][0].detail)

    def test_textbook_problem_is_flagged(self):
        found = _rules(_task(id="code-fizzbuzz", type="code", prompt="Write fizzbuzz for 1..100.", metadata={"tests": ASSERTING_SUITE}))
        self.assertIn("memorization_risk", found)
        self.assertIn("fizzbuzz", found["memorization_risk"][0].detail)

    def test_novel_problem_is_not_flagged_for_memorization(self):
        found = _rules(_task(prompt="Reconcile this ledger's partial refunds against its settlement batch."))
        self.assertNotIn("memorization_risk", found)

    def test_multi_file_with_only_path_checks_is_flagged(self):
        spec = TaskSpec(
            id="mf-1",
            type="multi-file",
            prompt="Build a two-page microsite.",
            validation=["has_paths"],
            metadata={"expected_paths": ["index.html", "style.css"]},
        )
        found = _rules(spec)
        self.assertIn("unanchored_fileset", found)
        self.assertIn("no check reads the file bodies", found["unanchored_fileset"][0].detail)

    def test_multi_file_without_expected_paths_is_flagged(self):
        """The guard used to require `has_paths`, so the unanchored case never fired."""
        spec = TaskSpec(id="mf-2", type="multi-file", prompt="Build a site.", validation=["non_empty", "zip_signature"])
        found = _rules(spec)
        self.assertIn("unanchored_fileset", found)
        self.assertIn("the fileset is unanchored", found["unanchored_fileset"][0].detail)

    def test_unusable_expected_paths_counts_as_unanchored(self):
        """`expected_paths()` drops a non-list, so the grader looks for nothing."""
        spec = TaskSpec(
            id="mf-4",
            type="multi-file",
            prompt="Build a site.",
            validation=["has_paths"],
            metadata={"expected_paths": "index.html"},
        )
        found = _rules(spec)
        self.assertIn("unanchored_fileset", found)
        self.assertIn("the fileset is unanchored", found["unanchored_fileset"][0].detail)

    def test_declared_paths_without_has_paths_are_flagged(self):
        spec = TaskSpec(
            id="mf-3",
            type="multi-file",
            prompt="Build a site.",
            validation=["non_empty", "zip_signature"],
            metadata={"expected_paths": ["index.html"]},
        )
        self.assertIn("unanchored_fileset", _rules(spec))

    def test_structural_only_advice_is_type_aware(self):
        """`has_required` is a text check; an image author cannot use it."""
        spec = _task(id="i-1", type="image", prompt="Draw a coffee hero.", validation=["non_empty", "png_signature"])
        detail = _rules(spec)["structural_only"][0].detail
        self.assertNotIn("has_required", detail)
        self.assertIn("image", detail)

    def test_structural_only_advice_still_names_the_text_checks(self):
        detail = _rules(_task(validation=["html"]))["structural_only"][0].detail
        self.assertIn("has_required", detail)
        self.assertIn("matches_pattern", detail)

    def test_missing_difficulty_is_info_not_a_warning(self):
        found = _rules(_task())
        self.assertEqual(found["unlabeled_difficulty"][0].severity, INFO)

    def test_declared_difficulty_clears_the_info(self):
        self.assertNotIn("unlabeled_difficulty", _rules(_task(metadata={"difficulty": "hard"})))

    def test_metadata_required_clears_the_finding_when_the_grader_will_read_it(self):
        """`has_required` is the only thing that makes the declaration mean anything."""
        spec = _task(validation=["html", "has_required"], metadata={"required": ["kite"]})
        self.assertNotIn("structural_only", _rules(spec))

    def test_metadata_required_does_not_clear_the_finding_when_has_required_is_not_requested(self):
        """The only read of metadata.required is inside `if "has_required" in requested`.

        Keying the exemption on the task *type` let a two-word YAML edit silence
        the finding while the grader read nothing. That is the hole this test
        exists to stop returning. It silenced **zero** shipped findings, not
        the hundred a comment here once claimed: no shipped spec declares
        `metadata.required` at all, so the count was the flagged-`html` total
        restated as if it were the number of affected specs.
        """
        for validation in (["html"], ["html_parses", "non_empty"], None):
            with self.subTest(validation=validation):
                spec = _task(validation=validation, metadata={"required": ["kite"]})
                found = _rules(spec)
                self.assertIn("structural_only", found)
                self.assertIn("metadata.required is declared", found["structural_only"][0].detail)
                self.assertIn("anchors nothing here", found["structural_only"][0].detail)

    def test_metadata_required_does_not_clear_the_finding_for_an_image(self):
        """`_validate_image` never reads metadata.required, so declaring it proves nothing."""
        spec = _task(
            id="i-3",
            type="image",
            prompt="Draw a latte on a wooden table.",
            validation=["non_empty", "png_signature"],
            metadata={"required": ["latte", "wooden table"]},
        )
        found = _rules(spec)
        self.assertIn("structural_only", found)
        self.assertIn("metadata.required is declared", found["structural_only"][0].detail)
        self.assertIn("anchors nothing here", found["structural_only"][0].detail)


class TestSuiteLevel(unittest.TestCase):
    def _family(self, count: int) -> list[TaskSpec]:
        return [
            TaskSpec(
                id=f"html-batch-{i:03d}",
                type="html",
                prompt=f"Create a landing page for a widget {i} targeted at buyers. Write a headline and a call to action.",
                validation=["html"],
            )
            for i in range(count)
        ]

    def test_near_duplicate_family_is_reported_once(self):
        families = find_duplicate_families(self._family(12), min_family=5)
        self.assertEqual(len(families), 1)
        self.assertEqual(families[0].rule, "near_duplicate_family")
        self.assertIn("12 specs", families[0].detail)

    def test_small_cluster_is_not_a_family(self):
        self.assertEqual(find_duplicate_families(self._family(3), min_family=5), [])

    def test_diverse_suite_has_no_family(self):
        specs = [
            TaskSpec(id="a", type="html", prompt="Landing page for a coffee roaster aimed at cafes."),
            TaskSpec(id="b", type="sql", prompt="Total revenue per month using a window function over orders."),
            TaskSpec(id="c", type="code", prompt="Implement an LRU cache with get and put operations."),
        ]
        self.assertEqual(find_duplicate_families(specs, min_family=2), [])

    def test_absent_holdout_arm_is_reported(self):
        report = audit_suite([_task()])
        self.assertIn("no_holdout_arm", report.by_rule())

    def test_a_holdout_spec_silences_the_holdout_finding(self):
        report = audit_suite([_task(), _task(id="h-1", metadata={"holdout": True})])
        self.assertNotIn("no_holdout_arm", report.by_rule())

    def test_warnings_alone_keep_the_report_ok(self):
        report = audit_suite([_task(validation=["html"])])
        self.assertTrue(report.errors == [])
        self.assertTrue(report.warnings)
        self.assertTrue(report.ok)

    def test_errors_make_the_report_not_ok(self):
        report = audit_suite([_task(validation=["has_title", "nonsense_check"])])
        self.assertFalse(report.ok)
        self.assertEqual(report.counts()["error"], 1)


class TestRendering(unittest.TestCase):
    def test_text_output_mentions_every_severity_present(self):
        report = audit_suite([_task(validation=["html", "nonsense_check"]), _task(id="t-2", metadata={"difficulty": "easy"})])
        text = report.to_text()
        self.assertIn("ERROR (1)", text)
        self.assertIn("WARN (", text)
        self.assertIn("INFO (", text)
        self.assertIn("unknown_validation_check", text)

    def test_clean_suite_says_so(self):
        text = audit_suite([], []).to_text()
        self.assertIn("No gaming surface found", text)

    def test_dict_output_is_json_serialisable_and_grouped(self):
        import json

        payload = audit_suite([_task(validation=["html", "nope"])]).to_dict()
        json.dumps(payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["specs"], 1)
        self.assertIn("unknown_validation_check", payload["rules"])

    def test_effective_checks_expands_the_html_shorthand(self):
        self.assertEqual(effective_checks(_task(validation=["html"])), {"html_parses", "non_empty"})
        self.assertEqual(effective_checks(_task()), {"html_parses", "non_empty", "has_title"})


class TestShippedSuite(unittest.TestCase):
    """The registry must describe the suite we actually ship."""

    def test_shipped_specs_have_no_unknown_check_names(self):
        report = audit_tree(REPO_TASKS)
        unknown = report.by_rule().get("unknown_validation_check", [])
        self.assertEqual(unknown, [], [f.detail for f in unknown])

    def test_shipped_specs_have_no_ignored_validation_lists(self):
        report = audit_tree(REPO_TASKS)
        ignored = report.by_rule().get("ignored_validation_list", [])
        self.assertEqual(ignored, [], [f.detail for f in ignored])

    def test_shipped_code_specs_all_carry_a_real_test_suite(self):
        report = audit_tree(REPO_TASKS)
        self.assertEqual(report.by_rule().get("absent_grading_contract", []), [])

    def test_shipped_suite_loads_and_has_specs(self):
        report = audit_tree(REPO_TASKS)
        self.assertGreater(report.specs, 50)


if __name__ == "__main__":
    unittest.main()


class TestAvailabilityF1(TestCase):
    """F1: a long chain of local base classes is a legal parse at any length.

    Tree depth is bounded by the parser; the length of a *base-class chain* is
    not, because each class is a separate top-level statement. Resolving that
    chain by recursion therefore overflowed the stack and took the gate's answer
    for every other spec with it.
    """

    @staticmethod
    def _chained(count: int) -> str:
        lines = ["import unittest", ""]
        for index in range(count):
            base = f"C{index - 1}" if index else "object"
            lines.append(f"class C{index}({base}): pass")
        lines += ["", "class Suite(unittest.TestCase):", "    def test_x(self): pass"]
        return "\n".join(lines) + "\n"

    def test_a_thousand_chained_classes_still_answer(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_suite_task(directory, "self.assertEqual(1, 1)")
            (directory / "chain.yaml").write_text(
                textwrap.dedent(
                    """\
                    id: chain-1
                    type: code
                    prompt: Implement it.
                    validation: [tests_pass]
                    metadata:
                      difficulty: hard
                      module: solution.py
                      tests: |
                    """
                )
                + textwrap.indent(self._chained(1000), "    "),
                encoding="utf-8",
            )
            # the subprocess prints a finding count; the point is that it
            # printed one at all instead of tracebacking
            self.assertGreater(int(audit_tree_under_clock(directory, 60.0).strip()), 0)

    def test_the_chained_classes_are_still_audited(self):
        """The verdict, in-process, now that resolution no longer overflows."""
        self.assertEqual(
            orchestral_audit._suite_gate_reason(self._chained(1000)),
            _NO_COLLECTABLE_ASSERTION,
        )

    def test_a_chain_ending_in_testcase_still_resolves(self):
        """Bounding the recursion by refusing to answer would be a false
        positive on a real gate. A long chain that *does* end at a TestCase has
        to keep resolving, so the fix is a worklist rather than a depth cap.
        """
        lines = ["import unittest", "", "class Root(unittest.TestCase): pass", ""]
        for index in range(1000):
            base = f"C{index - 1}" if index else "Root"
            lines.append(f"class C{index}({base}): pass")
        source = "\n".join(lines)
        tree = ast.parse(source)
        imports, bound = orchestral_audit._unittest_names(tree)
        self.assertEqual(orchestral_audit._is_test_case(
            tree.body[-1],
            {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)},
            imports, bound,
        ), "sync")

    def test_a_cyclic_base_list_does_not_hang(self):
        source = "class A(B): pass\nclass B(A): pass\n"
        tree = ast.parse(source)
        locals_ = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        self.assertEqual(orchestral_audit._is_test_case(tree.body[0], locals_, {}, set()), "")


class TestAnchorDeclarationShape(TestCase):
    """F4 and F6: the declaration's *shape* decides what the runner compares.

    `runner.py` writes `for t in required`, so a mapping contributes its keys, a
    list its items, and a bare string its *characters*. The audit has to model
    that, or a declaration that anchors nothing clears the finding.
    """

    def _html(self, required: object) -> TaskSpec:
        return TaskSpec(
            id="h-decl",
            type="html",
            prompt="Create a landing page. Output a single self-contained HTML file.",
            validation=["html", "has_required"],
            metadata={"required": required},
        )

    def test_a_bare_string_is_iterated_per_character_so_it_anchors_nothing(self):
        for value in ("kite", "ab", "the"):
            with self.subTest(required=value):
                self.assertIn("structural_only", _rules(self._html(value)))

    def test_a_list_of_words_anchors(self):
        self.assertNotIn("structural_only", _rules(self._html(["kite", "surfboard"])))

    def test_a_single_character_token_anchors_nothing(self):
        for value in ([""], ["  "], [0], [1], ["a"]):
            with self.subTest(required=value):
                self.assertIn("structural_only", _rules(self._html(value)))

    def test_a_mapping_contributes_its_keys_so_it_anchors(self):
        """F6: the runner iterates a mapping, so `{"kite": true}` *is* enforced.
        Reporting it as unanchored is factually wrong.
        """
        self.assertNotIn("structural_only", _rules(self._html({"kite": True})))

    def test_the_advice_does_not_tell_the_author_the_declaration_is_missing(self):
        """F6's second half: the advice claimed the matching declaration was
        "missing or empty" while the author had written one. Here the finding is
        correct — a one-character token anchors nothing — so the advice has to
        describe what is actually wrong with the declaration.
        """
        detail = _rules(self._html(["a"]))["structural_only"][0].detail
        self.assertNotIn("missing or empty", detail)
        self.assertIn("character by character", detail)
        self.assertIn("Declare a list of words", detail)

    def test_the_token_model_matches_the_runner_on_every_shape(self):
        """The audit's notion of a declared token is the runner's, by
        construction rather than by coincidence.
        """
        for value in (["kite"], ("kite", "x"), {"kite": 1}, "kite", ["a", "kite"]):
            with self.subTest(required=value):
                runner_tokens = [str(t).lower() for t in (value or [])]
                model = [t.lower() for t in orchestral_audit._required_tokens(value)]
                self.assertEqual(model, [t for t in runner_tokens if len(t) > 1])


class TestSelfAnchoredTypesAreNotKeyedOnTheTypeName(TestCase):
    """F7: `constraint` and `needle` are labels in the runner, not graders.

    Both expand through `VALIDATION_SHORTHANDS` to the same plain `html` checks
    every other type gets, so a `validation: [html]` `constraint` spec has no
    topical anchor. Keying the early return on the type name is the same hole
    this issue closed for `metadata.required`.
    """

    def test_a_constraint_spec_with_only_html_checks_is_flagged(self):
        spec = TaskSpec(
            id="c-only",
            type="constraint",
            prompt="Write a blurb.",
            validation=["html"],
            metadata={},
        )
        self.assertIn("structural_only", _rules(spec))

    def test_a_needle_spec_with_only_html_checks_is_flagged(self):
        spec = TaskSpec(
            id="n-only",
            type="needle",
            prompt="Find the token.",
            validation=["html"],
            metadata={},
        )
        self.assertIn("structural_only", _rules(spec))

    def test_a_constraint_spec_with_a_real_anchor_is_not_flagged(self):
        spec = TaskSpec(
            id="c-anchored",
            type="constraint",
            prompt="Write a blurb.",
            validation=["html", "has_required"],
            metadata={"required": ["kite", "surfboard"]},
        )
        self.assertNotIn("structural_only", _rules(spec))

    def test_the_still_genuinely_self_anchored_types_stay_silent(self):
        for task_type in ("code", "sql", "extract", "api", "multi-file"):
            with self.subTest(type=task_type):
                self.assertNotIn(
                    "structural_only",
                    _rules(TaskSpec(id="s-1", type=task_type, prompt="Do it.", validation=["html"])),
                )


class TestConstantMessageIsTrueForBothPolarities(TestCase):
    """F8: a constant can fold to True or to False.

    `assert 1 == 2` folds to False and fails for *every* artifact, so a message
    saying it "cannot gate one" tells the author their suite is harmless when it
    is guaranteed to score 0. The old guard asserted the literal
    `assertNotIn("cannot fail")`, which the live wording passed by accident.
    """

    @staticmethod
    def _detail(assertion: str) -> str:
        spec = TaskSpec(
            id="c-msg",
            type="code",
            prompt="Write it.",
            validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": (
                "import unittest\n\nfrom solution import solve\n\n\n"
                "class T(unittest.TestCase):\n    def test_x(self):\n        "
                + assertion + "\n"
            )},
        )
        return _rules(spec)["absent_grading_contract"][0].detail

    def test_the_message_never_claims_the_suite_is_harmless(self):
        for assertion in ("assert 1 == 2", "assert 1 < 0", "self.assertEqual(1, 2)"):
            with self.subTest(assertion=assertion):
                detail = self._detail(assertion)
                for phrase in ("cannot fail", "cannot gate", "passes for any"):
                    self.assertNotIn(phrase, detail)

    def test_an_always_failing_constant_says_so(self):
        detail = self._detail("assert 1 == 2")
        self.assertIn("fails for every artifact", detail)

    def test_an_always_passing_constant_says_so(self):
        self.assertIn("passes for any artifact", self._detail("assert 1 == 1"))

    def test_an_assert_call_states_neither_polarity(self):
        """Whether `self.assertEqual(1, 2)` passes or fails follows from the
        method, and the check is method-agnostic on purpose. So its message
        claims only what is true either way: the outcome does not depend on the
        artifact.
        """
        detail = self._detail("self.assertEqual(1, 2)")
        self.assertIn("does not depend on what the solution produced", detail)

    def test_the_guard_would_notice_the_old_wording_coming_back(self):
        """The defect the old guard missed: a reword that keeps the same false
        claim. This pins the exact sentence, so no rewording passes silently.
        """
        self.assertIn("fails for every artifact", self._detail("assert 1 == 2"))
        for wording in (
            _CONSTANT_ASSERTIONS_FAILS, _CONSTANT_ASSERTIONS_PASSES, _CONSTANT_ASSERTIONS_NEUTRAL,
        ):
            self.assertNotIn("cannot gate", wording)
            self.assertNotIn("cannot fail", wording)


class TestEffectFreeIsComplete(TestCase):
    """F9: `global x` and a bare annotation also do nothing."""

    def test_a_global_statement_alone_is_inert(self):
        spec = TaskSpec(
            id="c-g",
            type="code",
            prompt="Write it.",
            validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": (
                "import unittest\n\n\nclass T(unittest.TestCase):\n"
                "    def test_x(self):\n        global x\n"
            )},
        )
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_a_bare_annotation_alone_is_inert(self):
        spec = TaskSpec(
            id="c-a",
            type="code",
            prompt="Write it.",
            validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": (
                "import unittest\n\n\nclass T(unittest.TestCase):\n"
                "    def test_x(self):\n        x: int\n"
            )},
        )
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_a_body_that_does_something_is_not_inert(self):
        spec = TaskSpec(
            id="c-d",
            type="code",
            prompt="Write it.",
            validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": (
                "import unittest\n\nfrom solution import solve\n\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_x(self):\n        self.assertEqual(solve('a'), 'a')\n"
            )},
        )
        self.assertNotIn("absent_grading_contract", _rules(spec))


class TestUnittestResolverIsNotOverWide(TestCase):
    """F10: a name bound by a non-unittest import is not `unittest`."""

    @staticmethod
    def _resolves(source: str) -> bool:
        tree = ast.parse(source)
        imports, bound = orchestral_audit._unittest_names(tree)
        return bool(orchestral_audit._is_test_case(
            tree.body[-1],
            {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)},
            imports, bound,
        ))

    def test_a_shadowed_unittest_import_does_not_resolve(self):
        self.assertFalse(self._resolves(
            "from mypkg import unittest\n\nclass T(unittest.TestCase): pass\n"))

    def test_the_documented_idioms_still_resolve(self):
        for source in (
            "import unittest\n\nclass T(unittest.TestCase): pass\n",
            "import unittest as ut\n\nclass T(ut.TestCase): pass\n",
            "from unittest import TestCase\n\nclass T(TestCase): pass\n",
            "from unittest import TestCase as TC\n\nclass T(TC): pass\n",
            "from unittest import case\n\nclass T(case.TestCase): pass\n",
            "import unittest.case\n\nclass T(unittest.case.TestCase): pass\n",
        ):
            with self.subTest(source=source.splitlines()[0]):
                self.assertTrue(self._resolves(source))


class TestPromptCallPatternIsNotABacktrackingBomb(TestCase):
    """F2: `method` is spec-controlled and was interpolated raw into a regex.

    Pre-existing since the original audit commit, and reported because it
    falsifies the same "the gate always answers" property.
    """

    def test_a_nested_quantifier_method_returns_promptly(self):
        spec = TaskSpec(
            id="a-1",
            type="api",
            prompt="A" * 30 + " build the widget page please.",
            metadata={"calls": [{"method": "(A+)+B", "path": "/x"}]},
        )
        start = time.monotonic()
        audit_spec(spec)
        self.assertLess(time.monotonic() - start, 5.0)

    def test_the_method_is_matched_literally(self):
        spec = TaskSpec(
            id="a-2",
            type="api",
            prompt="Call GET /x to fetch the widget.",
            metadata={"calls": [{"method": "G.T", "path": "/x"}]},
        )
        self.assertNotIn("prompt_states_the_answer", _rules(spec))
        self.assertIn("prompt_states_the_answer", _rules(TaskSpec(
            id="a-3", type="api", prompt="Call GET /x to fetch the widget.",
            metadata={"calls": [{"method": "GET", "path": "/x"}]})))


DOC = Path(__file__).resolve().parent.parent / "docs" / "task-audit.md"


def _doc_rules() -> list[tuple[str, str]]:
    """`(rule, severity)` for every row of the rules table in the doc."""
    rows: list[tuple[str, str]] = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        rows.append((cells[0].strip("`"), cells[1].strip("`")))
    return rows


class TestDocumentedClaimsAreExecuted(TestCase):
    """Every claim the audit doc makes, asserted against the running code.

    Written because three rounds of review kept finding doc clauses that
    described a stronger rule than the code implements, and because a claim no
    test reads is a sentence nobody re-reads. A doc claim that cannot be
    executed does not belong in that section — it belongs in a comment on the
    code it describes.
    """

    def suite(self, body: str) -> TaskSpec:
        return TaskSpec(
            id="c-doc",
            type="code",
            prompt="Write it.",
            validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": (
                "import unittest\n\nfrom solution import solve\n\n\n"
                "class T(unittest.TestCase):\n    def test_x(self):\n        " + body + "\n"
            )},
        )

    def html(self, **metadata: object) -> TaskSpec:
        return TaskSpec(
            id="h-doc",
            type="html",
            prompt="Create a landing page. Output a single self-contained HTML file.",
            validation=["html", "has_required"],
            metadata=metadata,
        )

    # --- the rules table ---

    def test_the_doc_and_the_code_agree_on_every_rule_and_its_severity(self):
        """Both directions, from the source rather than from one audit run.

        A run over `tasks/` proves the rules that *shipped specs* happen to
        trigger, which is not the same as the code being able to emit them. The
        doc and the code are compared directly instead, so a rename that leaves
        the doc behind — or a new rule that never reaches it — fails here.
        """
        source = Path(orchestral_audit.__file__).read_text(encoding="utf-8")
        severities = {"ERROR": ERROR, "WARN": WARN, "INFO": INFO}
        implemented: dict[str, str] = {}
        for call in ast.walk(ast.parse(source)):
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "Finding"):
                continue
            keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            rule, severity = keywords.get("rule"), keywords.get("severity")
            if not isinstance(rule, ast.Constant):
                continue
            if isinstance(severity, ast.Constant):
                level = severity.value
            elif isinstance(severity, ast.Name) and severity.id in severities:
                level = severities[severity.id]
            else:
                continue
            implemented[rule.value] = level
        self.assertGreater(len(implemented), 5, "no rules parsed out of audit.py")
        documented = dict(_doc_rules())
        self.assertEqual(
            sorted(set(documented) - set(implemented)),
            [],
            "the doc documents a rule the code cannot emit",
        )
        self.assertEqual(
            sorted(set(implemented) - set(documented)),
            [],
            "the code emits a rule the doc does not document",
        )
        for rule, severity in sorted(documented.items()):
            with self.subTest(rule=rule):
                self.assertEqual(implemented[rule], severity)

    # --- "does and does not prove": what it claims to catch ---

    def test_the_suite_must_parse(self):
        spec = TaskSpec(
            id="c-parse", type="code", prompt="Write it.", validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": "class T(unittest.TestCase)\n    def test_x(\n"},
        )
        self.assertIn("does not parse", _rules(spec)["absent_grading_contract"][0].detail)

    def test_the_five_documented_constants_are_caught(self):
        for assertion in (
            "assert 1 == 1", "assert not None", "self.assertIn(1, [1, 2])",
            "assert len([]) == 0", "assert not (1 == 2)",
        ):
            with self.subTest(assertion=assertion):
                self.assertIn("absent_grading_contract", _rules(self.suite(assertion)))

    def test_the_check_is_method_agnostic(self):
        """The doc says the rule does not enumerate the `assert*` methods. A
        method absent from any plausible list has to be caught anyway, or the
        claim is false and the version-dependent list rots silently.
        """
        for assertion in (
            "self.assertSetEqual(frozenset([1]), frozenset([1]))",
            "self.assertDictEqual({'a': 1}, {'a': 1})",
            "self.assertSequenceEqual((1,), (1,))",
        ):
            with self.subTest(assertion=assertion):
                self.assertIn("absent_grading_contract", _rules(self.suite(assertion)))

    def test_the_documented_inert_bodies_are_not_a_gate(self):
        for body in (
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n",
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        'doc'\n",
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        ...\n",
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        global flag\n",
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        flag: int\n",
        ):
            with self.subTest(body=body.splitlines()[-1].strip()):
                spec = TaskSpec(
                    id="c-inert", type="code", prompt="Write it.", validation=["tests_pass"],
                    metadata={"module": "solution.py", "tests": body},
                )
                self.assertIn("absent_grading_contract", _rules(spec))

    def test_a_coroutine_test_on_a_plain_testcase_does_not_count(self):
        body = ("import unittest\n\nfrom solution import solve\n\n\n"
                "class T(unittest.TestCase):\n"
                "    async def test_x(self):\n        self.assertEqual(solve('a'), 'a')\n")
        spec = TaskSpec(
            id="c-async", type="code", prompt="Write it.", validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": body},
        )
        self.assertIn("absent_grading_contract", _rules(spec))

    # --- the same section: the limits it claims NOT to catch ---

    def test_the_documented_unmodelled_forms_audit_clean(self):
        for assertion in (
            'assert f"{1}" == "1"',
            'assert "a,b".split(",") == ["a", "b"]',
        ):
            with self.subTest(assertion=assertion):
                self.assertNotIn("absent_grading_contract", _rules(self.suite(assertion)))

    def test_the_documented_self_arranged_assertion_audits_clean(self):
        """Disclosed as needing execution. If a future change catches it, the doc
        limit is out of date and this test is what says so.
        """
        body = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                "    def test_x(self):\n        self.flag = True\n"
                "        self.assertTrue(self.flag)\n")
        spec = TaskSpec(
            id="c-self", type="code", prompt="Write it.", validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": body},
        )
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_the_documented_unresolvable_base_is_reported_not_cleared(self):
        """The doc now says such a suite is reported and calls that direction
        safe, so the report is the contract.
        """
        body = ("import unittest\n\nfrom solution import solve\n\nTC = unittest.TestCase\n\n\n"
                "class T(TC):\n    def test_x(self):\n        self.assertEqual(solve('a'), 'a')\n")
        spec = TaskSpec(
            id="c-tc", type="code", prompt="Write it.", validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": body},
        )
        self.assertIn("absent_grading_contract", _rules(spec))

    # --- declared anchors ---

    def test_the_documented_useless_declarations_all_clear_nothing(self):
        for value in ("", "kite", ["", ], [0], ["a"], ["  "]):
            with self.subTest(required=value):
                self.assertIn("structural_only", _rules(self.html(required=value)))

    def test_the_documented_anchoring_declarations_anchor(self):
        for value in (["kite"], ("kite", "surfboard"), {"kite": True}):
            with self.subTest(required=value):
                self.assertNotIn("structural_only", _rules(self.html(required=value)))

    def test_every_match_everything_pattern_the_doc_names_really_matches(self):
        """F5 named one instance of a class and called it "one limit". The doc now
        names the class, so the doc's own list is pinned to the behaviour: drop
        one and this fails, invent one and the doc has to say so.
        """
        text = DOC.read_text(encoding="utf-8")
        for pattern in (".", "^", ".*", r"[\s\S]*"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, text)
                spec = TaskSpec(
                    id="h-pat", type="html",
                    prompt="Create a landing page. Output a single HTML file.",
                    validation=["html", "matches_pattern"], metadata={"pattern": pattern},
                )
                self.assertNotIn("structural_only", _rules(spec))

    def test_a_usable_pattern_anchors(self):
        spec = TaskSpec(
            id="h-pat2", type="html",
            prompt="Create a landing page. Output a single HTML file.",
            validation=["html", "matches_pattern"], metadata={"pattern": "kite|surfboard"},
        )
        self.assertNotIn("structural_only", _rules(spec))

    # --- the availability section ---

    def test_the_documented_bounds_all_answer(self):
        """The claim is that these *answer*. `None` is an answer — it means the
        suite was accepted, which is the documented conservative outcome of a cap.
        What must never happen is a raise, a hang, or a wrong verdict on a
        neighbouring tautology, so that is what is asserted.
        """
        def reason(body: str) -> str | None:
            return orchestral_audit._suite_gate_reason(
                "import unittest\n\n\nclass T(unittest.TestCase):\n"
                "    def test_x(self):\n        " + body + "\n"
            )

        for label, body in (
            ("pow chain", "assert 2**2**2**2**2**2 == 1"),
            ("sequence repetition", "assert 'ab' * 10**9 * 10**9 == 'x'"),
            ("deep unary", "assert " + " not" * 2000 + " None"),
        ):
            with self.subTest(case=label):
                started = time.monotonic()
                self.assertIsNone(reason(body))
                self.assertLess(time.monotonic() - started, 5.0)
        # non-vacuity: a real tautology in the same shape is still caught
        self.assertIs(reason("assert 1 == 1"), _CONSTANT_ASSERTIONS_PASSES)
        self.assertIs(reason("assert 1 == 2"), _CONSTANT_ASSERTIONS_FAILS)


# The metadata keys `orchestral/audit.py` reads, with a value of each the author
# could plausibly write and a rule could mishandle. Kept honest by
# `TestTheKeyListIsDerivedFromTheCode`, which derives the set from the source
# rather than trusting this tuple: the hand-written version omitted
# `expected_answer`, which two rules read.
_METADATA_KEYS = (
    "calls", "difficulty", "expected", "expected_answer", "fields", "holdout",
    "pattern", "reference_sql", "required", "tests",
)

_HOSTILE_VALUES = (
    5, 2.5, -3, True, 0, "", [], {}, "kite", "  ", ["kite"], {"kite": True},
    [None], [["kite"]], "((((", "^", 10**12,
)


def _nested_keys_reached() -> set[str]:
    """Every string key that appears inside one of `_HOSTILE_NESTED`'s values.

    Derived from the shapes, not listed beside them. The hand-written version was
    a `frozenset` of six names that nothing tied to the shapes actually supplying
    the coverage, so deleting all four `calls` entries — the only inputs that
    reach `call.get("method")` — left the suite green. The `calls` shapes are the
    F8 hole, and the fix for it was the one piece of F8 nothing verified.
    """
    return _keys_in(_HOSTILE_NESTED)


def _keys_in(shapes: object) -> set[str]:
    """Every string mapping key reachable inside `shapes`, at any depth.

    `metadata.calls` holds a *list of mappings*, so a walker that only descends
    into dicts sees none of it — which is the gap this derivation exists to close.
    """
    reached: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            reached.update(key for key in value if isinstance(key, str))
            for inner in value.values():
                walk(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                walk(inner)

    for shape in shapes:  # type: ignore[union-attr]
        for value in shape.values():  # type: ignore[union-attr]
            walk(value)
    return reached


def _spec_with_metadata(**metadata: object) -> TaskSpec:
    return TaskSpec(
        id="h-meta",
        type="html",
        prompt="Create a landing page. Output a single self-contained HTML file.",
        validation=["html", "has_required", "matches_pattern", "no_forbidden"],
        metadata=metadata,
    )


class TestHostileMetadataNeverRaises(TestCase):
    """No metadata value any author can write may take the gate's answer away.

    `required: 5` raised `TypeError` out of `_required_tokens` and with it every
    other spec's verdict, in the one function in the file that read metadata
    without a type guard. Fixing that instance is not the point: the point is a
    test that asks *every* rule about *every* shape, so the next unguarded read
    fails here instead of in someone's CI.
    """

    def test_no_rule_raises_on_any_metadata_shape(self):
        for key in _METADATA_KEYS:
            for value in _HOSTILE_VALUES:
                with self.subTest(key=key, value=repr(value)):
                    audit_spec(_spec_with_metadata(**{key: value}))  # must not raise

    def test_no_rule_raises_on_a_hostile_value_nested_inside_metadata(self):
        """`metadata: {calls: 5}` is not the only way to reach `call.get("method")`.
        A list of entries with a hostile value inside one reaches it too, and a
        key list alone would not notice.
        """
        for metadata in _HOSTILE_NESTED:
            with self.subTest(metadata=repr(metadata)):
                audit_spec(_spec_with_metadata(**metadata))  # must not raise

    def test_no_rule_raises_when_several_keys_are_hostile_at_once(self):
        for first, second in (("required", "pattern"), ("required", "calls"),
                              ("fields", "required"), ("tests", "expected"),
                              ("holdout", "difficulty")):
            for value in (5, [], {}, "kite"):
                with self.subTest(keys=(first, second), value=repr(value)):
                    audit_spec(_spec_with_metadata(**{first: value, second: value}))

    def test_no_rule_raises_on_a_hostile_metadata_for_every_task_type(self):
        for task_type in ("html", "code", "extract", "sql", "api", "multi-file",
                          "constraint", "needle", "image", "video"):
            with self.subTest(type=task_type):
                for value in (5, [], {}, "kite"):
                    audit_spec(TaskSpec(
                        id="t-meta", type=task_type, prompt="Do it.",
                        validation=["html", "has_required"],
                        metadata={"required": value, "pattern": value,
                                  "fields": value, "calls": value, "tests": value},
                    ))

    def test_the_whole_suite_answers_when_one_spec_carries_a_hostile_key(self):
        """The failure mode was not one bad finding, it was 125 missing ones."""
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "hostile.yaml").write_text(
                textwrap.dedent(
                    """\
                    id: hostile-meta
                    type: html
                    prompt: Create a landing page. Output a single HTML file.
                    validation: [html, has_required]
                    metadata:
                      difficulty: hard
                      required: 5
                    """
                ),
                encoding="utf-8",
            )
            (directory / "ordinary.yaml").write_text(
                textwrap.dedent(
                    """\
                    id: ordinary-meta
                    type: html
                    prompt: Create a landing page. Output a single HTML file.
                    validation: [html]
                    """
                ),
                encoding="utf-8",
            )
            # the real CI command, so a raise cannot be mistaken for a finding
            self.assertTrue(audit_tree_under_clock(directory).strip())


class TestUnusableRequiredDeclarationIsNamed(TestCase):
    """F1: a declaration the grader cannot iterate is reported, not raised.

    The rule whose stated job is "can every check the suite asks for actually
    fire?" was the rule that raised on a misconfigured declaration. At runtime
    `runner.py` raises on the same spec, so the audit is the only static place
    that can name the defect.
    """

    def test_a_scalar_required_is_reported_and_anchors_nothing(self):
        """The grader raises on these, so they are an error rather than an
        unanchored-spec warning. Both halves matter: the report, and the fact that
        the declaration anchors nothing.
        """
        for value in (5, 2.5, -3, True, 10**12):
            with self.subTest(required=value):
                self.assertIn("unreadable_spec_fields", _rules(_spec_with_metadata(required=value)))
                self.assertFalse(orchestral_audit._has_topic_anchor(_spec_with_metadata(required=value)))

    def test_a_falsy_or_empty_declaration_still_reports_nothing_wrong(self):
        """`0`, `""`, `[]` and `{}` are what the runner treats as empty, and the
        runner reports an empty `required` as its own error. The audit has no
        reason to add noise there.
        """
        for value in (0, "", [], {}):
            with self.subTest(required=value):
                found = _rules(_spec_with_metadata(required=value))
                self.assertIn("structural_only", found)
                detail = found["structural_only"][0].detail
                self.assertIn("names nothing the grader can compare against", detail)
                self.assertNotIn("the grader iterates it", detail)

    def test_an_iterable_but_vacuous_required_is_still_only_a_warning(self):
        """A bare string and a one-character token are readable by the grader, so
        they are an unanchored spec rather than a spec that crashes. The severity
        follows the grader's behaviour, not a preference.
        """
        for value in ("kite", ["a"], ["  "], "  "):
            with self.subTest(required=repr(value)):
                found = _rules(_spec_with_metadata(required=value))
                self.assertIn("structural_only", found)
                self.assertEqual(found["structural_only"][0].severity, WARN)

    def test_the_advice_names_the_shape_rather_than_the_topic(self):
        """`required: 5` is not a spec that needs a topic anchor, so telling the
        author to declare a list of words is the wrong instruction. That is the
        F6 defect again: advice that sends the author to fix the wrong thing.
        """
        detail = _rules(_spec_with_metadata(required=5))["unreadable_spec_fields"][0].detail
        self.assertIn("the grader iterates it", detail)
        self.assertIn("a int", detail)  # the shape, named
        self.assertIn("list of words", detail)

    def test_a_non_iterable_required_anchors_nothing(self):
        self.assertEqual(orchestral_audit._required_tokens(5), [])
        self.assertEqual(orchestral_audit._required_tokens(2.5), [])
        self.assertEqual(orchestral_audit._required_tokens(float("nan")), [])


class TestTheTokenListIsTheRunnersTokenList(TestCase):
    """F2: the audit's tokens and the grader's tokens must be the same list.

    `_scalar_token` used to `.strip()` before measuring, so the two disagreed in
    both directions: `" kite "` was `kite` to the audit and `" kite "` to the
    grader, meaning an artifact with the bare word scored 0 while the audit
    called the spec anchored. The grader's line is `str(t).lower()`, so the token
    is `str(value)` and nothing else.
    """

    def test_a_surrounded_token_keeps_its_spaces(self):
        self.assertEqual(orchestral_audit._required_tokens([" kite "]), [" kite "])
        self.assertEqual(orchestral_audit._required_tokens(" kite "), [])

    def test_a_whitespace_only_token_declares_nothing(self):
        """Stricter than the grader, in the safe direction: ordinary indented
        HTML satisfies `"  "`, so treating it as a token would anchor nothing.
        """
        for value in (["  "], ["\t"], ["\n  "], "   "):
            with self.subTest(value=repr(value)):
                self.assertEqual(orchestral_audit._required_tokens(value), [])

    def test_the_model_equals_the_graders_list_for_every_modelled_shape(self):
        """Named for what it claims. The previous version of this test said
        "every shape" and enumerated five, none of them a scalar — and the shape
        it omitted raised. The name is now the claim, so the enumeration and the
        name are checked against each other.
        """
        shapes: list[object] = [["kite"], ("kite", "x"), {"kite": 1}, "kite",
                             ["a", "kite"], [" kite "], ["kite", ""], 5, 0, None, {}]
        for value in shapes:
            with self.subTest(value=repr(value)):
                try:
                    grader_tokens = [str(t).lower() for t in (value or [])]
                except TypeError:
                    grader_tokens = None  # the grader raises; the audit reports
                if grader_tokens is None:
                    self.assertEqual(orchestral_audit._required_tokens(value), [])
                    continue
                self.assertEqual(
                    [t.lower() for t in orchestral_audit._required_tokens(value)],
                    [t for t in grader_tokens if len(t) > 1 and t.strip()],
                )


class TestMixedSuiteStaysSilentForTheRightReason(TestCase):
    """F3: a suite holding a constant *and* a real assertion is not reported.

    The behaviour is right and stays: a suite pinned at 0 by `assert 1 == 2`
    cannot inflate a score, which is the only thing this rule is for. The doc's
    stated reason — "the suite discriminates" — is its opposite, so the reason is
    now the true one and the clause is executed by a test rather than asserted
    in prose.
    """

    @staticmethod
    def _suite(body: str) -> TaskSpec:
        return TaskSpec(
            id="c-mixed", type="code", prompt="Write it.", validation=["tests_pass"],
            metadata={"module": "solution.py", "tests": (
                "import unittest\n\nfrom solution import solve\n\n\n"
                "class T(unittest.TestCase):\n    def test_x(self):\n" + body + "\n"
            )},
        )

    def test_a_constant_plus_a_real_assertion_is_not_reported(self):
        found = _rules(self._suite(
            "        assert 1 == 2\n        self.assertEqual(solve('a b'), 'a-b')\n"))
        self.assertNotIn("absent_grading_contract", found)

    def test_a_never_passing_constant_does_not_make_the_suite_harmless(self):
        """The doc may not claim such a suite discriminates: it fails for every
        artifact, so the score is 0 whatever the solution produces.
        """
        detail = _rules(self._suite("        assert 1 == 2\n"))[
            "absent_grading_contract"][0].detail
        self.assertIn("fails for every artifact", detail)

    def test_the_doc_pins_the_clock_disclosure(self):
        """QA measured the base-chain walk at 11.3 s for N=5000 and asked for the
        disclosure rather than a fix. A disclosure no test reads is the failure
        mode this section exists to remove, so the claim is pinned in both
        directions: the cost is stated, and it is not claimed to be bounded.
        """
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("It is not a bound on the clock", text)
        self.assertIn("O(N²)", text)
        self.assertNotIn("It is a bound on the clock", text)

    def test_the_doc_pins_the_unstripped_token(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("the audit does not strip", text)
        self.assertNotIn("the audit strips", text)

    def test_the_doc_pins_the_field_accounting(self):
        """The claim is that all six `TaskSpec` fields are accounted for. Pinning
        the sentence keeps a future field from being added without either a guard
        or a note that nothing reads it.
        """
        flat = " ".join(DOC.read_text(encoding="utf-8").split())
        self.assertIn("All six `TaskSpec` fields are accounted for", flat)
        for field in ("`id`", "`type`", "`assets`"):
            with self.subTest(field=field):
                self.assertIn(field, flat)

    def test_the_doc_pins_the_strict_exit_code_for_a_raising_spec(self):
        """QA's point was that the doc did not say what happens to the gate, in
        either direction. Pinned in both: a spec whose grader raises exits 1, and
        the doc says so.
        """
        flat = " ".join(DOC.read_text(encoding="utf-8").split())
        self.assertIn("`--strict` exits 1 on it", flat)
        self.assertNotIn("`--strict` exits 0 on it", flat)

    def test_the_doc_states_the_silent_reason_as_a_cannot_inflate_score(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("cannot inflate a score", text)
        self.assertNotIn("there *is* an assertion, and\nthe suite discriminates", text)


class TestTheAuditAgreesWithTheRealGrader(TestCase):
    """The claim "the audit's list has to be the runner's list", executed.

    The other test in this area reimplements `runner.py`'s line and compares two
    models, which cannot catch the two disagreeing with the *code*. This one calls
    `Runner._validate` itself, so a future change to either side that breaks the
    agreement fails here rather than in a leaderboard.
    """

    ARTIFACTS: ClassVar[dict[str, str]] = {
        "word only": "<body>kite</body>",
        "spaced": "<body> kite </body>",
        "generic": "the quick brown fox jumps over the lazy dog",
    }

    @staticmethod
    def _spec(required: object) -> TaskSpec:
        return TaskSpec(
            id="x", type="html", prompt="Write it.",
            validation=["html", "has_required"], metadata={"required": required},
        )

    def _grader_passes_any_artifact(self, required: object) -> bool:
        return all(
            Runner()._validate(self._spec(required), artifact)[1]["checks"].get("has_required")
            for artifact in self.ARTIFACTS.values()
        )

    def test_a_declaration_the_grader_cannot_read_is_reported_not_raised(self):
        """F1. The grader raises on this spec; the audit has to name it, because
        the audit is the only static place that can.
        """
        with self.assertRaises(TypeError):
            Runner()._validate(self._spec(5), self.ARTIFACTS["generic"])
        found = _rules(self._spec(5))
        self.assertIn("unreadable_spec_fields", found)
        self.assertIn("the grader iterates it", found["unreadable_spec_fields"][0].detail)

    def test_a_bare_string_anchors_only_when_the_grader_really_discriminates(self):
        """F4. `required: kite` is iterated per character, so the grader passes
        every artifact — and the audit must refuse to call that anchored. This
        is the assertion that would have caught a declaration that *looks* like
        an anchor and is not.
        """
        self.assertTrue(self._grader_passes_any_artifact("kite"))
        self.assertFalse(orchestral_audit._has_topic_anchor(self._spec("kite")))

    def test_a_token_with_spaces_agrees_with_the_grader(self):
        """F2. The grader compares `" kite "` with its spaces, so the bare word
        does not satisfy it. An audit that stripped the token would call this
        spec anchored while the grader scored a correct artifact 0.
        """
        spec = self._spec([" kite "])
        self.assertTrue(orchestral_audit._has_topic_anchor(spec))
        self.assertFalse(Runner()._validate(spec, self.ARTIFACTS["word only"])[1]["checks"]["has_required"])
        self.assertTrue(Runner()._validate(spec, self.ARTIFACTS["spaced"])[1]["checks"]["has_required"])

    def test_a_mapping_anchors_because_the_grader_enforces_its_keys(self):
        """F6."""
        spec = self._spec({"kite": True})
        self.assertTrue(orchestral_audit._has_topic_anchor(spec))
        self.assertFalse(Runner()._validate(spec, self.ARTIFACTS["generic"])[1]["checks"]["has_required"])

    def test_every_declaration_where_the_audit_anchors_really_discriminates(self):
        """The general form of the three above: whenever the audit says a spec is
        anchored, at least one of these artifacts must actually fail the grader.
        """
        for required in (["kite"], ("kite", "surfboard"), {"kite": True}, [" kite "],
                         ["ab"], ["kite", "a"], 2, 2.5, True, float("nan"), None,
                         "kite", ["  "], ["a"], {}, [], 0, ""):
            with self.subTest(required=repr(required)):
                if not orchestral_audit._has_topic_anchor(self._spec(required)):
                    continue
                self.assertFalse(
                    self._grader_passes_any_artifact(required),
                    "the audit calls this anchored, but the grader passes every artifact",
                )


def _metadata_lookups_in_audit() -> dict[str, set[str]]:
    """Every name `orchestral/audit.py` looks up, mapped to the expression it is
    looked up on.

    Derived from the source on purpose. The hand-written list this replaces
    omitted `expected_answer`, which two rules read, and nothing failed — because
    nothing compared the list to the code. The scan deliberately
    over-approximates: it catches every string-keyed `.get()` and subscript in
    the file, so a name reached through a helper parameter (`metadata.get("fields")`)
    is covered as well as one reached through `spec.metadata`. Over-approximating
    is the safe direction for a hostile-value test: extra names are noise, a
    missing name is a blind spot.
    """
    source = Path(orchestral_audit.__file__).read_text(encoding="utf-8")
    found: dict[str, set[str]] = {}

    def record(name: str, owner: str) -> None:
        found.setdefault(name, set()).add(owner)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                record(node.args[0].value, ast.unparse(node.func.value))
        elif isinstance(node, ast.Subscript):
            if not (isinstance(node.value, ast.Attribute)
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                continue
            record(node.slice.value, ast.unparse(node.value))
    return found


def _metadata_keys_read_by_audit() -> set[str]:
    """The names looked up on a `metadata` expression."""
    return {
        name for name, owners in _metadata_lookups_in_audit().items()
        if any("metadata" in owner for owner in owners)
    }


def _nested_keys_read_by_audit() -> set[str]:
    """Names looked up on something *inside* metadata, e.g. a `calls` entry."""
    return {
        name for name, owners in _metadata_lookups_in_audit().items()
        if not any("metadata" in owner for owner in owners)
    }


# Nested structures a hostile value can hide inside. `metadata: {calls: 5}` is
# not the only shape that reaches `call.get("method")` — a list of entries with a
# hostile value inside one reaches it too, and the key list alone would not notice.
_HOSTILE_NESTED = (
    {"calls": [{"method": 5, "path": 5}]},
    {"calls": [{"method": ["x"], "path": ["x"]}]},
    {"calls": [{"method": "(A+)+B", "path": "(A+)+B"}]},
    {"calls": ["a string, not a mapping"]},
    {"fields": {"a": 5}},
    {"fields": {"a": {"type": 5, "required": 5}}},
    {"fields": {"a": {"required": {"nested": 5}}}},
    {"expected": [5]},
    {"expected": [{"kite": 5}]},
    {"tests": 5},
    {"holdout": 5},
    {"difficulty": {"a": 5}},
)


class TestTheKeyListIsDerivedFromTheCode(TestCase):
    """F8: the comment claimed "every key any rule reads" and omitted one."""

    def test_every_metadata_key_the_audit_reads_is_in_the_hostile_value_list(self):
        missing = sorted(_metadata_keys_read_by_audit() - set(_METADATA_KEYS))
        self.assertEqual(missing, [], f"read by audit.py, absent from _METADATA_KEYS: {missing}")

    def test_every_key_read_inside_metadata_is_reached_by_a_nested_hostile_value(self):
        """`method` and `path` are read off a `calls` entry, not off metadata, so
        they are covered by nesting rather than by the key list. Dropping either
        coverage has to fail here.
        """
        nested = _nested_keys_reached()
        for name in sorted(_nested_keys_read_by_audit()):
            with self.subTest(key=name):
                self.assertIn(name, nested)

    def test_deleting_a_nested_shape_is_detected(self):
        """The survivor QA found, as a test rather than as a mutation.

        `method` and `path` are only reached by a `calls` entry, so if the `calls`
        shapes go, so does the coverage — and the derived set is what notices.
        """
        reached = _nested_keys_reached()
        self.assertIn("method", reached)
        self.assertIn("path", reached)
        without_calls = [
            shape for shape in _HOSTILE_NESTED if "calls" not in shape
        ]
        self.assertNotIn("method", _keys_in(without_calls))
        self.assertNotIn("path", _keys_in(without_calls))

    def test_the_derivation_itself_is_not_vacuous(self):
        """The previous version of this check compared a constant to itself, so
        it passed on an empty code change. This one reads the code, and asserts
        the code still has the shape it is supposed to have.
        """
        read = _metadata_keys_read_by_audit()
        self.assertGreaterEqual(len(read), 8, f"derivation found only {sorted(read)}")
        for expected in ("required", "fields", "tests", "pattern", "expected",
                         "expected_answer", "reference_sql"):
            self.assertIn(expected, read)
        self.assertIn("expected_answer", _METADATA_KEYS)


class TestTheMetadataContainerIsGuarded(TestCase):
    """F7: a type-invalid *container* escaped validation entirely.

    `required: 5` was fixed last round; the container it hangs off was not, and
    the fix added a new unguarded read of the same shape six lines above the one
    that raised. A spec whose `metadata` is a list or a string cannot be read by
    the grader or by any rule.
    """

    HOSTILE_CONTAINERS = (5, 2.5, -3, True, "kite", [1, 2], ("a",), None)

    def test_a_non_mapping_metadata_does_not_raise_in_audit_spec(self):
        for value in self.HOSTILE_CONTAINERS:
            with self.subTest(metadata=repr(value)):
                spec = TaskSpec(id="c-cont", type="html", prompt="Write it.",
                                validation=["html"], metadata=value)
                self.assertIsInstance(audit_spec(spec), list)  # must not raise

    def test_a_non_mapping_metadata_is_reported_with_its_actual_shape(self):
        spec = TaskSpec(id="c-cont", type="html", prompt="Write it.",
                        validation=["html"], metadata=5)
        rules = _rules(spec)
        self.assertIn("unreadable_spec_fields", rules)
        self.assertIn("int", rules["unreadable_spec_fields"][0].detail)

    def test_a_non_mapping_validation_does_not_raise(self):
        for value in (5, "html", {"html": True}, 2.5):
            with self.subTest(validation=repr(value)):
                spec = TaskSpec(id="c-v", type="html", prompt="Write it.",
                                validation=value, metadata={})
                self.assertIsInstance(audit_spec(spec), list)

    def test_a_non_string_prompt_does_not_raise(self):
        for value in (5, ["Write it."], None):
            with self.subTest(prompt=repr(value)):
                spec = TaskSpec(id="c-p", type="html", prompt=value,
                                validation=["html"], metadata={})
                self.assertIsInstance(audit_spec(spec), list)

    def test_load_task_rejects_it_with_the_projects_own_error(self):
        """`load_task` already rejects an unknown `type` with `ConfigError`; a
        type-invalid field is the same class of problem and belongs on the path
        the project already owns, not in a rule ten frames deep.
        """
        for value, key in ((5, "metadata"), (5, "validation"), (5, "prompt"),
                           ("kite", "metadata"), ([1, 2], "metadata")):
            with self.subTest(key=key, value=repr(value)), \
                    tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / "bad.yaml"
                    body = textwrap.dedent("""\
                        id: bad
                        type: html
                        prompt: Write it.
                        validation: [html]
                        """)
                    if key == "prompt":
                        body = body.replace("prompt: Write it.", f"prompt: {value!r}"
                                            if isinstance(value, str) else f"prompt: {value}")
                    else:
                        body += f"{key}: {value!r}\n"
                    path.write_text(body, encoding="utf-8")
                    with self.assertRaises(orchestral_config.ConfigError) as caught:
                        orchestral_config.load_task(path)
                    self.assertIn(key, str(caught.exception))

    def test_a_well_formed_spec_is_unaffected(self):
        spec = TaskSpec(id="c-ok", type="html", prompt="Write it.",
                        validation=["html"], metadata={"difficulty": "hard"})
        self.assertNotIn("unreadable_spec_fields", _rules(spec))


class TestTheFindingSetIsReproducible(TestCase):
    """F9: the same commit produced different output on four consecutive runs.

    `most_common(6)` over a `set` breaks ties by hash order, so a cluster with
    more than six equally-shared terms got six arbitrary ones and the finding's
    *content* changed. Two runs under different hash seeds now have to agree.
    """

    def _audit_json(self) -> str:
        import json
        report = audit_tree(REPO_TASKS)
        return json.dumps(
            sorted(
                (f.severity, f.rule, f.task_id or "", f.detail or "")
                for f in report.findings
            )
        )

    def test_two_runs_under_different_hash_seeds_agree(self):
        """Run in subprocesses: the seed is fixed at interpreter start, so this
        cannot be checked in-process without lying about it.
        """
        repo = str(Path(orchestral_audit.__file__).resolve().parent.parent)
        script = (
            "import json, sys;"
            "from pathlib import Path;"
            f"sys.path.insert(0, {repo!r});"
            "from orchestral.audit import audit_tree;"
            f"r = audit_tree(Path({str(REPO_TASKS)!r}));"
            "print(json.dumps(sorted((f.severity, f.rule, f.task_id or '', f.detail or '')"
            " for f in r.findings)))"
        )
        outputs = []
        for seed in ("0", "1"):
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            outputs.append(result.stdout.strip())
        self.assertEqual(
            outputs[0], outputs[1],
            "the finding set is not reproducible across hash seeds",
        )


class TestEveryTaskSpecFieldIsGuarded(TestCase):
    """F13 and F14: the fourth and sixth fields, and the two live crashes.

    `TaskSpec` has six fields. Three were guarded; `id` and `type` were not, and
    each takes the whole tree's answer rather than one spec's finding — `id`
    because the duplicate-family rule sorts it, `type` because the membership
    test needs a hash. `assets` is the sixth field and nothing reads it, so not
    guarding it is correct rather than an omission.
    """

    def test_an_unhashable_type_does_not_raise_in_audit_spec(self):
        """The direct-caller half of F14. `load_task` rejects it first on the file
        path, so this is the only thing covering the other route — and QA noted
        both, while my first fix only tested the loader.
        """
        for value in (["a"], {"a": 1}, {"b", "a"}, 5, 2.5):
            with self.subTest(type=repr(value)):
                spec = TaskSpec(id="c-t", type=value, prompt="Write it.",
                                validation=["html"], metadata={})
                found = _rules(spec)
                self.assertIn("unreadable_spec_fields", found)
                self.assertIn("type", found["unreadable_spec_fields"][0].detail)

    def test_a_non_string_id_is_reported_rather_than_crashing_the_sort(self):
        spec = TaskSpec(id=5, type="html", prompt="Write it.", validation=["html"], metadata={})
        self.assertIsInstance(audit_spec(spec), list)

    def test_a_suite_with_one_non_string_id_still_answers(self):
        """F13. `find_duplicate_families` sorts the ids of a family, so five
        identical prompts with one `id: 5` is the smallest input that reaches it.
        """
        specs = [
            TaskSpec(id=identifier, type="html",
                     prompt="Create a product page with a headline and a button.",
                     validation=["html"], metadata={})
            for identifier in ("dup-00", "dup-01", "dup-02", "dup-03", 5)
        ]
        report = audit_suite(specs)
        self.assertIn("unreadable_spec_fields", {f.rule for f in report.findings})

    def test_an_unhashable_type_is_rejected_by_load_task_before_the_membership_test(self):
        """F14. `type` is checked with `in TASK_TYPES` on a frozenset, which needs
        a hash, so `type: [a]` raised before any isinstance guard could run.
        """
        for value in (["a"], {"a": 1}, {"b", "a"}, 5, 2.5, True):
            with self.subTest(type=repr(value)), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "bad.yaml"
                literal = value if isinstance(value, str) else repr(value)
                path.write_text(
                    f"id: bad\ntype: {literal}\nprompt: Write it.\nvalidation: [html]\n",
                    encoding="utf-8",
                )
                with self.assertRaises(orchestral_config.ConfigError) as caught:
                    orchestral_config.load_task(path)
                self.assertIn("type", str(caught.exception))

    def test_load_task_rejects_a_non_string_id(self):
        for value in ("5", "[a]", "{a: 1}"):
            with self.subTest(id=value), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "bad.yaml"
                path.write_text(
                    f"id: {value}\ntype: html\nprompt: Write it.\nvalidation: [html]\n",
                    encoding="utf-8",
                )
                with self.assertRaises(orchestral_config.ConfigError) as caught:
                    orchestral_config.load_task(path)
                self.assertIn("id", str(caught.exception))

    def test_a_well_formed_id_and_type_are_unaffected(self):
        spec = TaskSpec(id="ok-1", type="html", prompt="Write it.",
                        validation=["html"], metadata={})
        self.assertNotIn("unreadable_spec_fields", _rules(spec))
        self.assertIn("structural_only", _rules(spec))  # rules still ran


class TestTheSuiteLevelExclusionIsPinned(TestCase):
    """F15: `audit_suite` drops an unreadable spec from the suite-level rules.

    Reverting that one line left the suite green while reinstating the original
    crash, because no test called `audit_suite` with a spec it could not read.
    This is the sixth pass's defect: a claim that the class is closed, shipped
    inside the commit that closes it, with nothing behind it.
    """

    def test_audit_suite_answers_when_a_spec_carries_a_hostile_container(self):
        specs = [
            TaskSpec(id="broken", type="html", prompt="Write it.",
                     validation=["html"], metadata=5),
            TaskSpec(id="ordinary", type="html", prompt="Write it.",
                     validation=["html", "has_required"],
                     metadata={"required": ["kite"], "difficulty": "hard"}),
        ]
        report = audit_suite(specs)
        rules = {f.rule for f in report.findings}
        self.assertIn("unreadable_spec_fields", rules)
        # and the readable spec still gets its own verdict
        self.assertEqual(report.specs, 2)

    def test_audit_suite_answers_when_a_spec_carries_a_non_string_id(self):
        specs = [
            TaskSpec(id=identifier, type="html",
                     prompt="Create a product page with a headline and a button.",
                     validation=["html"], metadata={})
            for identifier in ("dup-00", "dup-01", "dup-02", "dup-03", 5)
        ]
        self.assertIsInstance(audit_suite(specs).findings, list)

    def test_the_holdout_rule_would_crash_on_an_unreadable_spec(self):
        """The control that makes the exclusion load-bearing.

        `check_holdout_arm` reads `spec.metadata.get("holdout")` for every spec it
        is given, so the exclusion is the only thing between a hostile container
        and a tree-wide traceback. This asserts the danger is real, so the test
        above cannot pass by the exclusion being unnecessary.
        """
        specs = [
            TaskSpec(id="a", type="html", prompt="Write it.", validation=["html"], metadata={}),
            TaskSpec(id="b", type="html", prompt="Write it.", validation=["html"], metadata=5),
        ]
        with self.assertRaises(AttributeError):
            check_holdout_arm(specs)
        self.assertIsInstance(audit_suite(specs).findings, list)


class TestMalformedDeclarationsAreAllErrors(TestCase):
    """F19: the severity split was my judgement, not a stated criterion, and it
    was unpinned — a coordinated downgrade in the code and the doc left the suite
    green.

    `AuditReport.ok` says "errors mean a requested gate cannot fire", and a
    non-iterable `required` is exactly that. My argument for a warning was that
    the runner's abort is louder than any audit line, which is an argument about
    noise, not about the criterion `--strict` is wired to. The answer to noise is
    a precise message, which the finding already carries. So there is now one
    severity, and it is the one the file's own policy states.
    """

    def test_a_non_iterable_required_is_an_error_not_a_warning(self):
        rules = _rules(_spec_with_metadata(required=5))
        self.assertEqual(rules["unreadable_spec_fields"][0].severity, ERROR)
        # and the run is refused
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "bad.yaml").write_text(
                textwrap.dedent("""\
                    id: bad-req
                    type: html
                    prompt: Create a landing page. Output a single HTML file.
                    validation: [html, has_required]
                    metadata:
                      difficulty: hard
                      required: 5
                    """),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "harness.py", "audit", "--strict",
                 "--tasks-dir", str(directory)],
                capture_output=True, text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            self.assertNotEqual(result.returncode, 0,
                                "a spec whose grader raises must not pass --strict")

    def test_every_malformed_declaration_uses_the_same_severity(self):
        """One severity for the whole class, so the next author cannot quietly
        split it again and leave the suite green.
        """
        for value in (5, 2.5, -3, True, 10**12, 1.0):
            with self.subTest(required=repr(value)):
                found = _rules(_spec_with_metadata(required=value))
                self.assertEqual(found["unreadable_spec_fields"][0].severity, ERROR)


class TestSharedTermsAreTheDistinctiveOnes(TestCase):
    """F17: alphabetical tie-breaks pick English function words.

    `and`, `at` and `are` are in more than half the shipped corpus, so a finding
    that lists them tells a reader nothing about the template. Ordering by
    count, then by how rare the term is across the corpus, then by name, is
    equally deterministic and actually informative.
    """

    #: The family under test, and a corpus around it. The corpus is the point: a
    #: term in most of the suite carries no information about one family, and with
    #: nothing else in the suite every term ties on both count and rarity, so the
    #: tie-break falls through to the name and picks `and`.
    FAMILY_PROMPT = (
        "Create a landing page for a carbon offset marketplace. Write a headline, "
        "a value proposition, feature bullets and a call-to-action button in one "
        "self-contained HTML file."
    )
    CORPUS_PROMPT = (
        "Summarise the quarterly revenue and margin table for the finance team in "
        "plain prose, and flag anything that moved more than five percent."
    )

    def _family(self, count: int) -> list:
        specs = [
            TaskSpec(id=f"dup-{i:02d}", type="html", prompt=self.FAMILY_PROMPT,
                     validation=["html"], metadata={})
            for i in range(count)
        ] + [
            TaskSpec(id=f"x-{i:02d}", type="html", prompt=self.CORPUS_PROMPT,
                     validation=["html"], metadata={})
            for i in range(40)
        ]
        return audit_suite(specs).findings

    def test_a_shared_family_does_not_report_corpus_wide_function_words(self):
        # the corpus prompt forms a family of its own, so select by the ids named
        families = [f for f in self._family(6) if f.rule == "near_duplicate_family"]
        family = [f for f in families if "dup-" in f.detail]
        self.assertEqual(len(family), 1, f"expected one dup family, got {len(family)}")
        detail = family[0].detail
        self.assertIn("Most shared terms:", detail)
        listed = detail.split("Most shared terms:")[1]
        for word in ("and", "the", "for", "with", "are", "that"):
            with self.subTest(word=word):
                self.assertNotIn(f"'{word}'", listed,
                                 f"{word!r} is in most of the corpus and carries no information")

    def test_the_ordering_is_reproducible_across_runs(self):
        first = [f.detail for f in self._family(6)]
        second = [f.detail for f in self._family(6)]
        self.assertEqual(first, second)

    def test_a_term_the_whole_corpus_shares_is_not_reported(self):
        """The function words are in the *family* prompt too, so the only thing
        that keeps them out is that the corpus contains them more widely. This
        asserts the corpus actually does, so the test above cannot pass vacuously.
        """
        family = self._family(6)
        self.assertEqual(
            len([f for f in family if f.rule == "near_duplicate_family"]), 2,
            "expected one family from the corpus prompt and one from the test prompt",
        )

    def test_a_rare_shared_term_is_reported(self):
        families = [f for f in self._family(6) if f.rule == "near_duplicate_family"]
        detail = next(f for f in families if "dup-" in f.detail).detail
        self.assertIn("carbon", detail)
