"""Tests that one CI gate cannot hide another.

GitHub Actions skips every remaining step in a job once one step fails. The
gates used to share a single `test` job, so a red `Tests` step meant `Lint` and
`Types` never ran and a PR could merge with lint and type errors. These tests
read `.github/workflows/ci.yml` and fail if that coupling returns, and they keep
the CONTRIBUTING.md CI claim in step with the workflow it describes.

See DUK-200.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# job id -> the command that gate must actually run
#
# The suite gate is `test-matrix`, not `test`. `test` is the aggregate reporter
# that branch protection requires; it runs no gate command, it only reports the
# three above. See the comment on the `test` job in ci.yml, and DUK-227.
GATES = {
    "test-matrix": "unittest discover",
    "lint": "ruff check",
    "types": "mypy",
}

# Jobs that report gates rather than running one. Excluded from the
# "one gate per job" rule, which would otherwise fail them for running zero.
REPORTERS = {"test"}

COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def _gate_jobs() -> dict:
    jobs = _jobs()
    missing = sorted(set(GATES) - set(jobs))
    if missing:
        raise AssertionError(f"ci.yml defines no job for the gate(s) {missing}")
    return jobs


class TestCIGatesAreSeparateJobs(unittest.TestCase):
    def test_every_gate_is_its_own_job(self):
        jobs = _gate_jobs()
        for job_id, command in GATES.items():
            self.assertIn(
                command, _run_text(jobs[job_id]),
                f"the `{job_id}` job does not run `{command}`",
            )

    def test_every_job_is_a_declared_gate(self):
        """A job in ci.yml that is not in GATES is invisible to the gate rules.

        Without this, adding a legitimate new check produced "job `security`
        runs the gates []; split them so a failure in one cannot skip the
        others" — advice that is wrong, since the job runs exactly one gate.
        A guard whose failure message misdirects gets disabled.
        """
        undeclared = {j for j in _jobs() if j not in GATES and j not in REPORTERS}
        self.assertEqual(
            undeclared, set(),
            f"job(s) {sorted(undeclared)} run in ci.yml but are not declared in "
            f"GATES, so the gate rules skip them. Add each to GATES with the "
            f"command it runs, and wire it into the reporter's needs",
        )

    def test_no_job_bundles_two_gates(self):
        """A shared job re-couples the gates: a failing step skips the rest."""
        for job_id, job in _jobs().items():
            run_text = _run_text(job)
            found = [gate for gate, command in GATES.items() if command in run_text]
            if job_id in REPORTERS:
                self.assertEqual(
                    found, [],
                    f"reporter `{job_id}` must not run a gate itself; it reports them",
                )
                continue
            self.assertEqual(
                len(found), 1,
                f"job `{job_id}` runs the gates {sorted(found)}; split them so a "
                "failure in one cannot skip the others",
            )

    def test_reporter_needs_every_job_in_the_workflow(self):
        """No job in ci.yml may fail while the required reporter says success.

        The invariant is deliberately about *every* non-reporter job, not about
        the GATES map. An earlier version compared the reporter's `needs` against
        GATES, which is a declared list: a job added to ci.yml without being
        declared in GATES was invisible to it, so a new check could fail while
        `test` still reported green. A negative control caught exactly that.
        """
        jobs = _jobs()
        expected = {job_id for job_id in jobs if job_id not in REPORTERS}
        for job_id in REPORTERS:
            needs = jobs[job_id].get("needs") or []
            if isinstance(needs, str):
                needs = [needs]
            self.assertEqual(
                set(needs), expected,
                f"reporter `{job_id}` needs {sorted(needs)} but ci.yml defines "
                f"{sorted(expected)}. Every job must be wired in, or one can fail "
                f"while the required check reports success",
            )

    def test_reporter_runs_unconditionally(self):
        """The opposite failure: if the reporter is itself conditional it is
        skipped whenever a gate fails, and a skipped required check blocks the
        pull request. That is the wedge this reporter was added to clear."""
        for job_id in REPORTERS:
            condition = str(_jobs()[job_id].get("if", ""))
            self.assertIn(
                "always()", condition,
                f"reporter `{job_id}` is conditional; a skipped required check "
                "blocks the pull request, so it must run with if: always()",
            )

    def test_gate_jobs_do_not_wait_on_each_other(self):
        jobs = _gate_jobs()
        for job_id in GATES:
            needs = jobs[job_id].get("needs") or []
            if isinstance(needs, str):
                needs = [needs]
            overlap = set(needs) & set(GATES)
            self.assertFalse(
                overlap, f"job `{job_id}` waits on gate job(s) {sorted(overlap)}",
            )

    def test_no_gate_step_ignores_its_job_result(self):
        """A skipped step reports as neutral, which is how a gate goes quiet."""
        for job_id, job in _jobs().items():
            for step in job.get("steps", []):
                self.assertNotIn(
                    "continue-on-error", step,
                    f"step {step.get('name', job_id)!r} in job `{job_id}` can fail "
                    "without failing the job",
                )
                condition = str(step.get("if", ""))
                self.assertNotIn(
                    "always()", condition,
                    f"step {step.get('name', job_id)!r} in job `{job_id}` runs "
                    "unconditionally; gates must fail their own job",
                )
                self.assertNotIn(
                    "failure()", condition,
                    f"step {step.get('name', job_id)!r} in job `{job_id}` is "
                    "skipped on failure",
                )


class TestContributingMatchesWorkflow(unittest.TestCase):
    def test_stated_job_for_the_audit_actually_runs_it(self):
        """Job names in prose drift when jobs are renamed, and nothing else catches it.

        CONTRIBUTING.md states which job runs the task-spec audit. Asserting the
        gate *count* (below) did not catch this: it stayed at three while the
        job it named was renamed out from under the sentence. DUK-227.
        """
        match = re.search(r"audit runs as a step in the `([a-z0-9-]+)` job", CONTRIBUTING.read_text())
        self.assertIsNotNone(
            match, "CONTRIBUTING.md must say which job runs the task-spec audit"
        )
        named = match.group(1)
        jobs = _jobs()
        self.assertIn(named, jobs, f"CONTRIBUTING.md names job `{named}`, which ci.yml does not define")
        self.assertIn(
            "harness.py audit", _run_text(jobs[named]),
            f"CONTRIBUTING.md says the audit runs in `{named}`, but it does not",
        )

    def test_stated_ci_gate_count_matches_the_workflow(self):
        match = re.search(r"All (\w+) run in CI", CONTRIBUTING.read_text())
        self.assertIsNotNone(match, "CONTRIBUTING.md must state which gates run in CI")
        word = match.group(1).lower()
        self.assertIn(word, COUNT_WORDS, f"unknown gate count {match.group(1)!r}")
        self.assertEqual(
            COUNT_WORDS[word], len(GATES),
            "CONTRIBUTING.md claims a different number of CI gates than ci.yml runs",
        )


if __name__ == "__main__":
    unittest.main()
