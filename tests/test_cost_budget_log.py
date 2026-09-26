"""The `Enforce cost budget` step must log the spend it is auditing.

The step is the only cost control in CI, so its log line is the sole
evidence of what a run actually cost. When the step escaped `\\${...}`
the shell printed the variable's *name* instead of its value, so the gate
that caps spend never said how much it spent.

These tests execute the real step script under bash against a real
runs/index.db, because the failure mode is shell interpolation -- a
string assertion on the YAML would not have caught the regression.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from orchestral.storage import RunMeta, RunStore

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "orchestral.yml"
BUDGET_STEP = "Enforce cost budget"
MAX_COST_USD = "0.05"


def _load_step_script(path: Path = WORKFLOW) -> str:
    """Return the `run:` body of the cost-budget step as the shell sees it."""
    workflow: dict[str, Any] = yaml.load(
        path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["eval"]["steps"]
    matches = [step for step in steps if step.get("name") == BUDGET_STEP]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {BUDGET_STEP!r} step, found {len(matches)}"
        )
    return str(matches[0]["run"])


def _index_run(root: Path, cost_usd: float, run_id: str = "r1") -> None:
    # The step opens the hardcoded relative path runs/index.db, so the index
    # has to sit one level below the cwd the step runs from.
    store = RunStore(root / "runs")
    store.index_meta(
        RunMeta(
            run_id=run_id,
            orchestrator="o",
            task_id="t",
            worker="w",
            status="finished",
            started_at="2026-09-26T12:00:00+00:00",
            finished_at="2026-09-26T12:05:00+00:00",
            total_cost_usd=cost_usd,
        )
    )


class CostBudgetLogTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("bc") is None:
            self.skipTest("the cost-budget step shells out to bc")
        self.script = _load_step_script()

    def _run_step(self, cost_usd: float) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _index_run(root, cost_usd)
            return subprocess.run(
                ["bash", "-e", "-c", self.script],
                cwd=root,
                env={"MAX_COST_USD": MAX_COST_USD, "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )

    def test_under_budget_log_reports_spend_and_ceiling(self) -> None:
        """A passing run states what it spent, not the template's placeholder."""
        result = self._run_step(0.0123)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.0123", result.stdout)
        self.assertIn(MAX_COST_USD, result.stdout)
        self.assertNotIn("${", result.stdout)

    def test_over_budget_log_reports_spend_and_ceiling(self) -> None:
        """A failing run names the real amount so the breach is diagnosable."""
        result = self._run_step(0.42)

        self.assertEqual(result.returncode, 1)
        self.assertIn("0.42", result.stdout)
        self.assertIn(MAX_COST_USD, result.stdout)
        self.assertNotIn("${", result.stdout)

    def test_step_carries_no_escaped_dollar(self) -> None:
        """Guard the class of bug: `\\$` silences expansion everywhere in the step."""
        self.assertNotIn("\\$", self.script)


if __name__ == "__main__":
    unittest.main()
