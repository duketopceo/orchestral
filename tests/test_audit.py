"""Tests for the static task-spec audit and the fail-closed check-name guard."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from dataclasses import replace
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
from orchestral.config import TASK_TYPES, TaskSpec, load_task
from orchestral.runner import Runner

REPO_TASKS = Path(__file__).resolve().parent.parent / "tasks"


def replace_spec_metadata(spec: TaskSpec, **changes) -> TaskSpec:
    """A copy of `spec` with fields replaced, so a fixture edit does not mutate the original."""
    return replace(spec, **changes)


def validation_check_table(doc: str) -> str:
    """The `## Validation checks` section — the hand-maintained table of every name.

    Scoped to the section rather than the whole file so a name that survives
    somewhere in the prose does not stand in for its table row. A guard that
    accepts any mention is a guard that cannot fail.
    """
    lines = doc.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "## Validation checks"), None)
    if start is None:
        raise AssertionError("docs/task-spec.md has no '## Validation checks' section")
    rest = lines[start + 1 :]
    end = next((i for i, line in enumerate(rest) if line.startswith("## ")), len(rest))
    return "\n".join(rest[:end])


def undocumented_names(names: set[str], doc: str) -> list[str]:
    """Registered check names the hand-maintained doc table does not carry."""
    table = validation_check_table(doc)
    return sorted(name for name in names if f"| `{name}` |" not in table)


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
CONSTANT_SUITE = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        assert True\n"
CONSTANT_ASSERT_TRUE = (
    "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
)
CONSTANT_ASSERT_EQUAL = (
    "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1, 1)\n"
)


def _suite(body: str, *, prelude: str = "import unittest\n\nimport solution\n") -> str:
    """One collected test method in a TestCase, with `body` as its suite."""
    return f"{prelude}\n\nclass T(unittest.TestCase):\n    def test_x(self):\n{body}\n"


def _code(**metadata) -> TaskSpec:
    base = {"id": "c-suite", "type": "code", "prompt": "Write it.", "metadata": {"module": "solution.py"}}
    base["metadata"].update(metadata)
    return TaskSpec(**base)


# QC's nine tautology shapes, verbatim. The first four were already caught; the
# next five each audited clean and each passed against a 4-byte artifact.
TAUTOLOGY_BODIES = {
    "assertTrue(True)": "        self.assertTrue(True)",
    "assertEqual(1, 1)": "        self.assertEqual(1, 1)",
    "assertTrue(1 == 1)": "        self.assertTrue(1 == 1)",
    "assertEqual(1, 1+0)": "        self.assertEqual(1, 1+0)",
    "assertEqual(2 * 3, 6)": "        self.assertEqual(2 * 3, 6)",
    "assertIn(x, [x])": "        x = solution.solve('a b')\n        self.assertIn(x, [x])",
    "assertIn(x, (x,))": "        x = solution.solve('a b')\n        self.assertIn(x, (x,))",
    "self.assertIs(s, s)": "        s = solution.solve('a b')\n        self.assertIs(s, s)",
    "self.assertEqual(x, x)": "        x = solution.solve('a b')\n        self.assertEqual(x, x)",
    "assertEqual(0, len(''))": "        self.assertEqual(0, len(''))",
}

# A collected, non-constant assertion that reads the interpreter rather than the
# module under test. It is not a tautology, so the only thing left to give it
# away is the missing reference.
MODULE_FREE_BODY = "        self.assertGreater(len(sys.argv), 0)"
MODULE_FREE_PRELUDE = "import unittest\nimport sys\n"

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
        for name in sorted(names):
            if name in shorthands.get(task_type, frozenset()):
                if name not in source:
                    missing.append((task_type, name))
            elif f'checks["{name}"]' not in source:
                missing.append((task_type, name))
    return missing


def _task(**kwargs) -> TaskSpec:
    base = {"id": "t-1", "type": "html", "prompt": "Write a page about kites."}
    base.update(kwargs)
    return TaskSpec(**base)


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

    def test_every_registered_check_name_is_documented_in_the_task_spec(self):
        """`docs/task-spec.md` carries a hand-maintained table of every name.

        Two other tests hold the registry to the runner. Nothing held it to the
        doc, so a name could ship implemented, tested, and undocumented, and the
        table would silently fall a row behind the registry.
        """
        from orchestral.audit import VALIDATION_CHECKS

        names = {name for group in VALIDATION_CHECKS.values() for name in group}
        self.assertEqual(undocumented_names(names, self._spec_doc()), [])

    def test_the_doc_drift_guard_flags_a_name_the_table_omits(self):
        """A guard that only ever sees a complete table cannot detect its own blind spot."""
        self.assertEqual(undocumented_names({"has_paths", "has_novel_check"}, self._spec_doc()), ["has_novel_check"])
        self.assertEqual(undocumented_names({"has_paths"}, self._spec_doc()), [])

    @staticmethod
    def _spec_doc() -> str:
        return (REPO_TASKS.parent / "docs" / "task-spec.md").read_text()


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
        # `module` must name the file the suite imports, or the spec grades one
        # file and tests another: the runner would demand `a.py` while the suite
        # does `from solution import solve`, and the suite would ImportError.
        spec = TaskSpec(
            id="c-3",
            type="code",
            prompt="Write it.",
            metadata={"module": "solution.py", "tests": ASSERTING_SUITE},
        )
        self.assertNotIn("absent_grading_contract", _rules(spec))

    def test_a_suite_that_tests_a_different_module_than_the_spec_grades_is_an_error(self):
        """The declared module and the imported module are the same gate, twice.

        This fixture was the shape the previous test used, and it passed because
        nothing compared the two. A spec that grades `a.py` while its suite
        imports `solution` can never pass a correct artifact, and the audit did
        not say so.
        """
        spec = TaskSpec(
            id="c-mismatch", type="code", prompt="Write it.",
            metadata={"module": "a.py", "tests": ASSERTING_SUITE},
        )
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("never names 'a.py'", found["absent_grading_contract"][0].detail)

    def test_word_assert_in_a_docstring_is_not_an_assertion(self):
        spec = TaskSpec(
            id="c-4",
            type="code",
            prompt="Write it.",
            metadata={"tests": '"""Assert the slug is lowercase."""\n\n\ndef test_x():\n    pass\n'},
        )
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_constant_assertion_is_an_error(self):
        """`assert True` can never fail, so it gates nothing."""
        spec = TaskSpec(id="c-5", type="code", prompt="Write it.", metadata={"tests": CONSTANT_SUITE})
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("constant", found["absent_grading_contract"][0].detail)

    def test_constant_assert_true_call_is_an_error(self):
        spec = TaskSpec(id="c-6", type="code", prompt="Write it.", metadata={"tests": CONSTANT_ASSERT_TRUE})
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_constant_assert_equal_is_an_error(self):
        spec = TaskSpec(id="c-7", type="code", prompt="Write it.", metadata={"tests": CONSTANT_ASSERT_EQUAL})
        self.assertIn("absent_grading_contract", _rules(spec))

    def test_assertion_outside_a_collected_test_is_an_error(self):
        """unittest collects only TestCase methods, so this assertion never runs."""
        for suite in (UNCOLLECTED_SUITE, NOT_A_TEST_CASE, WRONG_NAME_SUITE):
            with self.subTest(suite=suite.splitlines()[0]):
                spec = TaskSpec(id="c-8", type="code", prompt="Write it.", metadata={"tests": suite})
                found = _rules(spec)
                self.assertIn("absent_grading_contract", found)
                self.assertIn("collect", found["absent_grading_contract"][0].detail)

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


class TestSuiteTautologies(unittest.TestCase):
    """A suite that reads nothing from the artifact cannot discriminate.

    `docs/task-audit.md` discloses that an assertion the *test itself* arranges —
    `self.assertTrue(self.flag)`, `self.assertEqual(f(x), f(x))` — needs
    execution to detect, and the audit does not claim otherwise. The shapes here
    are the ones that need no execution at all: the assertion is a tautology by
    construction, so no artifact can turn it red.
    """

    # QC's nine shapes are in TAUTOLOGY_BODIES, at module scope.

    def test_every_tautology_shape_is_an_error(self):
        for label, body in TAUTOLOGY_BODIES.items():
            with self.subTest(shape=label):
                found = _rules(_code(tests=_suite(body)))
                self.assertIn("absent_grading_contract", found, f"{label} audited clean")
                self.assertEqual(found["absent_grading_contract"][0].severity, ERROR)

    def test_a_real_suite_is_not_a_tautology(self):
        """The control: reading the artifact must stay clean, or the rule is useless."""
        for label, body in {
            "equality against a return value": "        self.assertEqual(solution.solve('a b'), 'a-b')",
            "smoke check on a return value": "        self.assertIsNotNone(solution.solve('a b'))",
            "a length of a return value": "        self.assertEqual(len(solution.solve('a b')), 3)",
            "membership of a return value": "        self.assertIn(solution.solve('a b'), ['a-b'])",
        }.items():
            with self.subTest(shape=label):
                self.assertNotIn("absent_grading_contract", _rules(_code(tests=_suite(body))))

    def test_a_repeated_call_is_left_to_execution(self):
        """`f(x) == f(x)` repeats, but `f` may read the artifact.

        The doc keeps this class as execution-only, so flagging it here would
        claim a bound the audit does not have.
        """
        body = "        f = solution.solve\n        self.assertEqual(f('a b'), f('a b'))"
        self.assertNotIn("absent_grading_contract", _rules(_code(tests=_suite(body))))

    def test_a_negating_assertion_is_not_reported_as_a_tautology(self):
        """`assertNotEqual(x, x)` can never pass — a broken suite, not a gate
        that cannot fail, and naming it a tautology would misdescribe it."""
        body = "        x = solution.solve('a b')\n        self.assertNotEqual(x, x)"
        self.assertNotIn("absent_grading_contract", _rules(_code(tests=_suite(body))))

    # A collected, non-constant assertion that reads the interpreter rather than
    # the module under test. It is not a tautology, so the only thing left to
    # give it away is the missing reference.
    def test_a_suite_that_never_names_the_module_is_an_error(self):
        """The assertion is not a constant, so only the module reference gives it away."""
        spec = _code(tests=_suite(MODULE_FREE_BODY, prelude=MODULE_FREE_PRELUDE))
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("never names 'solution.py'", found["absent_grading_contract"][0].detail)

    def test_every_way_of_naming_the_module_counts_as_a_reference(self):
        """A real suite uses whichever reads best, so all of them must clear the rule.

        The shipped `code-*` specs need two of these (`from calc import evaluate`,
        `from lru import LRUCache`), so a check that only understood `import x`
        would flag honest specs.
        """
        cases = {
            "import module": (
                "import unittest\nimport solution\n",
                "        self.assertGreater(len(solution.__name__), 0)",
            ),
            "from module import name": (
                "import unittest\nfrom solution import solve\n",
                "        self.assertGreater(len(solve.__name__), 0)",
            ),
            "import_module by name": (
                "import unittest\nimport importlib\n",
                "        m = importlib.import_module('solution')\n        self.assertGreater(len(m.__name__), 0)",
            ),
        }
        for label, (prelude, body) in cases.items():
            with self.subTest(form=label):
                self.assertNotIn(
                    "absent_grading_contract", _rules(_code(tests=_suite(body, prelude=prelude))), label
                )

    def test_the_tautology_message_wins_over_the_module_message(self):
        """A suite of constants is a tautology whether or not it names the module."""
        spec = _code(tests=_suite("        assert True", prelude="import unittest\n"))
        self.assertIn("constant", _rules(spec)["absent_grading_contract"][0].detail)

    def test_the_declared_module_is_the_one_the_runner_looks_for(self):
        """`metadata.module` overrides the default, so the reference check follows it."""
        suite = _suite(MODULE_FREE_BODY, prelude="import unittest\nimport calc\nimport sys\n")
        named = TaskSpec(
            id="c-mod", type="code", prompt="Write it.", metadata={"module": "calc.py", "tests": suite}
        )
        self.assertNotIn("absent_grading_contract", _rules(named))
        # the same suite, against the runner's default module, never names it
        self.assertIn("absent_grading_contract", _rules(_code(tests=suite)))


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

    def test_multi_file_reading_bodies_is_not_flagged(self):
        """`has_paths` plus a body-reading check with tokens is an anchored fileset."""
        spec = TaskSpec(
            id="mf-2",
            type="multi-file",
            prompt="Build a two-page microsite.",
            validation=["has_paths", "has_content"],
            metadata={
                "expected_paths": ["index.html", "style.css"],
                "required_content": {"index.html": ["pricing"], "style.css": ["pricing"]},
            },
        )
        found = _rules(spec)
        self.assertNotIn("unanchored_fileset", found)
        self.assertNotIn("unknown_validation_check", found)

    def test_multi_file_without_expected_paths_is_flagged(self):
        """The guard used to require `has_paths`, so the unanchored case never fired."""
        spec = TaskSpec(id="mf-3", type="multi-file", prompt="Build a site.", validation=["non_empty", "zip_signature"])
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
            id="mf-5",
            type="multi-file",
            prompt="Build a site.",
            validation=["non_empty", "zip_signature"],
            metadata={"expected_paths": ["index.html"]},
        )
        found = _rules(spec)
        self.assertIn("unanchored_fileset", found)
        self.assertIn("has_paths is not requested", found["unanchored_fileset"][0].detail)

    def test_has_content_without_metadata_stays_flagged(self):
        """Asking for the check without declaring the tokens is still unanchored.

        The check fails closed at grade time, so the gate fires — but a spec
        that requests it and configures nothing is not a graded site, and the
        audit should still say so.
        """
        spec = TaskSpec(
            id="mf-6",
            type="multi-file",
            prompt="Build a two-page microsite.",
            validation=["has_paths", "has_content"],
            metadata={"expected_paths": ["index.html"]},
        )
        self.assertIn("unanchored_fileset", _rules(spec))

    def test_every_unanchored_fileset_state_is_an_error(self):
        """Deleting one token from a spec must not be a route to green.

        Warn was the hole: the audit named the exact fix and `--strict` still
        exited 0, so a one-token deletion turned a declared gate off with no edit
        to the audit itself. Same defect as `unknown_validation_check`, so same
        severity.
        """
        unanchored = {
            "expected_paths unread": {
                "validation": ["non_empty", "zip_signature"],
                "metadata": {"expected_paths": ["index.html", "style.css"]},
                "names": "has_paths is not requested",
            },
            "required_content unread": {
                "validation": ["non_empty", "zip_signature", "has_paths"],
                "metadata": {
                    "expected_paths": ["index.html", "style.css"],
                    "required_content": {"index.html": ["pricing"]},
                },
                "names": "has_content is not requested",
            },
            "both unread": {
                "validation": ["non_empty", "zip_signature"],
                "metadata": {
                    "expected_paths": ["index.html", "style.css"],
                    "required_content": {"index.html": ["pricing"]},
                },
                "names": "has_paths is not requested",
            },
            "nothing declared": {
                "validation": ["non_empty", "zip_signature"],
                "metadata": {},
                "names": "the fileset is unanchored",
            },
            "names and byte counts only": {
                "validation": ["has_paths"],
                "metadata": {"expected_paths": ["index.html", "style.css"]},
                "names": "no check reads the file bodies",
            },
        }
        for label, case in unanchored.items():
            with self.subTest(state=label):
                spec = TaskSpec(
                    id="mf-e", type="multi-file", prompt="Build a site.",
                    validation=case["validation"], metadata=case["metadata"],
                )
                found = _rules(spec)
                self.assertIn("unanchored_fileset", found, label)
                finding = found["unanchored_fileset"][0]
                self.assertEqual(finding.severity, ERROR, label)
                self.assertIn(case["names"], finding.detail, label)

    def test_both_unread_inputs_are_named_in_one_finding(self):
        """One finding, both shapes: the author gets the whole fix, not half of it."""
        spec = TaskSpec(
            id="mf-both",
            type="multi-file",
            prompt="Build a site.",
            validation=["non_empty", "zip_signature"],
            metadata={
                "expected_paths": ["index.html"],
                "required_content": {"index.html": ["pricing"]},
            },
        )
        detail = _rules(spec)["unanchored_fileset"][0].detail
        self.assertIn("has_paths is not requested", detail)
        self.assertIn("has_content is not requested", detail)

    def test_a_declared_path_the_body_check_does_not_cover_is_unread(self):
        """`has_content` forces a body for every path it names, and no other.

        A path in `expected_paths` that the body check does not cover is declared
        and read by nobody, even though the fileset as a whole is anchored — which
        is why the early return cannot be "any body check is enough".
        """
        spec = TaskSpec(
            id="mf-partial",
            type="multi-file",
            prompt="Build a site.",
            validation=["non_empty", "zip_signature", "has_content"],
            metadata={
                "expected_paths": ["index.html", "style.css"],
                "required_content": {"index.html": ["pricing"]},
            },
        )
        found = _rules(spec)
        self.assertIn("unanchored_fileset", found)
        self.assertEqual(found["unanchored_fileset"][0].severity, ERROR)
        self.assertIn("has_paths is not requested", found["unanchored_fileset"][0].detail)

    def test_has_paths_is_redundant_when_the_body_check_covers_every_declared_path(self):
        """The converse: `has_content` on all declared paths reads them all, so
        dropping `has_paths` costs the spec nothing and must not be reported."""
        spec = TaskSpec(
            id="mf-covered",
            type="multi-file",
            prompt="Build a site.",
            validation=["non_empty", "zip_signature", "has_content"],
            metadata={
                "expected_paths": ["index.html", "style.css"],
                "required_content": {"index.html": ["pricing"], "style.css": ["pricing"]},
            },
        )
        self.assertNotIn("unanchored_fileset", _rules(spec))

    def test_deleting_a_gate_token_from_the_shipped_spec_is_an_error(self):
        """The shipped spec with a load-bearing token removed must stop passing.

        `has_content` is load-bearing and `has_paths` is not: `has_content` needs
        a body for every path it names, so it already forces both declared files
        to exist. Dropping `has_paths` there costs the spec nothing, and saying
        otherwise would train authors to ignore the error.
        """
        shipped = load_task(REPO_TASKS / "multi-file-site.yaml")
        dropped = replace_spec_metadata(
            shipped, validation=[c for c in shipped.validation if c != "has_content"]
        )
        found = _rules(dropped)
        self.assertIn("unanchored_fileset", found)
        self.assertEqual(found["unanchored_fileset"][0].severity, ERROR)
        self.assertIn("has_content is not requested", found["unanchored_fileset"][0].detail)

        redundant = replace_spec_metadata(
            shipped, validation=[c for c in shipped.validation if c != "has_paths"]
        )
        self.assertNotIn("unanchored_fileset", _rules(redundant))


class TestPresenceOnlyExtractContract(unittest.TestCase):
    """`required` grades presence and type; it never compares a value."""

    def test_required_fields_without_expected_is_a_warning(self):
        spec = TaskSpec(
            id="x-presence",
            type="extract",
            prompt="Pull the invoice number, date, and total.",
            metadata={
                "fields": {
                    "invoice_number": {"type": "string", "required": True},
                    "total": {"type": "number", "required": True},
                }
            },
        )
        found = _rules(spec)
        self.assertIn("presence_only_extract_contract", found)
        finding = found["presence_only_extract_contract"][0]
        self.assertEqual(finding.severity, WARN)
        self.assertIn("invoice_number", finding.detail)
        self.assertIn("never compares a value", finding.detail)

    def test_a_fabricated_value_scores_the_same_as_a_correct_one(self):
        """Why the finding exists, stated as a fact about the runner."""
        from orchestral.extract import check_extraction

        spec = TaskSpec(
            id="x-presence",
            type="extract",
            prompt="Pull the total.",
            metadata={"fields": {"total": {"type": "number", "required": True}}},
        )
        report = check_extraction(spec.metadata, '{"total": 0.01}')
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(report["field_results"], {})

    def test_expected_clears_the_finding(self):
        spec = TaskSpec(
            id="x-presence",
            type="extract",
            prompt="Pull the total.",
            metadata={
                "fields": {"total": {"type": "number", "required": True}},
                "expected": {"total": 249.0},
            },
        )
        self.assertNotIn("presence_only_extract_contract", _rules(spec))

    def test_an_empty_contract_is_the_absent_contract_error_not_this_warning(self):
        """No required field and no expected is already an error; do not double-report."""
        spec = TaskSpec(id="x-empty", type="extract", prompt="Pull the total.", metadata={})
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertNotIn("presence_only_extract_contract", found)

    def test_other_types_are_untouched(self):
        for task_type in ("html", "code", "sql", "api", "multi-file", "image"):
            with self.subTest(type=task_type):
                self.assertNotIn("presence_only_extract_contract", _rules(_task(id="t-p", type=task_type)))


class TestMediaSpecsAreNotJudgedByTextChecks(unittest.TestCase):
    """`image` / `video` artifacts are bytes; a text token is not a weaker
    anchor, it is an unimplemented one. The audit used to ask for
    `has_required` on a PNG, which the runner drops as an unknown check — the
    spec would have looked anchored while gating on nothing.

    This supersedes the earlier `type-aware advice` contract, which still
    reported `structural_only` for a media spec. A warning the author cannot
    close teaches people to ignore warnings; `judge_gated_media` states the
    real condition instead: the topicality of a PNG rests on the run carrying
    `--judge`.
    """

    def _media(self, task_type: str, **kwargs) -> TaskSpec:
        base = {
            "id": f"{task_type}-1",
            "type": task_type,
            "prompt": "Generate a hero image for a coffee subscription page.",
            "validation": ["non_empty", "png_signature" if task_type == "image" else "mp4_signature"],
        }
        base.update(kwargs)
        return TaskSpec(**base)

    def test_media_is_not_reported_as_structural_only(self):
        for task_type in ("image", "video"):
            with self.subTest(task_type=task_type):
                found = _rules(self._media(task_type))
                self.assertNotIn("structural_only", found)

    def test_media_reports_judge_gated_media_at_info(self):
        for task_type in ("image", "video"):
            with self.subTest(task_type=task_type):
                found = _rules(self._media(task_type))
                self.assertIn("judge_gated_media", found)
                finding = found["judge_gated_media"][0]
                self.assertEqual(finding.severity, INFO)
                self.assertIn("--judge", finding.detail)

    def test_text_anchor_on_a_media_spec_is_still_an_error(self):
        """The audit does not become a way to *excuse* a text anchor on bytes."""
        spec = self._media("image", validation=["non_empty", "png_signature", "has_required"])
        found = _rules(spec)
        self.assertIn("unknown_validation_check", found)
        self.assertNotIn("structural_only", found)

    def test_structural_only_advice_still_names_the_text_checks(self):
        detail = _rules(_task(validation=["html"]))["structural_only"][0].detail
        self.assertIn("has_required", detail)
        self.assertIn("matches_pattern", detail)


    def test_missing_difficulty_is_info_not_a_warning(self):
        found = _rules(_task())
        self.assertEqual(found["unlabeled_difficulty"][0].severity, INFO)

    def test_declared_difficulty_clears_the_info(self):
        self.assertNotIn("unlabeled_difficulty", _rules(_task(metadata={"difficulty": "hard"})))

    def test_metadata_required_clears_the_finding_for_a_text_type(self):
        spec = _task(validation=["html"], metadata={"required": ["kite"]})
        self.assertNotIn("structural_only", _rules(spec))

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
        # Nothing in the spec can clear this one: `metadata.required` is inert
        # for a PNG, so the finding that remains is the honest one — the
        # artifact is gated on the run carrying `--judge`, not on the spec.
        self.assertNotIn("structural_only", found)
        self.assertIn("judge_gated_media", found)


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
