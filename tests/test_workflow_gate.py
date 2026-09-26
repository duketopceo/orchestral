"""The paid `eval` job is a required merge gate, so its skip logic is a gate too.

These tests read `.github/workflows/orchestral.yml` as the source of truth
rather than restating its rules in Python. The relevance patterns are extracted
from the step's own JavaScript and evaluated here, so editing the workflow
changes what these tests exercise.

The load-bearing property is that a skipped eval still *reports*. A workflow
filtered out by `paths`/`paths-ignore` reports no check at all, which leaves a
required check pending on main forever — strictly worse than the latency and
spend this file exists to avoid. (DUK-198)
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "orchestral.yml"

# Steps that spend provider money or read output the paid run produces. Each
# must be guarded, or the job fails on a missing run instead of skipping it.
PAID_STEPS = {
    "Install orchestral",
    "Run eval",
    "Generate reports",
    "Upload reports",
    "Comment on PR",
    "Enforce cost budget",
}

# `true` = the diff can change the eval's result, so the eval must run.
RELEVANT_PATHS = [
    "orchestral/runner.py",
    "orchestral/__init__.py",
    "orchestral/web/server.py",
    "harness.py",
    "tasks/landing-page-coffee.yaml",
    "pyproject.toml",
    # The gate's own file. It sets the harness invocation the paid step runs, and
    # it holds the skip logic itself: if editing it were not relevant, the
    # relevance set could be widened, the guard removed, and the job gutted while
    # `eval` still reported success doing nothing. (DUK-256)
    ".github/workflows/orchestral.yml",
    ".github/workflows/ci.yml",
]
# `false` = the diff cannot change the eval's result, so the paid step is skipped.
IRRELEVANT_PATHS = [
    "README.md",
    "docs/task-spec.md",
    "docs/plans/2026-09-16-0214-serve-web-gui-plan.md",
    "tests/test_runner.py",
    "tests/test_workflow_gate.py",
    # Prefix and suffix boundaries: these are not the directories the gate reads.
    "orchestral_extra/helper.py",
    "orchestrality.py",
    "my_harness.py",
    "harness.py.bak",
    "tasks_extra/other.yaml",
    "my-pyproject.toml",
    # `.github/` outside `workflows/` is not CI configuration, and a
    # sibling directory is not the one the pattern names.
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/workflows-legacy/eval.yml",
]


def _load() -> dict[str, Any]:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _js_patterns(script: str) -> list[re.Pattern[str]]:
    """Pull the RELEVANT regex literals out of the classify step's JavaScript.

    Testing a Python copy of the patterns would pass even if the workflow's own
    patterns were wrong, so the patterns under test are the ones that ship.
    """
    declaration = re.search(r"const RELEVANT = \[(.*?)\];", script, re.DOTALL)
    assert declaration is not None, "classify step must declare a RELEVANT pattern list"
    literals = re.findall(r"/((?:[^/\\]|\\.)*)/[a-z]*", declaration.group(1))
    assert literals, "RELEVANT must not be empty"
    return [re.compile(source.replace(r"\/", "/")) for source in literals]


class ConcurrencyTests(unittest.TestCase):
    """A re-push supersedes the run in flight for the same PR head."""

    def test_superseded_runs_are_cancelled(self) -> None:
        concurrency = _load()["concurrency"]
        self.assertEqual(concurrency["cancel-in-progress"], "true")

    def test_group_is_keyed_per_pull_request(self) -> None:
        group = _load()["concurrency"]["group"]
        self.assertIn("github.event.pull_request.number", group)
        # A ref-only key would be fine for pull_request (the ref is
        # refs/pull/N/merge) but would collide across PRs on workflow_dispatch.
        self.assertIn("github.ref", group)

    def test_two_pull_requests_cannot_cancel_each_other(self) -> None:
        group = _load()["concurrency"]["group"]
        pr_number = re.search(r"github\.event\.pull_request\.number", group)
        self.assertIsNotNone(pr_number)
        # The PR number must be able to win over the ref, or a push to one PR
        # head could cancel a run belonging to another.
        self.assertLess(group.index("pull_request.number"), group.index("github.ref"))


class ReportingTests(unittest.TestCase):
    """A skipped eval must report, because `eval` is a required check."""

    def test_eval_still_triggers_on_pull_request(self) -> None:
        workflow = _load()
        # `on` parses as the boolean True under YAML 1.1 unless forced to a string.
        triggers = workflow.get("on", workflow.get(True))
        self.assertIn("pull_request", triggers)

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

    def test_the_job_is_not_guarded_away(self) -> None:
        job_condition = _load()["jobs"]["eval"].get("if", "")
        self.assertNotIn("steps.diff", job_condition)
        self.assertNotIn("outputs.relevant", job_condition)

    def test_every_paid_step_is_guarded(self) -> None:
        steps = _load()["jobs"]["eval"]["steps"]
        by_name = {step.get("name"): step for step in steps}
        for name in PAID_STEPS:
            with self.subTest(step=name):
                self.assertIn(name, by_name, msg=f"{name} step must exist")
                self.assertIn(
                    "steps.diff.outputs.relevant",
                    by_name[name].get("if", ""),
                    msg=f"{name} must be guarded, or the job fails on a missing run",
                )


class RelevanceTests(unittest.TestCase):
    """The skip verdict is a function of the diff, read from the shipped patterns."""

    @classmethod
    def setUpClass(cls) -> None:
        steps = _load()["jobs"]["eval"]["steps"]
        classify = next((s for s in steps if s.get("id") == "diff"), None)
        assert classify is not None, "classify step must expose id: diff"
        cls.script = classify["with"]["script"]
        cls.patterns = _js_patterns(cls.script)

    def _matches(self, path: str) -> bool:
        return any(pattern.search(path) for pattern in self.patterns)

    def test_product_source_still_gates(self) -> None:
        for path in RELEVANT_PATHS:
            with self.subTest(path=path):
                self.assertTrue(self._matches(path), msg=f"{path} must still run the paid eval")

    def test_docs_and_tests_are_skipped(self) -> None:
        for path in IRRELEVANT_PATHS:
            with self.subTest(path=path):
                self.assertFalse(self._matches(path), msg=f"{path} must skip the paid eval")

    def test_renames_are_checked_on_both_names(self) -> None:
        # Moving a relevant file away still removes it from the package, so the
        # old name has to be classified too.
        self.assertIn("previous_filename", self.script)

    def test_an_unseeable_diff_is_treated_as_relevant(self) -> None:
        # The list-files API truncates at 3000 files without signalling it.
        # Treating that as "skip" would silently drop the gate.
        self.assertIn("LIST_FILES_CAP", self.script)
        self.assertIn("files.length >= LIST_FILES_CAP", self.script)

    def test_manual_dispatch_always_runs_the_paid_eval(self) -> None:
        # A dispatch names a task and model pair explicitly, so there is no diff
        # to reason about and the human asked for the run.
        self.assertIn("let relevant = true", self.script)
        self.assertIn("context.eventName === 'pull_request'", self.script)

    def test_no_pull_request_payload_reaches_the_eval(self) -> None:
        """The gate is a function of the tree, never of data the PR controls."""
        self.assertNotIn("pull_request.body", self.script)
        self.assertNotIn("pull_request.title", self.script)


if __name__ == "__main__":
    unittest.main()
