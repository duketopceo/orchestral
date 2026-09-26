"""Tests for the sql task type — read-only sqlite execution validator."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec, load_task
from orchestral.runner import Runner
from orchestral.sqlexec import build_fixture, extract_sql, run_readonly_query, run_sql_check

REPO_TASKS = Path(__file__).resolve().parent.parent / "tasks"

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

    def test_wrong_result_scores_zero_with_previews(self):
        report = run_sql_check(_metadata(), "SELECT grp, SUM(val) FROM t GROUP BY grp HAVING grp='a'")
        self.assertTrue(report["executed"])
        self.assertFalse(report["match"])
        self.assertEqual(report["score"], 0.0)
        self.assertIn("expected_preview", report)
        self.assertIn("got_preview", report)

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

    def test_empty_reference_is_spec_error_not_a_free_pass(self):
        """A reference with no rows would grade any zero-row query as 1.0."""
        empty_ref = "SELECT grp, val FROM t WHERE grp = 'zzz'"
        report = run_sql_check(_metadata(reference_sql=empty_ref), empty_ref)
        self.assertFalse(report["executed"])
        self.assertIsNone(report["score"])
        self.assertIn("no rows", report["error"])
        self.assertIn("task spec is broken", report["error"])

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


# The shipped fixture, re-priced: Kettle at 12.34 and three Kettles on the
# first shipped order. 3 x 12.34 is 37.019999999999996 in binary floating
# point, so the month's winning total is not representable to the cent.
FRACTIONAL_CENT_LINES = (
    "INSERT INTO order_lines VALUES (1, 1, 3, 12.34), (1, 3, 1, 12.5), "
    "(2, 2, 5, 40.0), (3, 3, 1, 12.5), (4, 3, 4, 12.5), (5, 2, 2, 40.0), "
    "(5, 1, 1, 12.34), (6, 1, 9, 12.34)"
)
# The same query with the shipped-status filter dropped: Jan's cancelled order
# adds 5 x 40.0, so the winner changes and the answer no longer matches.
UNFILTERED_SQL = """
    SELECT month, product, revenue FROM (
      SELECT strftime('%Y-%m', o.placed_on) AS month,
             p.name AS product,
             ROUND(SUM(ol.qty * ol.unit_price), 2) AS revenue,
             MAX(ROUND(SUM(ol.qty * ol.unit_price), 2)) OVER (
               PARTITION BY strftime('%Y-%m', o.placed_on)
             ) AS best
      FROM order_lines ol
      JOIN orders o ON o.id = ol.order_id
      JOIN products p ON p.id = ol.product_id
      GROUP BY month, p.name
    ) t
    WHERE revenue = best
    ORDER BY month ASC
