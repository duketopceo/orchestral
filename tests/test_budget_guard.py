"""Regression tests for the billed-cost budget guard.

The bug these lock down: `.github/workflows/orchestral.yml`'s `Enforce cost
budget` step compared the harness's configured rate-card estimate against
`MAX_COST_USD`. On 2026-09-26 the estimate understated provider-billed spend by
5.3x, so the cap never bound. The guard now reads `calls.api_cost_usd`.

The first test replays the measured failure exactly: four runs from a real
two-minute window in which the newest run row carried `total_cost_usd = 0.0`
while $0.32 was actually billed.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from orchestral.budget import (
    EXIT_OK,
    EXIT_OVER_BUDGET,
    EXIT_UNAUDITABLE,
    check_budget,
    main,
    read_billed_cost,
)
from orchestral.storage import RunMeta, RunStore

# The four runs created in the 2026-09-26T08:14-08:16 window of the local
# ledger, as measured: (run_id, started_at, status, estimate, billed, calls).
# The newest run is a *failed* one whose `runs.total_cost_usd` is 0.0.
MEASURED_WINDOW = [
    ("c939566c9e8b", "2026-09-26T08:14:20.434203+00:00", "finished", 0.008666, 0.078821, 5),
    ("ad9093d8d242", "2026-09-26T08:14:55.225988+00:00", "failed", 0.006248, 0.040063, 4),
    ("76e85c1e0900", "2026-09-26T08:15:19.116188+00:00", "finished", 0.012417, 0.202090, 6),
    ("2f510e4d13ef", "2026-09-26T08:15:59.015168+00:00", "failed", 0.0, 0.0, 3),
]


def _build_index(
    root: Path,
    runs: list[tuple[str, str, str, float, float, int]],
    *,
    price_calls: bool = True,
    dry_run: bool = False,
) -> Path:
    """Create a run index shaped like the paid eval's.

    `price_calls=False` records real calls with no provider-reported cost,
    which is what an unauditable run looks like.
    """
    store = RunStore(root)
    for run_id, started_at, status, estimate, billed, n_calls in runs:
        store.index_meta(
            RunMeta(
                run_id=run_id,
                orchestrator="orch",
                task_id="task",
                worker="worker",
                status=status,
                started_at=started_at,
                finished_at=started_at,
                total_cost_usd=estimate,
                run_dir=str(root / run_id),
            )
        )
        for i in range(n_calls):
            share = billed / n_calls if n_calls else 0.0
            store.record_call(
                run_id=run_id,
                phase="worker",
                step=i,
                role="worker",
                model="vendor/model",
                input_tokens=1000,
                output_tokens=500,
                cost_usd=(estimate / n_calls) if n_calls else 0.0,
                api_cost_usd=share if price_calls else None,
                pricing_source="configured",
                dry_run=dry_run,
            )
    return store.db


def _old_guard_cost(db: Path) -> float:
    """The check this guard replaced, verbatim from the workflow."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT total_cost_usd FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return float(row[0]) if row else 0.0


class BilledCostTests(unittest.TestCase):
    def test_reads_provider_billed_cost_not_the_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), MEASURED_WINDOW)
            cost = read_billed_cost(db)
        self.assertEqual(cost.runs, 4)
        self.assertEqual(cost.calls, 18)
        self.assertEqual(cost.priced_calls, 18)
        self.assertAlmostEqual(cost.billed_usd, 0.320974, places=5)
        self.assertAlmostEqual(cost.unpriced_estimate_usd, 0.0, places=6)
        # The rate card said 0.027331; the provider charged 0.320974.
        self.assertAlmostEqual(cost.estimate_usd, 0.027331, places=5)
        self.assertGreater(cost.estimate_ratio or 0, 11.0)

    def test_dry_run_calls_are_excluded_from_billed_cost(self) -> None:
        runs = [("r1", "2026-09-26T00:00:00+00:00", "finished", 0.0, 0.0, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), runs, dry_run=True)
            cost = read_billed_cost(db)
        self.assertEqual(cost.runs, 1)
        self.assertEqual(cost.calls, 0)
        self.assertEqual(cost.total_usd, 0.0)


