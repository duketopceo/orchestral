"""Tests for the static task-spec audit and the fail-closed check-name guard."""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from orchestral.audit import (
    ERROR,
    INFO,
    WARN,
    audit_spec,
    audit_suite,
    audit_tree,
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

    def test_unparseable_by_recursion_limit_is_an_error_not_a_crash(self):
        """The same shape in-process, so the verdict itself is asserted."""
        spec = TaskSpec(
            id="c-rec",
            type="code",
            prompt="Write it.",
            metadata={"tests": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                               "    def test_x(self):\n        assert " + "not " * 3000 + "None\n"},
        )
        found = _rules(spec)  # must not raise
        self.assertIn("absent_grading_contract", found)

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
