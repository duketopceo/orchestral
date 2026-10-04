"""Tests for judge_contract v2 — per-criterion {satisfied, supported, evidence}.

The wandr triple: a satisfied claim counts only when the judge quotes
verbatim artifact text that verifies it. Secret-bearing criteria never
reach the LLM — the rubric embeds the answer key — and grade
mechanically instead.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.judge import (
    JUDGE_CONTRACT,
    judge_artifact,
    judge_cache_key,
    task_criteria,
)
from orchestral.storage import JUDGE_CACHE_SCHEMA, RunStore


def _model(slug: str, role: str = "worker") -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.1, output_price_per_mtok=0.4)


def _client(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "latency_ms": 100,
        "id": "x",
    }
    return client


def _judge(task: TaskSpec, client: MagicMock, artifact: str = "<html>ok</html>"):
    return judge_artifact(
        logger=MagicMock(), step=1, task=task, artifact=artifact,
        judge=_model("j/m", "judge"), client=client, dry_run=False,
    )


class TestTaskCriteria(unittest.TestCase):
    def test_default_single_criterion(self):
        spec = TaskSpec(id="t", type="html", prompt="make a page")
        criteria = task_criteria(spec)
        self.assertEqual(len(criteria), 1)
        self.assertEqual(criteria[0]["id"], "task")
        self.assertEqual(criteria[0]["rubric"], "make a page")
        self.assertFalse(criteria[0]["secret"])

    def test_explicit_string_and_dict_entries(self):
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "criteria": [
                "has a working nav",
                {"id": "cta", "rubric": "primary CTA is visible"},
            ],
        })
        criteria = task_criteria(spec)
        self.assertEqual([c["id"] for c in criteria], ["c1", "cta"])
        self.assertEqual(criteria[1]["rubric"], "primary CTA is visible")

    def test_secret_flag_by_value_membership(self):
        # the rubric reproduces a graded metadata value — key name
        # doesn't matter, the string itself is the tell
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "expected_answer": "HunterX-4271-alpha",
            "criteria": [
                {"id": "ok", "rubric": "layout is clean"},
                {"id": "key", "rubric": "answer contains HunterX-4271-alpha"},
            ],
        })
        criteria = task_criteria(spec)
        self.assertFalse(criteria[0]["secret"])
        self.assertTrue(criteria[1]["secret"])

    def test_holdout_required_marks_secret(self):
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "holdout": True,
            "required": ["seekrit-token-9934"],
            "criteria": [{"id": "s", "rubric": "includes seekrit-token-9934"}],
        })
        self.assertTrue(task_criteria(spec)[0]["secret"])

    def test_public_required_and_forbidden_mark_secret(self):
        # required/forbidden are answer keys on *any* spec — a rubric
        # embedding one must be withheld whether or not the spec is a
        # holdout arm
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "required": ["FALCON-4417-open"],
            "forbidden": ["lorem-ipsum-token-99"],
            "criteria": [
                {"id": "req", "rubric": "output includes FALCON-4417-open"},
                {"id": "fb", "rubric": "output avoids lorem-ipsum-token-99"},
                {"id": "open", "rubric": "layout is clean"},
            ],
        })
        criteria = task_criteria(spec)
        self.assertTrue(criteria[0]["secret"])
        self.assertTrue(criteria[1]["secret"])
        self.assertFalse(criteria[2]["secret"])

    def test_cache_key_changes_with_criteria(self):
        a = TaskSpec(id="t", type="html", prompt="p")
        b = TaskSpec(id="t", type="html", prompt="p",
                     metadata={"criteria": ["different rubric"]})
        self.assertNotEqual(judge_cache_key(a, b"x"), judge_cache_key(b, b"x"))
        self.assertEqual(JUDGE_CACHE_SCHEMA, 3)


class TestMergeCriteria(unittest.TestCase):
    def test_verbatim_evidence_is_supported(self):
        artifact = "<html><h1>Welcome to Coffee</h1></html>"
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "criteria": [{"id": "title", "rubric": "has a title"}],
        })
        client = _client(
            '{"score": 0.9, "passed": true, "reasoning": "ok",'
            ' "criteria": [{"id": "title", "satisfied": true,'
            '  "evidence": "Welcome to Coffee"}]}'
        )
        result, _ = _judge(spec, client, artifact)
        (crit,) = result["criteria"]
        self.assertTrue(crit["satisfied"])
        self.assertTrue(crit["supported"])
        self.assertEqual(crit["evidence"], ["Welcome to Coffee"])
        self.assertEqual(result["unsupported_criteria"], 0)

    def test_hallucinated_evidence_is_flagged_not_trusted(self):
        artifact = "<html><h1>Welcome to Coffee</h1></html>"
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "criteria": [{"id": "title", "rubric": "has a title"}],
        })
        client = _client(
            '{"score": 0.9, "passed": true, "reasoning": "ok",'
            ' "criteria": [{"id": "title", "satisfied": true,'
            '  "evidence": "This text is not in the artifact"}]}'
        )
        result, _ = _judge(spec, client, artifact)
        (crit,) = result["criteria"]
        self.assertTrue(crit["satisfied"])
        self.assertFalse(crit["supported"])  # cited, but not verifiable
        self.assertEqual(result["unsupported_criteria"], 1)

    def test_v1_shape_response_is_unassessed_not_flagged(self):
        spec = TaskSpec(id="t", type="html", prompt="p")
        client = _client('{"score": 0.8, "passed": true, "reasoning": "nice"}')
        result, _ = _judge(spec, client)
        (crit,) = result["criteria"]
        self.assertIsNone(crit["satisfied"])
        self.assertIsNone(crit["supported"])
        self.assertEqual(crit["note"], "no criterion verdict in judge response")
        self.assertEqual(result["unsupported_criteria"], 0)

    def test_headline_fields_preserved(self):
        spec = TaskSpec(id="t", type="html", prompt="p")
        client = _client('{"score": 0.8, "passed": true, "reasoning": "nice"}')
        result, _ = _judge(spec, client)
        for key in ("score", "passed", "reasoning", "model", "judge_contract"):
            self.assertIn(key, result)
        self.assertEqual(result["judge_contract"], JUDGE_CONTRACT)


class TestSecretCanary(unittest.TestCase):
    """The answer key must never reach the judge — not in the prompt,
    not as a logged criterion rubric."""

    def test_secret_rubric_never_reaches_llm(self):
        secret = "seekrit-token-9934-x"
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "holdout": True,
            "required": [secret],
            "criteria": [
                {"id": "open", "rubric": "layout is clean"},
                {"id": "key", "rubric": f"output includes {secret}"},
            ],
        })
        artifact = f"<html>clean {secret}</html>"
        client = _client(
            '{"score": 0.9, "passed": true, "reasoning": "ok",'
            ' "criteria": [{"id": "open", "satisfied": true,'
            '  "evidence": "clean"}]}'
        )
        result, _ = _judge(spec, client, artifact)
        # canary: the secret rubric never reaches the judge as an
        # instruction — the artifact legitimately carries the string
        # (it's worker output), the criteria block must not
        prompt = client.chat.call_args.kwargs["messages"][1]["content"]
        criteria_section = prompt.split("Criteria:", 1)[1]
        self.assertNotIn(secret, criteria_section)
        self.assertNotIn("key:", criteria_section)
        # and the result entry withholds the rubric, graded mechanically
        key_entry = next(c for c in result["criteria"] if c["id"] == "key")
        self.assertNotIn("rubric", key_entry)
        self.assertEqual(key_entry["engine"], "mechanical")
        self.assertTrue(key_entry["satisfied"])  # artifact contains it
        self.assertIsNone(key_entry["supported"])
        open_entry = next(c for c in result["criteria"] if c["id"] == "open")
        self.assertTrue(open_entry["satisfied"])
        self.assertTrue(open_entry["supported"])

    def test_secret_absent_from_artifact_fails_mechanically(self):
        secret = "seekrit-token-9934-x"
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "holdout": True,
            "required": [secret],
            "criteria": [{"id": "key", "rubric": f"output includes {secret}"}],
        })
        client = _client('{"score": 0.5, "passed": false, "reasoning": "x"}')
        result, _ = _judge(spec, client, "<html>nothing here</html>")
        (entry,) = result["criteria"]
        self.assertFalse(entry["satisfied"])

    def test_forbidden_secret_grades_by_absence(self):
        # a rubric embedding a metadata.forbidden value is an *absence*
        # requirement — satisfied iff the artifact avoids the token
        bad = "lorem-ipsum-token-99"
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "forbidden": [bad],
            "criteria": [{"id": "fb", "rubric": f"output avoids {bad}"}],
        })
        client = _client('{"score": 0.5, "passed": false, "reasoning": "x"}')
        result, _ = _judge(spec, client, "<html>clean prose</html>")
        self.assertTrue(result["criteria"][0]["satisfied"])
        result, _ = _judge(spec, client, f"<html>uh oh {bad}</html>")
        self.assertFalse(result["criteria"][0]["satisfied"])

    def test_forbidden_pattern_grades_by_regex_absence(self):
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "forbidden_pattern": "lorem-ipsum!+",
            "criteria": [{"id": "fb", "rubric": "output avoids lorem-ipsum!+"}],
        })
        client = _client('{"score": 0.5, "passed": false, "reasoning": "x"}')
        result, _ = _judge(spec, client, "<html>calm prose</html>")
        self.assertTrue(result["criteria"][0]["satisfied"])
        result, _ = _judge(spec, client, "<html>lorem-ipsum!!</html>")
        self.assertFalse(result["criteria"][0]["satisfied"])


class TestDerivedVerdict(unittest.TestCase):
    """v2 hard verdict: when every open criterion is assessed, the
    headline pass/score derives from the criteria — the judge's scalar
    claim is retained as claimed_* for disagreement analysis."""

    def _spec(self):
        return TaskSpec(id="t", type="html", prompt="p", metadata={
            "criteria": [
                {"id": "a", "rubric": "has heading"},
                {"id": "b", "rubric": "has footer"},
            ],
        })

    def _reply(self, score, passed, criteria):
        return json.dumps({"score": score, "passed": passed,
                           "reasoning": "r", "criteria": criteria})

    def test_all_supported_derives_pass(self):
        artifact = "<h1>x</h1><footer>y</footer>"
        result, _ = _judge(self._spec(), _client(self._reply(0.9, True, [
            {"id": "a", "satisfied": True, "evidence": "<h1>x</h1>"},
            {"id": "b", "satisfied": True, "evidence": "<footer>y</footer>"},
        ])), artifact=artifact)
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(result["claimed_passed"], True)
        self.assertEqual(result["claimed_score"], 0.9)

    def test_unsupported_claim_overrides_judge_pass(self):
        # the judge claims pass and claims criterion b satisfied, but the
        # quoted evidence isn't in the artifact — the derived verdict fails
        result, _ = _judge(self._spec(), _client(self._reply(0.9, True, [
            {"id": "a", "satisfied": True, "evidence": "<h1>x</h1>"},
            {"id": "b", "satisfied": True, "evidence": "fabricated quote"},
        ])), artifact="<h1>x</h1>")
        self.assertFalse(result["passed"])
        self.assertEqual(result["unsupported_criteria"], 1)
        self.assertTrue(result["claimed_passed"])  # disagreement recorded

    def test_partial_satisfaction_derives_fractional_score(self):
        artifact = "<h1>x</h1><footer>y</footer>"
        result, _ = _judge(self._spec(), _client(self._reply(0.9, True, [
            {"id": "a", "satisfied": True, "evidence": "<h1>x</h1>"},
            {"id": "b", "satisfied": False, "evidence": ""},
        ])), artifact=artifact)
        self.assertFalse(result["passed"])
        self.assertEqual(result["score"], 0.5)

    def test_v1_shape_reply_keeps_scalar_verdict(self):
        # a judge that ignores the criteria block still yields its scalar
        # verdict — no criteria assessed means nothing to derive from
        result, _ = _judge(self._spec(), _client(
            '{"score": 0.8, "passed": true, "reasoning": "r"}'))
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 0.8)
        self.assertNotIn("claimed_passed", result)
        self.assertEqual(result["criteria_rollup"]["unassessed"], 2)

    def test_rollup_counts(self):
        artifact = "<h1>x</h1>"
        result, _ = _judge(self._spec(), _client(self._reply(0.5, False, [
            {"id": "a", "satisfied": True, "evidence": "<h1>x</h1>"},
            {"id": "b", "satisfied": True, "evidence": "made up"},
        ])), artifact=artifact)
        roll = result["criteria_rollup"]
        self.assertEqual(roll["total"], 2)
        self.assertEqual(roll["satisfied"], 2)
        self.assertEqual(roll["supported"], 1)
        self.assertEqual(roll["unsupported"], 1)
        self.assertEqual(roll["unassessed"], 0)

    def test_inconclusive_scalar_blocks_derivation(self):
        # a null/garbage scalar verdict marks the result inconclusive —
        # deriving a concrete pass/fail over it would record a verdict
        # next to a "no answer" flag
        result, _ = _judge(self._spec(), _client(self._reply(0.5, "maybe", [
            {"id": "a", "satisfied": True, "evidence": "<h1>x</h1>"},
            {"id": "b", "satisfied": True, "evidence": "<footer>y</footer>"},
        ])), artifact="<h1>x</h1><footer>y</footer>")
        self.assertTrue(result["inconclusive"])
        self.assertIsNone(result["passed"])
        self.assertNotIn("claimed_passed", result)
        self.assertEqual(result["criteria_rollup"]["satisfied"], 2)

    def test_derived_pass_respects_secret_mechanical(self):
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "expected_answer": "sekret-4242-xyz",
            "criteria": [
                {"id": "a", "rubric": "has heading"},
                {"id": "k", "rubric": "contains sekret-4242-xyz"},
            ],
        })
        # open criterion satisfied+supported but the artifact lacks the
        # secret — mechanical failure must veto the derived pass
        result, _ = _judge(spec, _client(self._reply(1.0, True, [
            {"id": "a", "satisfied": True, "evidence": "<h1>x</h1>"},
        ])), artifact="<h1>x</h1>")
        self.assertFalse(result["passed"])
        key = next(c for c in result["criteria"] if c["id"] == "k")
        self.assertFalse(key["satisfied"])
        self.assertEqual(key["engine"], "mechanical")


class TestDecisionsCriteria(unittest.TestCase):
    def test_decisions_engine_reports_unavailable_not_fabricated(self):
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "criteria": [{"id": "c1", "rubric": "clean"}],
        })
        judge = _model("~typesafe/jev-latest", "judge")
        client = MagicMock()
        client.decide.return_value = {
            "answers": {"verdict": {"noul": 0.9}, "quality": {"score": 3}},
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.001},
            "id": "d",
        }
        result, _ = judge_artifact(
            logger=MagicMock(), step=1, task=spec,
            artifact="<html>x</html>", judge=judge, client=client,
            dry_run=False,
        )
        (crit,) = result["criteria"]
        self.assertEqual(crit["engine"], "decisions")
        self.assertIsNone(crit["satisfied"])
        self.assertIsNone(crit["supported"])
        self.assertIn("unavailable", crit["note"])
        self.assertEqual(result["judge_contract"], JUDGE_CONTRACT)

    def test_decisions_secret_graded_mechanically(self):
        # the decisions engine can't assess a rubric it was never shown —
        # secret criteria on this path still grade mechanically, not
        # as unassessed entries under a mechanical label
        secret = "sekrit-deploy-7788"
        spec = TaskSpec(id="t", type="html", prompt="p", metadata={
            "required": [secret],
            "criteria": [
                {"id": "open", "rubric": "clean layout"},
                {"id": "key", "rubric": f"output includes {secret}"},
            ],
        })
        judge = _model("~typesafe/jev-latest", "judge")
        client = MagicMock()
        client.decide.return_value = {
            "answers": {"verdict": {"noul": 0.9}, "quality": {"score": 3}},
            "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.001},
            "id": "d",
        }
        result, _ = judge_artifact(
            logger=MagicMock(), step=1, task=spec,
            artifact=f"<html>{secret}</html>", judge=judge, client=client,
            dry_run=False,
        )
        key = next(c for c in result["criteria"] if c["id"] == "key")
        self.assertEqual(key["engine"], "mechanical")
        self.assertTrue(key["satisfied"])
        self.assertTrue(key["secret"])
        self.assertNotIn("rubric", key)


class TestCacheInvalidation(unittest.TestCase):
    def test_v2_cached_result_goes_cold(self):
        """A pre-v3 cached verdict (no criteria array) must not be
        served — schema v3 rows only."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO judge_cache VALUES (?, ?, ?, ?, ?)",
                    ("t", "j/m", "sha",
                     json.dumps({"schema": 2, "result": {"score": 0.9}}),
                     "2025-01-01"),
                )
            self.assertIsNone(store.get_judge_result("t", "j/m", "sha"))


if __name__ == "__main__":
    unittest.main()
