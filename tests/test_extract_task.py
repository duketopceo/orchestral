"""Tests for the extract task type — deterministic JSON field grading."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from orchestral.config import ModelConfig, TaskSpec
from orchestral.extract import check_extraction, extract_json
from orchestral.runner import Runner

METADATA = {
    "fields": {
        "name": {"type": "str", "required": True},
        "age": {"type": "int", "required": True},
        "vip": {"type": "bool"},
        "tier": {"type": "str", "enum": ["gold", "silver", "bronze"]},
    },
    "expected": {"name": "Ada", "age": 36, "vip": True, "tier": "gold"},
}

# DUK-94: `total` is declared but neither required nor expected, so nothing grades it.
UNANCHORED = {"fields": {"total": {"type": "number"}}}

# DUK-238: two artifacts for the one contract above. The right one scores 1.0; the
# wrong one has every required field present and every declared value wrong, so it
# scores 0.0 and clears any floor at or below 0.
_RIGHT_ARTIFACT = dict(METADATA["expected"])
_WRONG_ARTIFACT = {"name": "Grace", "age": 45, "vip": False, "tier": "bronze"}
_RIGHT = json.dumps(_RIGHT_ARTIFACT)
_WRONG = json.dumps(_WRONG_ARTIFACT)


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "extract-test",
        "type": "extract",
        "prompt": "Extract name/age/vip/tier from the record.",
        "metadata": dict(METADATA),
    }
    base.update(kwargs)
    return TaskSpec(**base)


class _FakeClient:
    """Chat stand-in: plan → two subtasks, workers → canned JSON, pick → 0."""

    def __init__(self, payloads: list[str], pick: int = 0):
        self.payloads = payloads
        self.pick = pick
        self.worker_calls = 0

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = self.payloads[min(self.worker_calls, len(self.payloads) - 1)]
            self.worker_calls += 1
        elif "candidates" in data:
            body = json.dumps({"subtask_id": self.pick})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [
                {"id": 0, "description": "first extraction"},
                {"id": 1, "description": "second extraction"},
            ]})
        else:
            body = json.dumps({"score": 0.5, "passed": True, "reasoning": "ok"})
        return {
            "content": body,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "latency_ms": 1,
            "id": "fake",
        }

    def close(self):
        pass


class TestExtractJson(unittest.TestCase):
    def test_raw_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('sure:\n```json\n{"a": 2}\n```'), {"a": 2})

    def test_embedded_in_prose(self):
        self.assertEqual(extract_json('The answer is {"a": 3} — done.'), {"a": 3})

    def test_array(self):
        self.assertEqual(extract_json("[1, 2]"), [1, 2])

    def test_unparseable(self):
        self.assertIsNone(extract_json("no json here"))


class TestCheckExtraction(unittest.TestCase):
    def test_perfect_scores_one(self):
        report = check_extraction(METADATA, json.dumps(METADATA["expected"]))
        self.assertTrue(report["parsed"])
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 1.0)

    def test_partial_credit(self):
        obj = dict(METADATA["expected"], tier="silver")
        report = check_extraction(METADATA, json.dumps(obj))
        self.assertAlmostEqual(report["score"], 0.75)
        self.assertFalse(report["field_results"]["tier"])
        self.assertFalse(report["passes"])  # default threshold is 1.0

    def test_pass_threshold_allows_partial(self):
        md = dict(METADATA, pass_threshold=0.5)
        obj = dict(METADATA["expected"], tier="silver", vip=False)
        report = check_extraction(md, json.dumps(obj))
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 0.5)

    def test_present_but_wrong_artifact_fails_a_zero_threshold(self):
        """DUK-238: `pass_threshold: 0` must not let a wrong artifact through.

        This test used to be `test_missing_required_fails_despite_threshold`. It
        set `pass_threshold=0.0` against an artifact *missing* a required field,
        so it went green on the presence gate (`score = 0.25 >= 0.0` was already
        satisfied) and never exercised the threshold it named. A green test that
        certifies the fail-open is how this survived DUK-94 review. Here every
        required field is present and every declared value is wrong, so the score
        is 0.0 and the floor is the only gate left to fail.
        """
        md = dict(METADATA, pass_threshold=0.0)
        report = check_extraction(md, _WRONG)
        self.assertTrue(report["checks"]["required_present"])
        self.assertTrue(report["checks"]["types_ok"])
        self.assertEqual(report["score"], 0.0)
        self.assertFalse(report["passes"])

    def test_missing_required_fails_under_the_lowest_legal_floor(self):
        """The presence gate survives the most permissive floor the contract allows."""
        md = dict(METADATA, pass_threshold=0.01)
        report = check_extraction(md, json.dumps({"name": "Ada"}))
        self.assertFalse(report["checks"]["required_present"])
        self.assertIn("age", report["missing_required"])
        self.assertFalse(report["passes"])

    def test_bool_is_not_int(self):
        obj = dict(METADATA["expected"], age=True)
        report = check_extraction(METADATA, json.dumps(obj))
        self.assertFalse(report["checks"]["types_ok"])
        self.assertFalse(report["passes"])

    def test_enum_violation_fails(self):
        obj = dict(METADATA["expected"], tier="platinum")
        report = check_extraction(METADATA, json.dumps(obj))
        self.assertFalse(report["checks"]["types_ok"])

    def test_non_object_json_scores_zero(self):
        report = check_extraction(METADATA, "[1, 2, 3]")
        self.assertTrue(report["parsed"])
        self.assertEqual(report["score"], 0.0)
        self.assertFalse(report["passes"])

    def test_unparseable_scores_none(self):
        report = check_extraction(METADATA, "I cannot help with that.")
        self.assertFalse(report["parsed"])
        self.assertIsNone(report["score"])
        self.assertFalse(report["passes"])

    def test_no_expected_means_schema_only(self):
        md = {"fields": {"name": {"type": "str", "required": True}}}
        self.assertEqual(check_extraction(md, '{"name": "x"}')["score"], 1.0)
        self.assertEqual(check_extraction(md, '{"name": 1}')["score"], 0.0)

    def test_list_fields_compare_elementwise(self):
        # The list branch of _strict_eq pairs elements with zip(); a length
        # mismatch must score 0 rather than pair the shorter prefix and pass.
        md = {"expected": {"tags": ["a", "b"]}}
        self.assertEqual(check_extraction(md, json.dumps({"tags": ["a", "b"]}))["score"], 1.0)
        self.assertEqual(check_extraction(md, json.dumps({"tags": ["a"]}))["score"], 0.0)
        self.assertEqual(check_extraction(md, json.dumps({"tags": ["a", "c"]}))["score"], 0.0)

    def test_bool_nested_in_a_list_is_not_an_int(self):
        # [True] == [1] in Python, so the elementwise walk must re-check the
        # bool/int distinction instead of deferring to list __eq__.
        md = {"expected": {"counts": [1, 2]}}
        self.assertEqual(check_extraction(md, json.dumps({"counts": [1, 2]}))["score"], 1.0)
        report = check_extraction(md, json.dumps({"counts": [True, 2]}))
        self.assertEqual(report["score"], 0.0)
        self.assertFalse(report["field_results"]["counts"])


class TestPassThreshold(unittest.TestCase):
    """DUK-238: `pass_threshold` is a declared score floor, and a bad one is an error.

    Two defects, one line. `passes` is gated on `score >= threshold`, so a floor of
    `0` declared no floor at all: a wrong artifact scores 0.0 and passed. And the
    value was read with a bare `float(...)`, so `pass_threshold: high` raised out
    of `check_extraction`, through `Runner._validate_extract`, into the run's
    `try:` — where the handler set `meta.status = "failed"` and re-raised, killing
    the run after the model had been paid.

    Both artifacts are graded against each value. The right one proves a bad floor
    is refused even when the worker was perfect — the only thing that can fail it
    is the floor itself. The wrong one is the original fail-open repro.
    """

    ARTIFACTS = (
        ("right", _RIGHT),
        ("wrong", _WRONG),
    )

    # Every value the issue calls out, plus the shapes a YAML author reaches for.
    REFUSED = (0, 0.0, -1, -0.5, False, True, "off", "high", "50%", "0.5.0")

    def test_a_refused_floor_fails_the_run_and_is_reported(self):
        for value in self.REFUSED:
            for name, artifact in self.ARTIFACTS:
                with self.subTest(pass_threshold=value, artifact=name):
                    report = check_extraction(
                        dict(METADATA, pass_threshold=value), artifact
                    )
                    self.assertFalse(report["passes"])
                    self.assertTrue(
                        any("pass_threshold" in e for e in report["errors"]),
                        report["errors"],
                    )

    def test_a_bool_is_refused_rather_than_coerced(self):
        """`float(True) == 1.0`, so `pass_threshold: yes` worked by accident.

        `no` became 0.0 and passed everything. Asserting `passes is False` alone
        would not catch this: a perfect artifact clears a floor of 1.0 anyway, so
        the refusal has to be visible in the errors.
        """
        report = check_extraction(dict(METADATA, pass_threshold=True), _RIGHT)
        self.assertFalse(report["passes"])
        self.assertTrue(any("bool" in e for e in report["errors"]), report["errors"])

    def test_a_malformed_floor_never_raises(self):
        """The read is reached on every parseable artifact, so it must be total.

        `1j`, a list, a dict, and an arbitrary object all defeat `float()` with
        TypeError; the strings defeat it with ValueError. Each used to propagate
        out of the grader and cost the run its record.
        """
        for value in ("off", "high", "50%", [], {}, (), object(), 1j):
            with self.subTest(pass_threshold=value):
                report = check_extraction(
                    dict(METADATA, pass_threshold=value), _RIGHT
                )
                self.assertFalse(report["passes"])

    def test_a_nan_floor_is_reported_rather_than_passed_through(self):
        """`score >= nan` is False, so a NaN fails closed — but silently."""
        report = check_extraction(
            dict(METADATA, pass_threshold=float("nan")), _RIGHT
        )
        self.assertFalse(report["passes"])
        self.assertTrue(any("pass_threshold" in e for e in report["errors"]), report["errors"])

    def test_an_empty_yaml_value_is_not_declared(self):
        """`pass_threshold:` with nothing after it loads as None, not as 0.

        The blank-value slip is ordinary authoring, so it takes the default rather
        than failing the run. Loaded through `yaml.safe_load` on purpose: a
        hand-built `{"pass_threshold": None}` would not prove the YAML path.
        """
        spec = yaml.safe_load(
            "type: extract\n"
            "metadata:\n"
            "  expected:\n"
            "    name: Ada\n"
            "    age: 36\n"
            "    vip: true\n"
            "    tier: gold\n"
            "  pass_threshold:\n"
        )
        self.assertIsNone(spec["metadata"]["pass_threshold"])
        report = check_extraction(spec["metadata"], _RIGHT)
        self.assertTrue(report["passes"])
        self.assertFalse(
            any("pass_threshold" in e for e in report["errors"]), report["errors"]
        )

    def test_an_absent_floor_is_the_default_too(self):
        report = check_extraction(METADATA, _RIGHT)
        self.assertTrue(report["passes"])
        self.assertFalse(
            any("pass_threshold" in e for e in report["errors"]), report["errors"]
        )

    def test_a_floor_below_one_is_accepted_and_not_reported(self):
        """The fix refuses a bad floor; it must not start refusing good ones."""
        md = dict(METADATA, pass_threshold=0.5)
        obj = dict(METADATA["expected"], tier="silver")
        report = check_extraction(md, json.dumps(obj))
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 0.75)
        self.assertFalse(
            any("pass_threshold" in e for e in report["errors"]), report["errors"]
        )

    def test_a_quoted_number_is_still_read_as_a_floor(self):
        """`pass_threshold: "0.5"` worked before this change and is not a typo.

        It is a `str`, not a score, but `float()` reads it and the value is inside
        (0, 1]. Refusing every string would fail a spec that is doing what it says.
        """
        md = dict(METADATA, pass_threshold="0.5")
        obj = dict(METADATA["expected"], tier="silver")
        report = check_extraction(md, json.dumps(obj))
        self.assertTrue(report["passes"])
        self.assertFalse(
            any("pass_threshold" in e for e in report["errors"]), report["errors"]
        )

    def test_a_floor_above_one_needs_no_refusal_and_still_fails_closed(self):
        """A score is a fraction, so it cannot clear 2.0 — the run fails anyway."""
        report = check_extraction(
            dict(METADATA, pass_threshold=2.0), _RIGHT
        )
        self.assertFalse(report["passes"])
        self.assertEqual(report["score"], 1.0)


class TestUnanchoredContract(unittest.TestCase):
    """A declared field that nothing grades must not score like a graded one."""

    def test_absent_field_no_longer_scores_one(self):
        """The DUK-94 repro: an empty artifact passed a contract that graded nothing."""
        report = check_extraction(UNANCHORED, "{}")
        self.assertFalse(report["checks"]["contract_anchored"])
        self.assertFalse(report["passes"])
        self.assertEqual(report["score"], 0.0)

    def test_fabricated_value_no_longer_scores_like_an_absent_one(self):
        """A right-typed fabrication used to be indistinguishable from absence."""
        absent = check_extraction(UNANCHORED, "{}")
        fabricated = check_extraction(UNANCHORED, '{"total": 99999}')
        self.assertEqual(absent["passes"], fabricated["passes"])
        self.assertFalse(fabricated["passes"])
        self.assertFalse(fabricated["checks"]["contract_anchored"])

    def test_empty_contract_fails_closed(self):
        report = check_extraction({}, "{}")
        self.assertFalse(report["checks"]["contract_anchored"])
        self.assertFalse(report["passes"])
        self.assertEqual(report["score"], 0.0)

    def test_error_names_the_offending_field_and_the_fix(self):
        report = check_extraction(UNANCHORED, "{}")
        detail = " ".join(report["errors"])
        self.assertIn("total", detail)
        self.assertIn("required: true", detail)

    def test_decorative_field_beside_a_graded_one_still_fails(self):
        """`a` anchors the contract, so the audit is clean — `b` is still ungraded."""
        md = {"fields": {"a": {"type": "str", "required": True}, "b": {"type": "int"}},
              "expected": {"a": "x"}}
        for artifact in ('{"a": "x"}', '{"a": "x", "b": 7}'):
            report = check_extraction(md, artifact)
            self.assertFalse(report["checks"]["contract_anchored"], artifact)
            self.assertFalse(report["passes"], artifact)

    def test_zero_threshold_cannot_rescue_an_unanchored_contract(self):
        md = dict(UNANCHORED, pass_threshold=0.0)
        self.assertFalse(check_extraction(md, '{"total": 1}')["passes"])

    def test_a_graded_field_is_enough(self):
        md = {"fields": {"total": {"type": "number", "required": True}}}
        report = check_extraction(md, '{"total": 1}')
        self.assertTrue(report["checks"]["contract_anchored"])
        self.assertTrue(report["passes"])

    def test_contract_anchoring_is_independent_of_the_artifact(self):
        """A parse failure must not be reported as a contract that grades nothing."""
        report = check_extraction(METADATA, "I cannot help with that.")
        self.assertFalse(report["parsed"])
        self.assertTrue(report["checks"]["contract_anchored"])
        self.assertNotIn("contract_anchored", " ".join(report["errors"]))
        self.assertFalse(report["passes"])


class TestMalformedContract(unittest.TestCase):
    """A wrong-shaped contract is the spec author's error, not a lost run.

    The contract is read before the artifact is parsed, so a non-mapping reaches
    every run. Before this, `fields` as a list raised `AttributeError` on any
    parseable artifact and, once the read moved ahead of the parse, on unparseable
    ones too — losing the run record over a typo.
    """

    WRONG_SHAPES = (["total"], "total", 7, None)

    def test_non_mapping_fields_never_raise(self):
        for value in self.WRONG_SHAPES:
            for artifact in ('{"total": 1}', "not json at all", "{}", "[1, 2]"):
                with self.subTest(fields=value, artifact=artifact):
                    report = check_extraction({"fields": value}, artifact)
                    self.assertFalse(report["passes"])

    def test_non_mapping_expected_never_raises(self):
        for value in ("total", 7, ["a"], None):
            for artifact in ('{"a": 1}', "not json at all"):
                with self.subTest(expected=value, artifact=artifact):
                    report = check_extraction({"expected": value}, artifact)
                    self.assertFalse(report["passes"])

    def test_malformation_is_reported_not_swallowed(self):
        report = check_extraction({"fields": ["total"]}, "{}")
        self.assertTrue(any("not a mapping" in e for e in report["errors"]), report["errors"])

    def test_malformed_expected_cannot_leave_a_presence_only_pass(self):
        """A required field plus an unreadable `expected` is the DUK-94 fail-open.

        `_as_mapping` drops the malformed value, so the contract quietly degrades
        to a presence check: `{"name": "Eve"}` scored 1.0 and passed even though the
        author declared value grades that never ran. The malformation is still
        reported, and it must also stop the pass.
        """
        # `None` is excluded: it means "not declared", so it is not a dropped grade.
        for value in [v for v in self.WRONG_SHAPES if v is not None]:
            for artifact in ('{"name": "Eve"}', '{"name": "Anyone At All"}'):
                with self.subTest(expected=value, artifact=artifact):
                    md = {
                        "fields": {"name": {"type": "str", "required": True}},
                        "expected": value,
                    }
                    report = check_extraction(md, artifact)
                    self.assertFalse(report["passes"])
                    self.assertFalse(report["checks"]["contract_anchored"])
                    self.assertEqual(report["score"], 0.0)
                    self.assertTrue(
                        any("metadata.expected" in e and "not a mapping" in e
                            for e in report["errors"]),
                        report["errors"],
                    )

    def test_well_formed_required_only_contract_still_passes(self):
        """The fix targets a dropped `expected`, not the deliberate schema-only form."""
        md = {"fields": {"name": {"type": "str", "required": True}}}
        report = check_extraction(md, '{"name": "Eve"}')
        self.assertTrue(report["checks"]["contract_anchored"])
        self.assertTrue(report["passes"])

    def test_numeric_field_name_reports_instead_of_raising(self):
        """YAML keeps `7:` as an int key; joining the names raised TypeError.

        The raise escaped `check_extraction`, so the malformed contract produced no
        report at all — the run record the finding exists to produce was lost.
        """
        report = check_extraction({"fields": {7: {"type": "number"}}}, "{}")
        self.assertFalse(report["passes"])
        self.assertFalse(report["checks"]["contract_anchored"])
        detail = " ".join(report["errors"])
        self.assertIn("declares 7", detail)
        self.assertIn("required: true", detail)

    def test_absent_key_is_not_called_a_malformation(self):
        """`fields: null` and a missing key both mean 'not declared'."""
        for metadata in ({"fields": None}, {}, {"fields": {}}):
            with self.subTest(metadata=metadata):
                report = check_extraction(metadata, "{}")
                self.assertFalse(any("not a mapping" in e for e in report["errors"]))

    def test_dropped_fields_still_fails_closed_on_nothing_to_grade(self):
        """A malformed `fields` plus no `expected` grades nothing, so it must fail."""
        report = check_extraction({"fields": ["total"]}, "{}")
        self.assertFalse(report["checks"]["contract_anchored"])
        self.assertEqual(report["score"], 0.0)

    def test_dropped_fields_leaves_expected_as_the_anchor(self):
        """A malformed `fields` must not also discard a usable `expected`."""
        report = check_extraction({"fields": "oops", "expected": {"a": "x"}}, '{"a": "x"}')
        self.assertTrue(report["checks"]["contract_anchored"])
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 1.0)



class TestExtractRunner(unittest.TestCase):
    def test_dry_run_passes_and_writes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"),
            )
            run_dir = Path(meta.run_dir)
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)
            artifact = run_dir / "artifact.json"
            self.assertTrue(artifact.exists())
            self.assertEqual(json.loads(artifact.read_text())["name"], "Ada")

    def test_live_correct_extraction_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = json.dumps(METADATA["expected"])
            client = _FakeClient(payloads=[good])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)

    def test_live_partial_extraction_fails_with_partial_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            partial = json.dumps(dict(METADATA["expected"], tier="silver"))
            client = _FakeClient(payloads=[partial])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertFalse(meta.passes)
            self.assertAlmostEqual(meta.score, 0.75)

    def test_orchestrator_pick_decides_score(self):
        """Orchestrator picks candidate 1 — its worse extraction scores."""
        with tempfile.TemporaryDirectory() as tmp:
            good = json.dumps(METADATA["expected"])
            bad = json.dumps({"name": "Ada", "age": 36, "vip": True, "tier": "bronze"})
            client = _FakeClient(payloads=[good, bad], pick=1)
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertFalse(meta.passes)
            self.assertAlmostEqual(meta.score, 0.75)
            self.assertEqual(
                json.loads((Path(meta.run_dir) / "artifact.json").read_text())["tier"], "bronze"
            )

    def test_unparseable_candidate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(payloads=["sorry, I can't extract that"])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertFalse(meta.passes)
            self.assertIsNone(meta.score)

    def test_unanchored_contract_fails_through_the_runner(self):
        """A perfect extraction still fails when the contract grades nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            task = _task(metadata={"fields": {"total": {"type": "number"}}})
            client = _FakeClient(payloads=['{"total": 99999}'])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(task, _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertFalse(meta.passes)
            self.assertEqual(meta.score, 0.0)
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertFalse(report["checks"]["contract_anchored"])


    def test_a_malformed_floor_fails_the_run_without_destroying_it(self):
        """DUK-238: a typo in one spec used to cost the whole run.

        The bare `float(...)` raised out of `check_extraction`, through
        `Runner._validate_extract`, into the run's `try:` — where the handler set
        `meta.status = "failed"`, logged `run.failed`, and re-raised, after the
        model had been paid. The run must instead finish with a report that names
        the bad key, and it must finish rather than blow up.
        """
        with tempfile.TemporaryDirectory() as tmp:
            task = _task(metadata=dict(METADATA, pass_threshold="high"))
            client = _FakeClient(payloads=[_RIGHT])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(task, _model("org/x", "orchestrator"), _model("wrk/ex", "worker"))
            self.assertEqual(meta.status, "finished")
            self.assertFalse(meta.passes)
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertEqual(report["score"], 1.0)
            self.assertTrue(
                any("pass_threshold" in e for e in report["errors"]), report["errors"]
            )


if __name__ == "__main__":
    unittest.main()
