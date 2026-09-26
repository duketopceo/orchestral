"""Tests for the static task-spec audit and the fail-closed check-name guard."""

from __future__ import annotations

import inspect
import tempfile
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
ASSERTING_SUITE = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1, 1)\n"

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
        for task_type, names in VALIDATION_CHECKS.items():
            source = inspect.getsource(getattr(runner.Runner, VALIDATOR_FOR_TYPE[task_type]))
            for name in sorted(set(names) - VALIDATION_SHORTHANDS):
                with self.subTest(type=task_type, check=name):
                    self.assertIn(f'checks["{name}"]', source)


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

    def test_extract_without_fields_or_expected_is_an_error(self):
        """`check_extraction` scores an empty contract as 1.0, so no rule saw it."""
        spec = TaskSpec(id="x-1", type="extract", prompt="Pull the totals from this invoice.", metadata={})
        found = _rules(spec)
        self.assertIn("absent_grading_contract", found)
        self.assertIn("fields", found["absent_grading_contract"][0].detail)

    def test_extract_with_fields_only_is_clean_of_that_error(self):
        spec = TaskSpec(
            id="x-2",
            type="extract",
            prompt="Pull the total.",
            metadata={"fields": {"total": {"type": "number"}}},
        )
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
