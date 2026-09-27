"""Tests that every required status check can actually be produced.

Branch protection matches a check *run name*. A matrix job reports as
`<job> (<matrix value>)`, so a job keyed `test` with a `python-version` matrix
reports `test (3.11)`, never `test`. If branch protection requires a context no
job can emit, every pull request is blocked forever and nothing in CI reports
why.

That is not hypothetical. On 2026-09-26 a ruleset on `main` was created
requiring the context `test`. The workflow's `test` job had been a four-way
matrix well before that, so the required context was never emitted and the
branch was wedged. The workflow even carried a comment claiming the job "keeps
its original name so a branch-protection required check by that name still
reports", and `test_ci_gate_independence.py` "enforced" it — by reading the YAML
job key, which is the declared name, not the name GitHub emits. The control
believed to cover this could never fire.

So: `.github/required-checks.json` records the contexts branch protection
requires, and these tests assert the workflow can produce every one of them.
See DUK-227.

The limitation is real and worth stating: CI cannot read branch protection, so
this file must be updated in the same change that alters protection. It detects
drift in the workflow (a matrix entry removed, a job renamed) and makes the
protection-to-workflow coupling explicit and reviewable. It cannot detect a
protection change made without updating this file.
"""

from __future__ import annotations

import json
import unittest
from itertools import product
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRACT = REPO_ROOT / ".github" / "required-checks.json"

# The context that wedged `main`. See the module docstring.
CONTEXT_THAT_BROKE_MAIN = "test"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _axis_values(matrix: dict) -> list[list[str]]:
    """Matrix axes as a list of value-lists, one list per combination axis."""
    return [[str(v) for v in matrix[key]] for key in matrix]


def _context_for(job_id: str, job: dict) -> list[str]:
    """Every check-run name GitHub reports for one job.

    A matrix job appends its matrix values, joined with ", ", in matrix
    definition order. A job without a matrix reports only its own name. The
    `name:` key overrides the job id, exactly as on GitHub.

    Multi-axis ordering is unverified: this workflow has no multi-axis matrix, so
    there was nothing to observe. Definition order is used because it is at
    least the order the author wrote. If it is wrong, the guard fails *closed* —
    a required context it cannot reproduce is reported as unproducible rather
    than silently passing — so the guess is safe in the direction that matters.
    """
    base = str(job.get("name") or job_id)
    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict) or not matrix:
        return [base]
    return [f"{base} ({', '.join(combo)})" for combo in product(*_axis_values(matrix))]


def emitted_contexts(jobs: dict | None = None) -> set[str]:
    """Every check-run name this workflow can report."""
    return {name for job_id, job in (jobs or _jobs()).items() for name in _context_for(job_id, job)}


def required_contexts() -> list[str]:
    return list(json.loads(CONTRACT.read_text())["required"])


def unproducible(required: list[str], emitted: set[str]) -> list[str]:
    """Required contexts no job can report, in the order declared."""
    return [c for c in required if c not in emitted]


class TestRequiredCheckContract(unittest.TestCase):
    def test_contract_is_not_empty(self):
        # An empty contract makes the other assertions vacuous.
        self.assertTrue(required_contexts(), "required-checks.json declares no required contexts")

    def test_every_required_context_is_producible(self):
        missing = unproducible(required_contexts(), emitted_contexts())
        self.assertEqual(
            missing,
            [],
            f"branch protection requires check name(s) {missing} that ci.yml cannot report. "
            "Either the workflow must emit that exact name, or required-checks.json must be "
            "updated in the same change as the protection rule.",
        )

    def test_contract_names_are_not_bare_matrix_job_keys(self):
        # The precise trap: a matrix job keyed `test` is not a context named
        # `test`. Catching it here is cheaper than a wedged branch.
        emitted = emitted_contexts()
        for name in required_contexts():
            self.assertIn(
                name,
                emitted,
                f"required context {name!r} looks like a bare job key with a matrix; "
                f"GitHub would report a suffixed name",
            )


class TestGitHubContextNaming(unittest.TestCase):
    """The expansion rule is the whole test, so it gets tested too."""

    def test_matrix_job_reports_suffixed_names(self):
        jobs = {"test": {"strategy": {"matrix": {"python-version": ["3.11", "3.12"]}}}}
        self.assertEqual(_context_for("test", jobs["test"]), ["test (3.11)", "test (3.12)"])

    def test_job_without_matrix_reports_its_own_name(self):
        self.assertEqual(_context_for("lint", {"runs-on": "ubuntu-latest"}), ["lint"])

    def test_explicit_name_overrides_the_job_key(self):
        jobs = {"build": {"name": "compile"}}
        self.assertEqual(_context_for("build", jobs["build"]), ["compile"])

    def test_multi_axis_matrix_yields_one_context_per_combination(self):
        # Order-agnostic on purpose: the axis ordering is unverified (see
        # _context_for). What must hold is one context per combination, each
        # naming the job and carrying every axis value.
        jobs = {"t": {"strategy": {"matrix": {"os": ["a", "b"], "py": ["3.11"]}}}}
        names = _context_for("t", jobs["t"])
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        # Every combination names the job and carries the single-axis value...
        for name in names:
            self.assertTrue(name.startswith("t ("), name)
            self.assertIn("3.11", name)
        # ...and across the combinations, every value of the other axis appears.
        self.assertTrue(any("a" in n for n in names), names)
        self.assertTrue(any("b" in n for n in names), names)


class TestTheRegressionThisExistsFor(unittest.TestCase):
    def test_detector_flags_the_context_that_wedged_main(self):
        """The live ruleset requires `test`; this workflow cannot emit it.

        If this assertion ever starts passing, the detector has gone lax and the
        guard no longer protects anything. Keep it failing.
        """
        self.assertNotIn(CONTEXT_THAT_BROKE_MAIN, emitted_contexts())
        self.assertEqual(
            unproducible([CONTEXT_THAT_BROKE_MAIN], emitted_contexts()),
            [CONTEXT_THAT_BROKE_MAIN],
        )

    def test_the_old_guard_could_not_have_caught_this(self):
        """Why a second test was needed, stated as an executable claim.

        `test_ci_gate_independence.py` reads the YAML job key. The key exists and
        is called `test`, so a key-based check passes while the branch is wedged.
        """
        self.assertIn("test", _jobs(), "the job key is `test`, which is what the old guard read")
        self.assertEqual(
            unproducible([CONTEXT_THAT_BROKE_MAIN], emitted_contexts()),
            [CONTEXT_THAT_BROKE_MAIN],
            "the declared key and the emitted name diverge, which is the whole defect",
        )


if __name__ == "__main__":
    unittest.main()