class BudgetVerdictTests(unittest.TestCase):
    def test_over_billed_cost_fails_while_the_estimate_is_under_cap(self) -> None:
        """The regression: estimate $0.01, provider billed $0.32, cap $0.05."""
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), MEASURED_WINDOW)
            verdict = check_budget(db, 0.05)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.exit_code, EXIT_OVER_BUDGET)
        self.assertIn("billed $0.320974", verdict.summary)

    def test_the_old_estimate_check_passed_on_the_same_index(self) -> None:
        """Proves the replaced guard read the wrong number, not a stricter one."""
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), MEASURED_WINDOW)
            old_cost = _old_guard_cost(db)
            new_verdict = check_budget(db, 0.05)
        self.assertLessEqual(old_cost, 0.05)  # old guard: green
        self.assertFalse(new_verdict.ok)  # new guard: red

    def test_within_budget_passes(self) -> None:
        runs = [("r1", "2026-09-26T00:00:00+00:00", "finished", 0.01, 0.04, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), runs)
            verdict = check_budget(db, 0.05)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.exit_code, EXIT_OK)

    def test_exactly_at_the_cap_passes(self) -> None:
        runs = [("r1", "2026-09-26T00:00:00+00:00", "finished", 0.001, 0.05, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), runs)
            verdict = check_budget(db, 0.05)
        self.assertTrue(verdict.ok)

    def test_unpriced_calls_are_added_at_their_estimate(self) -> None:
        """A priced $0.01 call plus a $0.09 estimated call breaches a $0.05 cap."""
        root = Path(tempfile.mkdtemp())
        store = RunStore(root)
        store.index_meta(
            RunMeta(
                run_id="r1",
                orchestrator="o",
                task_id="t",
                worker="w",
                status="finished",
                started_at="2026-09-26T00:00:00+00:00",
                total_cost_usd=0.1,
            )
        )
        store.record_call(
            run_id="r1", phase="worker", step=0, role="worker", model="m",
            cost_usd=0.01, api_cost_usd=0.01,
        )
        store.record_call(
            run_id="r1", phase="judge", step=1, role="judge", model="m",
            cost_usd=0.09, api_cost_usd=None,
        )
        verdict = check_budget(store.db, 0.05)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.exit_code, EXIT_OVER_BUDGET)
        self.assertAlmostEqual(verdict.cost.total_usd, 0.10, places=6)

    def test_unauditable_run_fails_closed(self) -> None:
        """Real calls with no provider pricing cannot be shown to be in budget."""
        runs = [("r1", "2026-09-26T00:00:00+00:00", "failed", 0.001, 0.0, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), runs, price_calls=False)
            verdict = check_budget(db, 0.05)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.exit_code, EXIT_UNAUDITABLE)

    def test_dry_run_passes(self) -> None:
        runs = [("r1", "2026-09-26T00:00:00+00:00", "finished", 0.0, 0.0, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), runs, dry_run=True)
            verdict = check_budget(db, 0.05)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.exit_code, EXIT_OK)

    def test_empty_index_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(Path(tmp), [])
            verdict = check_budget(db, 0.05)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.exit_code, EXIT_UNAUDITABLE)

    def test_missing_index_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["--runs-dir", tmp, "--max-cost-usd", "0.05"]), EXIT_UNAUDITABLE)

    def test_corrupt_index_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            db.write_bytes(b"not a database")
            self.assertEqual(main(["--runs-dir", str(db), "--max-cost-usd", "0.05"]), EXIT_UNAUDITABLE)


class BudgetCliTests(unittest.TestCase):
    def test_cli_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            over = _build_index(
                Path(tmp) / "over", MEASURED_WINDOW
            ).parent
            self.assertEqual(main(["--runs-dir", str(over), "--max-cost-usd", "0.05"]), EXIT_OVER_BUDGET)
            under = _build_index(
                Path(tmp) / "under",
                [("r1", "2026-09-26T00:00:00+00:00", "finished", 0.001, 0.01, 2)],
            ).parent
            self.assertEqual(main(["--runs-dir", str(under), "--max-cost-usd", "0.05"]), EXIT_OK)

    def test_cli_accepts_a_bare_index_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(
                Path(tmp), [("r1", "2026-09-26T00:00:00+00:00", "finished", 0.001, 0.01, 2)]
            )
            self.assertEqual(main(["--runs-dir", str(db), "--max-cost-usd", "0.05"]), EXIT_OK)


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "orchestral.yml"


class WorkflowWiringTests(unittest.TestCase):
    """The workflow must call the guard, not re-derive the check in shell."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.load(cls.text, Loader=yaml.BaseLoader)
        cls.steps = cls.workflow["jobs"]["eval"]["steps"]
        cls.guard = next(s for s in cls.steps if s.get("name") == "Enforce cost budget")

    def test_guard_step_invokes_the_guard_module(self) -> None:
        self.assertIn("python3 -m orchestral.budget", self.guard["run"])
        self.assertIn("--max-cost-usd", self.guard["run"])

    def test_guard_step_no_longer_compares_the_rate_card_estimate(self) -> None:
        self.assertNotIn("total_cost_usd FROM runs", self.text)
        self.assertNotIn("bc -l", self.text)

    def test_cap_is_a_workflow_dispatch_input(self) -> None:
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertIn("max_cost_usd", inputs)
        self.assertEqual(inputs["max_cost_usd"]["default"], "0.05")

    def test_pricing_drift_decision_is_recorded_in_the_file(self) -> None:
        """AC #3: the drift gate is either wired in or declined with a reason."""
        drift_steps = [s for s in self.steps if "harness.py prices" in str(s.get("run", ""))]
        self.assertEqual(len(drift_steps), 1, "expected exactly one drift step")
        self.assertIn("DECLINED", self.text)
        self.assertIn("pricing_drift()", self.text)


if __name__ == "__main__":
    unittest.main()