"""
# The tie-break the prompt promises, in each direction. The prompt is the only
# thing a candidate reads, so the reference has to answer what the prompt says
# rather than what the query happens to do. DUK-117.
TIE_FIRST_RE = re.compile(r"sorts first|alphabetically first|smallest name", re.I)
TIE_LAST_RE = re.compile(r"sorts last|alphabetically last|largest name", re.I)


class TestShippedMonthlyRevenueSpec(unittest.TestCase):
    """DUK-90 and DUK-117: the shipped reference must answer for any price set.

    The reference compares each month's rounded revenue against the same
    month's maximum. Rounding only one side of that comparison dropped every
    month whose winning total did not equal its own 2-decimal rounding, and
    kept answering normally for the months without that gap. The result is a
    partial answer rather than a total failure, and a non-empty reference
    still grades — which is what the empty-reference check is for.

    Separately, "WHERE revenue = best" returned a row for every product tied
    at the month's maximum, while the prompt promised one row per month — so a
    candidate that broke the tie was graded wrong. The prompt now states the
    tie-break and ROW_NUMBER() picks exactly that winner.
    """

    def setUp(self):
        self.spec = load_task(REPO_TASKS / "sql-monthly-revenue.yaml")
        self.reference = self.spec.metadata["reference_sql"]
        self.repriced = {
            **self.spec.metadata,
            "seed": [*self.spec.metadata["seed"][:2], FRACTIONAL_CENT_LINES],
        }

    def _rows(self, metadata, sql):
        with tempfile.TemporaryDirectory() as tmp:
            db = build_fixture(
                Path(tmp),
                ";\n".join(metadata["schema"]),
                ";\n".join(metadata["seed"]),
            )
            rows, err = run_readonly_query(db, sql)
        self.assertIsNone(err)
        return rows

    def _tied_metadata(self):
        """The shipped fixture with a second January winner.

        1 x 50.0 for Grinder on the 2025-01-20 order puts Grinder level with
        Kettle at 90.0, so January has two products tied for the month maximum.
        Derived from the shipped seed so there is no third copy to drift.
        """
        seed = [
            line + ", (3, 2, 1, 50.0)" if "INSERT INTO order_lines" in line else line
            for line in self.spec.metadata["seed"]
        ]
        self.assertNotEqual(
            seed, self.spec.metadata["seed"], "shipped seed has no order_lines insert"
        )
        return {**self.spec.metadata, "seed": seed}

    def _prompt_tie_direction(self) -> str:
        """Read the tie-break the prompt promises: 'first' or 'last'."""
        prompt = self.spec.prompt
        first, last = bool(TIE_FIRST_RE.search(prompt)), bool(TIE_LAST_RE.search(prompt))
        self.assertNotEqual(
            first,
            last,
            f"prompt must state exactly one tie-break direction, got first={first} last={last}",
        )
        return "first" if first else "last"

    def test_reference_answers_the_shipped_fixture(self):
        report = run_sql_check(self.spec.metadata, self.reference)
        self.assertTrue(report["executed"])
        self.assertTrue(report["match"])
        self.assertEqual(report["rows_expected"], 2)
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(
            self._rows(self.spec.metadata, self.reference),
            [("2025-01", "Kettle", 90.0), ("2025-02", "Grinder", 80.0)],
        )

    def test_reference_answers_fractional_cent_prices(self):
        report = run_sql_check(self.repriced, self.reference)
        self.assertTrue(report["executed"])
        self.assertTrue(report["match"])
        self.assertEqual(report["rows_expected"], 2)
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(
            self._rows(self.repriced, self.reference),
            [("2025-01", "Kettle", 37.02), ("2025-02", "Grinder", 80.0)],
        )

    def test_wrong_query_still_fails_on_fractional_cent_prices(self):
        report = run_sql_check(self.repriced, UNFILTERED_SQL)
        self.assertTrue(report["executed"])
        self.assertFalse(report["match"])
        self.assertEqual(report["score"], 0.0)

    def test_prompt_states_a_tie_break(self):
        self.assertEqual(self._prompt_tie_direction(), "first")

    def test_reference_tie_break_matches_the_prompt(self):
        """The prompt and the reference must agree on a tied month.

        The expected winner is derived from the direction the prompt states, so
        editing either side alone fails here: a prompt promising "sorts last"
        expects Kettle, and a reference still sorting names first answers
        Grinder. Reverting the reference to "WHERE revenue = best" fails too —
        it returns a row per tied product.
        """
        tied = self._tied_metadata()
        winner = {"first": "Grinder", "last": "Kettle"}[self._prompt_tie_direction()]
        self.assertEqual(
            self._rows(tied, self.reference),
            [("2025-01", winner, 90.0), ("2025-02", "Grinder", 80.0)],
        )

    def test_tied_month_yields_one_row_per_month(self):
        """One row per month even when two products tie for the maximum."""
        rows = self._rows(self._tied_metadata(), self.reference)
        months = {month for month, _, _ in rows}
        self.assertEqual(months, {"2025-01", "2025-02"})
        self.assertEqual(len(rows), len(months))

    def test_the_other_tie_break_scores_zero(self):
        """A candidate that breaks the tie the other way is graded wrong."""
        wrong = self.reference.replace("p.name ASC", "p.name DESC")
        self.assertNotEqual(
            wrong, self.reference, "reference no longer breaks ties by product name"
        )
        report = run_sql_check(self._tied_metadata(), wrong)
        self.assertTrue(report["executed"])
        self.assertFalse(report["match"])
        self.assertEqual(report["rows_got"], 2)
        self.assertEqual(report["score"], 0.0)


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
