"""The paid `eval` workflow is a required merge gate surface, so its structure
is a gate too.

These tests read `.github/workflows/orchestral.yml` as the source of truth
rather than restating its rules in Python, so editing the workflow changes what
these tests exercise.

The load-bearing property is that a skipped eval still *reports*. A workflow
filtered out by `paths`/`paths-ignore` reports no check at all, which leaves a
required check pending on main forever — strictly worse than the latency and
spend this file exists to avoid. (DUK-198)

The paid eval is dispatch-only and environment-gated: PR-controlled code can
never reach the provider key (DUK-184), and the per-ref concurrency guard keeps
a re-dispatch from queueing behind its own stale run (DUK-198).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "orchestral.yml"

# Steps that spend provider money or read output the paid run produces. Each
# must exist so the job's paid surface is pinned: removing a step silently
# shrinks the audit trail the environment approval is supposed to cover.
PAID_STEPS = {
    "Install orchestral",
    "Run eval",
    "Generate reports",
    "Upload reports",
    "Enforce cost budget",
}


def _load() -> dict[str, Any]:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class ConcurrencyTests(unittest.TestCase):
    """A re-dispatch supersedes the run in flight for the same ref."""

    def test_superseded_runs_are_cancelled(self) -> None:
        concurrency = _load()["concurrency"]
        self.assertEqual(concurrency["cancel-in-progress"], "true")

    def test_group_is_keyed_per_ref(self) -> None:
        group = _load()["concurrency"]["group"]
        self.assertIn("github.ref", group)


class ReportingTests(unittest.TestCase):
    """A skipped eval must report, because `eval` is a required check."""

    def test_no_path_filtering_on_any_trigger(self) -> None:
        """The one-way-door trap: a path-filtered workflow reports no check.

        With `eval` required on main, every docs-only and test-only PR would
        block forever with `eval` never reported, and nothing in the PR would
        show why. This holds across every workflow, since `test` gates merges
        the same way `eval` does.
        """
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
            triggers = workflow.get("on", workflow.get(True)) or {}
            for event, config in triggers.items():
                if not isinstance(config, dict):
                    continue
                for key in ("paths", "paths-ignore"):
                    with self.subTest(workflow=path.name, event=event):
                        self.assertNotIn(
                            key,
                            config,
                            msg=f"{path.name}: {event} filters on {key}, so a required check never reports",
                        )

    def test_every_paid_step_exists(self) -> None:
        steps = _load()["jobs"]["eval"]["steps"]
        by_name = {step.get("name"): step for step in steps}
        for name in PAID_STEPS:
            with self.subTest(step=name):
                self.assertIn(name, by_name, msg=f"{name} step must exist")


if __name__ == "__main__":
    unittest.main()
