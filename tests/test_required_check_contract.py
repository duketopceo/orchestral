"""Tests that every required status check can actually be produced.

Branch protection matches a check *run name*. A matrix job reports as
`<job> (<matrix value>)`, so a job keyed `test` with a `python-version` matrix
reports `test (3.11)`, never `test`. If branch protection requires a context no
job can emit, every pull request is blocked forever and nothing in CI reports
why.

That is not hypothetical, and the mechanism is worse than a typo. On
2026-09-26T16:58:50Z a ruleset was created on `main` requiring the context
`test`. At that moment this workflow was correct: the `test` job was a single
non-matrix job and a bare `test` check run existed. Four hours later PR #68
merged the declared-support-range matrix (`2914b9b`), the emitted names became
`test (3.11)` through `test (3.14)`, and from that commit every pull request was
blocked because the required context could no longer be produced. No one
weakened the rule. A routine CI merge invalidated it as a side effect, and
nothing noticed.

Two things made it invisible. The workflow carried a comment claiming the job
"keeps its original name so a branch-protection required check by that name still
reports", which was false from the moment the matrix landed. And
`test_ci_gate_independence.py` read the YAML job key, so its name-coverage
claim could never fire. That guard is still worth having — it catches
re-coupling, `continue-on-error` and `always()` conditions, and it is not
replaced here. Only its claim to cover check names was false.

So: `.github/required-checks.json` records the contexts branch protection
requires, and these tests assert the workflow can still produce every one of
them. See DUK-227.

The list is not a constant and is not tied to one repair. It mirrors whatever
the live rule requires, so under the "require the four real contexts" repair it
holds four matrix names, and under the "emit a stable test" repair it holds a
single `test`. The tests here pass under either shape; only the JSON differs.
The commit that changes protection must change it too.

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
from typing import ClassVar

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
    """Fixtures, not live state.

    An earlier version of this file asserted that the live workflow cannot emit
    `test`. That is true today and false the moment option E lands — E adds a
    job that emits exactly `test`. A test that fails when the bug is fixed is a
    test that pins the wrong thing, so the incident is asserted with synthetic
    workflows and the live state is left to the tests above.
    """

    # The shape on `main` at the time of the incident: a matrix job, so no bare
    # `test` is emitted.
    MATRIX_WORKFLOW: ClassVar[dict] = {
        "test": {"strategy": {"matrix": {"python-version": ["3.11", "3.12"]}}},
    }
    # The shape option E produces: renamed matrix plus a non-matrix aggregate.
    AGGREGATE_WORKFLOW: ClassVar[dict] = {
        "test-matrix": {"strategy": {"matrix": {"python-version": ["3.11", "3.12"]}}},
        "test": {"runs-on": "ubuntu-latest", "needs": ["test-matrix"]},
    }

    def test_matrix_shape_cannot_satisfy_a_bare_test_requirement(self):
        # What actually wedged `main` on 2026-09-26T21:02:17Z.
        emitted = emitted_contexts(self.MATRIX_WORKFLOW)
        self.assertNotIn(CONTEXT_THAT_BROKE_MAIN, emitted)
        self.assertEqual(unproducible([CONTEXT_THAT_BROKE_MAIN], emitted), [CONTEXT_THAT_BROKE_MAIN])

    def test_aggregate_shape_does_satisfy_it(self):
        # So the guard is not just a permanent red: the repair clears it, and
        # the guard reports green once the contract matches the new shape.
        emitted = emitted_contexts(self.AGGREGATE_WORKFLOW)
        self.assertIn(CONTEXT_THAT_BROKE_MAIN, emitted)
        self.assertEqual(unproducible([CONTEXT_THAT_BROKE_MAIN], emitted), [])

    def test_the_old_guard_read_the_wrong_artifact(self):
        """Why a second test was needed, stated as an executable claim.

        `test_ci_gate_independence.py` reads the YAML job key. The key is `test`
        in the wedging shape, so a key-based check passes while the branch is
        wedged. The declared key and the emitted name are different artifacts.
        """
        self.assertIn("test", self.MATRIX_WORKFLOW, "the job key is `test`, which is what the old guard read")
        self.assertNotIn(
            CONTEXT_THAT_BROKE_MAIN,
            emitted_contexts(self.MATRIX_WORKFLOW),
            "the declared key and the emitted name diverge, which is the whole defect",
        )


if __name__ == "__main__":
    unittest.main()
