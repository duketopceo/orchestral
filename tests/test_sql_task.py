"""Tests for the sql task type — read-only sqlite execution validator."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.sqlexec import build_fixture, extract_sql, run_readonly_query, run_sql_check

SCHEMA = [
    "CREATE TABLE t (id INTEGER PRIMARY KEY, grp TEXT, val REAL)",
]
SEED = [
    "INSERT INTO t VALUES (1, 'a', 10.0), (2, 'a', 20.0), (3, 'b', 5.0)",
]
REFERENCE = "SELECT grp, SUM(val) AS total FROM t GROUP BY grp ORDER BY grp"


def _metadata(**overrides):
    md = {"schema": SCHEMA, "seed": SEED, "reference_sql": REFERENCE}
    md.update(overrides)
    return md


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "sql-test",
        "type": "sql",
        "prompt": "Total value per group, ordered by group.",
        "metadata": _metadata(),
    }
    base.update(kwargs)
    return TaskSpec(**base)


class _FakeClient:
    """Chat stand-in: plan → two subtasks, workers → canned SQL, pick → index 0."""

    def __init__(self, queries: list[str]):
        self.queries = queries
        self.worker_calls = 0

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = self.queries[min(self.worker_calls, len(self.queries) - 1)]
            self.worker_calls += 1
        elif "candidates" in data:
            body = json.dumps({"subtask_id": 0})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [
                {"id": 0, "description": "first query"},
                {"id": 1, "description": "second query"},
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


class TestExtractSql(unittest.TestCase):
    def test_dict_query_key(self):
        self.assertEqual(extract_sql({"query": "SELECT 1"}), "SELECT 1")

    def test_dict_content_key(self):
        self.assertEqual(extract_sql({"content": "SELECT 2"}), "SELECT 2")

    def test_fenced_block(self):
        self.assertEqual(extract_sql("Here:\n```sql\nSELECT 3\n```"), "SELECT 3")

    def test_raw_string(self):
        self.assertEqual(extract_sql("  SELECT 4  "), "SELECT 4")

    def test_empty(self):
        self.assertEqual(extract_sql({}), "")
        self.assertEqual(extract_sql(None), "")


class TestRunSqlCheck(unittest.TestCase):
    def test_matching_query_scores_one(self):
        report = run_sql_check(_metadata(), REFERENCE)
        self.assertTrue(report["executed"])
        self.assertTrue(report["match"])
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(report["rows_expected"], 2)

    def test_equivalent_query_matches(self):
        report = run_sql_check(
            _metadata(), "SELECT grp, SUM(val) AS total FROM t GROUP BY grp"
        )
        self.assertTrue(report["match"])

    def test_wrong_result_scores_zero_with_got_preview_only(self):
        report = run_sql_check(_metadata(), "SELECT grp, SUM(val) FROM t GROUP BY grp HAVING grp='a'")
        self.assertTrue(report["executed"])
        self.assertFalse(report["match"])
        self.assertEqual(report["score"], 0.0)
        self.assertIn("got_preview", report)
        # the reference rows are the answer key — the report says how many rows
        # a correct answer returns, never which rows those are
        self.assertEqual(report["rows_expected"], 2)
        self.assertNotIn("expected_preview", report)

    def test_invalid_sql_scores_zero(self):
        report = run_sql_check(_metadata(), "SELECT * FROM nope")
        self.assertTrue(report["executed"])
        self.assertEqual(report["score"], 0.0)
        self.assertIn("candidate query failed", report["error"])

    def test_write_attempt_fails_readonly(self):
        report = run_sql_check(_metadata(), "DELETE FROM t")
        self.assertTrue(report["executed"])
        self.assertEqual(report["score"], 0.0)
        self.assertIn("readonly", report["error"].lower())

    def test_missing_reference_is_spec_error(self):
        report = run_sql_check({"schema": SCHEMA, "seed": SEED}, "SELECT 1")
        self.assertFalse(report["executed"])
        self.assertIsNone(report["score"])
        self.assertIn("reference_sql", report["error"])

    def test_broken_reference_is_spec_error(self):
        report = run_sql_check(_metadata(reference_sql="SELECT bogus FROM nowhere"), "SELECT 1")
        self.assertIn("task spec is broken", report["error"])
        self.assertIsNone(report["score"])

    def test_empty_candidate(self):
        report = run_sql_check(_metadata(), "   ")
        self.assertFalse(report["executed"])
        self.assertIn("no SQL", report["error"])

    def test_ordered_flag_controls_row_order(self):
        reversed_rows = "SELECT grp, SUM(val) AS total FROM t GROUP BY grp ORDER BY grp DESC"
        self.assertTrue(run_sql_check(_metadata(), reversed_rows)["match"])
        ordered = run_sql_check(_metadata(ordered=True), reversed_rows)
        self.assertFalse(ordered["match"])

    def test_step_cap_aborts_runaway_query(self):
        rows, err = run_readonly_query(
            _fixture_db(), "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r) SELECT COUNT(*) FROM r",
            max_steps=5,
        )
        self.assertIsNone(rows)
        self.assertIsNotNone(err)


def _fixture_db():
    tmp = tempfile.mkdtemp()
    return build_fixture(Path(tmp), ";\n".join(SCHEMA), ";\n".join(SEED))


class TestSqlRunner(unittest.TestCase):
    def test_dry_run_passes_and_writes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/sql", "worker"),
            )
            run_dir = Path(meta.run_dir)
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)
            artifact = run_dir / "artifact.sql"
            self.assertTrue(artifact.exists())
            self.assertIn("SELECT", artifact.read_text().upper())
            report = json.loads((run_dir / "report.json").read_text())
            self.assertTrue(report["checks"]["executed"])
            self.assertTrue(report["checks"]["matches_reference"])

    def test_live_correct_query_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(queries=[json.dumps({"query": REFERENCE})])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/sql", "worker"))
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)

    def test_live_wrong_query_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(queries=["SELECT 1"])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/sql", "worker"))
            self.assertFalse(meta.passes)
            self.assertEqual(meta.score, 0.0)

    def test_orchestrator_pick_selects_winning_candidate(self):
        """Orchestrator picks subtask 1 — its wrong SQL decides the score."""
        with tempfile.TemporaryDirectory() as tmp:

            class PickSecond(_FakeClient):
                def chat(self, model, messages, max_tokens=4096, temperature=0.4):
                    data = json.loads(messages[-1]["content"])
                    if "candidates" in data:
                        return {
                            "content": json.dumps({"subtask_id": 1}),
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                            "latency_ms": 1, "id": "fake",
                        }
                    return super().chat(model, messages, max_tokens, temperature)

            client = PickSecond(queries=[json.dumps({"query": REFERENCE}), "SELECT 1"])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/sql", "worker"))
            self.assertFalse(meta.passes)
            self.assertEqual(meta.score, 0.0)
            self.assertEqual((Path(meta.run_dir) / "artifact.sql").read_text().strip(), "SELECT 1")


if __name__ == "__main__":
    unittest.main()
