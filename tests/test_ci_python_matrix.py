"""Tests for the CI Python version matrix.

`pyproject.toml` declares `requires-python = ">=3.11"` with no upper bound, so
every 3.x above 3.11 is a support claim. The `ci` workflow has to actually run
the suite across that range, or a test that encodes one interpreter's behaviour
can be green at the floor and broken everywhere else without anyone finding out.

DUK-161. The defect these guard against is the `textual` trap one level up: a
check reporting success for a property it only ever measured in one environment.

The range is open at the top, so nothing in the repo states where it ends. The
floor is read out of `requires-python`; the ceiling is the pinned constant
`_NEWEST_SUPPORTED_MINOR` below, a hand-maintained claim about which minors the
suite has actually been run on. DUK-225.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PAID_EVAL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestral.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_MATRIX = re.compile(r"python-version:\s*\[(?P<versions>[^\]]*)\]")
_REQUIRES_PYTHON = re.compile(r'requires-python\s*=\s*"\s*>=\s*(?P<floor>\d+)\.(?P<patch>\d+)\s*"')
_QUOTED = re.compile(r'"(?P<version>\d+\.\d+)"')

# The top of the declared range. `requires-python` is `>=` with no ceiling, so a
# matrix that stops below this is an untested support claim, not a passing check.
# Bump it on the next CPython minor release, and add that minor to ci.yml with it.
#
# This is a floor on the matrix, not an exact match: a row above it is extra
# coverage, not a failure. Do not satisfy the guard below by making the matrix
# float — a bare "3.14" silently becomes 3.15 and reopens the gap.
_NEWEST_SUPPORTED_MINOR = "3.14"


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _declared_floor() -> tuple[int, ...]:
    match = _REQUIRES_PYTHON.search(PYPROJECT.read_text())
    if match is None:
        raise AssertionError(
            f"could not read a '>=X.Y' requires-python floor out of {PYPROJECT.name}"
        )
    return (int(match.group("floor")), int(match.group("patch")))


def _uncommented(path: Path) -> str:
    """The file's own lines, with `#` comments dropped.

    ci.yml documents its matrix four lines above the matrix, and a bracketed
    `python-version: [...]` example in that comment is indistinguishable from the
    real row to a regex over the raw text. Matching one would let a comment
    satisfy the guard while the row it documents is gone.
    """
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _matrix_versions() -> list[str]:
    match = _MATRIX.search(_uncommented(CI_WORKFLOW))
    if match is None:
        raise AssertionError(
            "ci.yml pins a single python-version and has no matrix.\n"
            "A scalar pin means the suite is exercised on exactly one "
            "interpreter, so any test that depends on interpreter-specific "
            "behaviour is unverified everywhere else in the declared range.\n"
            "See DUK-161."
        )
    return _QUOTED.findall(match.group("versions"))


class TestCiPythonMatrix(unittest.TestCase):
    def test_ci_runs_the_suite_on_more_than_one_interpreter(self) -> None:
        versions = _matrix_versions()
        self.assertGreaterEqual(
            len(versions),
            2,
            f"expected a version matrix, found {versions}",
        )
        self.assertEqual(
            len(versions),
            len(set(versions)),
            f"duplicate entries in the matrix: {versions}",
        )

    def test_matrix_includes_the_declared_support_floor(self) -> None:
        floor = ".".join(str(part) for part in _declared_floor())
        self.assertIn(
            floor,
            _matrix_versions(),
            f"requires-python is >={floor} but ci.yml does not run that version, "
            "so the oldest supported interpreter is unverified",
        )

    def test_no_matrix_version_below_the_declared_floor(self) -> None:
        floor = _declared_floor()
        for version in _matrix_versions():
            parts = tuple(int(part) for part in version.split("."))
            self.assertGreaterEqual(
                parts,
                floor,
                f"ci.yml runs {version}, which is below the declared floor "
                f"{'.'.join(str(p) for p in floor)}",
            )

    def test_matrix_runs_the_newest_supported_minor(self) -> None:
        """The ceiling half of the range. The floor half is the two tests above.

        Nothing in the repo states where the open-ended `>=3.11` range stops, so
        the matrix can lose its newest row and every floor-side assertion still
        passes. That reports success for a range measured only at the bottom,
        and the top goes unverified silently.

        It trips when the matrix shrinks. It does not know whether 3.15 has
        shipped, so a constant left stale here is still an untested claim, just
        one that now fails loudly instead of passing quietly.
        """
        versions = _matrix_versions()
        self.assertIn(
            _NEWEST_SUPPORTED_MINOR,
            versions,
            f"the top of the range went unverified: ci.yml's newest row is "
            f"{max(versions, key=_version_key) if versions else 'absent'}, but "
            f"{_NEWEST_SUPPORTED_MINOR} is the newest minor this project has run "
            f"the suite on. requires-python is "
            f"'>={'.'.join(str(part) for part in _declared_floor())}' with no "
            f"ceiling, so {_NEWEST_SUPPORTED_MINOR} is claimed whether or not "
            f"ci.yml runs it. Put the row back. To widen the range instead, "
            f"bump _NEWEST_SUPPORTED_MINOR and ci.yml together — do not float "
            f'the version, since a bare "{_NEWEST_SUPPORTED_MINOR}" silently '
            f"becomes the next minor.",
        )

    def test_matrix_skips_no_minor_inside_its_own_range(self) -> None:
        """The interior of the range. The ends are owned by the tests above.

        Pinning the top and the floor leaves the middle unconstrained:
        `["3.11", "3.14"]` satisfies both ends and leaves 3.12 and 3.13 untested,
        which is the `textual` trap one level in again — the file would report
        the range covered while a hole sits in the middle of it.

        The bound is the highest row the matrix itself carries, not
        `_NEWEST_SUPPORTED_MINOR`, so the interior stays guarded while the pin
        is stale. A hole is a hole whichever end declared it, and a matrix that
        runs ahead of the pin is contiguous or it is not; there is no reason to
        let a stale pin decide this.
        """
        versions = _matrix_versions()
        floor = _declared_floor()
        same_major = [v for v in versions if _version_key(v)[0] == floor[0]]
        self.assertTrue(
            same_major,
            f"ci.yml runs no {floor[0]}.x interpreter, so the declared range "
            f"from {'.'.join(str(part) for part in floor)} up is unverified",
        )
        newest = max(same_major, key=_version_key)
        gaps = [
            f"{floor[0]}.{minor}"
            for minor in range(floor[1] + 1, _version_key(newest)[1] + 1)
            if f"{floor[0]}.{minor}" not in versions
        ]
        if gaps:
            self.fail(
                f"ci.yml runs up to {newest} but skips {', '.join(gaps)}, so those "
                f"minors are support claims with no run behind them. Put the rows "
                f"back, or drop the rows above them."
            )

    def test_workflow_expands_the_matrix_in_setup_python(self) -> None:
        text = CI_WORKFLOW.read_text()
        self.assertIn(
            "${{ matrix.python-version }}",
            text,
            "ci.yml declares a matrix but setup-python still pins a literal "
            "version, so every job would run the same interpreter",
        )

    def test_paid_eval_workflow_declares_no_python_version_matrix(self) -> None:
        """The other workflow pins a single version on purpose, for money.

        `orchestral.yml` runs a real paid OpenRouter eval, so a matrix there is
        one job — one bill — per interpreter. CI would report that as broader
        coverage, which is the opposite of what it is. The same defect class as
        above, with a cost instead of a false pass.

        This checks one narrow thing: that this workflow neither declares a
        `python-version` matrix nor expands one. It is not a spend cap, and the
        name used to claim it was.

        `_MATRIX` needs a literal `[`. A block-style `strategy.matrix` written
        without brackets is not matched, and is then caught by the second
        assertion anyway, because expanding that block into `setup-python`
        writes `matrix.python-version` into the file.

        Five shapes do get past both while multiplying a real bill. All five are
        green here today, each measured by expanding the job as Actions does and
        counting the paid-eval steps that expansion produces:

        - a `matrix:` on a non-python key, fanning jobs out at one interpreter
        - a `matrix.include` fanned out through `${{ matrix['python-version'] }}`,
          which the bracket-quote accessor keeps off the second assertion's
          literal
        - a second job that runs the eval again
        - the eval run twice inside the one job
        - a second workflow file beside `orchestral.yml` carrying its own
          matrix, which the hard-coded path above never reads

        Two more clear both assertions and are deliberately not called spend
        paths, because neither can be settled from this repository. A
        `python-version` written as a list but never expanded is not a
        `setup-python` input, and whether such a step selects one interpreter,
        coerces the list to a string, or fails outright was not measured, so
        this docstring does not claim it does. A `reusable-workflow` call bills
        once per caller instance, and whether the called file fans out is not
        measurable from here, because that file is not in this repository.

        A comment above the pin states the intent, and this makes the common
        accidental case fail loudly. Closing the rest is a judgement about what
        this workflow is allowed to become, so it is raised as a review question
        on DUK-202 rather than decided here.
        """
        text = PAID_EVAL_WORKFLOW.read_text()
        self.assertIsNone(
            _MATRIX.search(text),
            "orchestral.yml declares a python-version matrix, so the paid eval "
            "runs once per interpreter and multiplies real spend. ci.yml already "
            "runs the declared range, so nothing is lost by removing it here.",
        )
        self.assertNotIn(
            "matrix.python-version",
            text,
            "orchestral.yml expands a python-version matrix into the paid "
            "eval's interpreter, so one PR event bills once per matrix entry",
        )


if __name__ == "__main__":
    unittest.main()
