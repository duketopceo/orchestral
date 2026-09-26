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
GATES = {
    "test": "unittest discover",
    "lint": "ruff check",
    "types": "mypy",
}

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

    def test_no_job_bundles_two_gates(self):
        """A shared job re-couples the gates: a failing step skips the rest."""
        for job_id, job in _jobs().items():
            run_text = _run_text(job)
            found = [gate for gate, command in GATES.items() if command in run_text]
            self.assertEqual(
                len(found), 1,
                f"job `{job_id}` runs the gates {sorted(found)}; split them so a "
                "failure in one cannot skip the others",
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
