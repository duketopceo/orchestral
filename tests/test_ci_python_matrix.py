"""Tests for the CI Python version matrix.

`pyproject.toml` declares `requires-python = ">=3.11"` with no upper bound, so
every 3.x above 3.11 is a support claim. The `ci` workflow has to actually run
the suite across that range, or a test that encodes one interpreter's behaviour
can be green at the floor and broken everywhere else without anyone finding out.

DUK-161. The defect these guard against is the `textual` trap one level up: a
check reporting success for a property it only ever measured in one environment.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_MATRIX = re.compile(r"python-version:\s*\[(?P<versions>[^\]]*)\]")
_REQUIRES_PYTHON = re.compile(r'requires-python\s*=\s*"\s*>=\s*(?P<floor>\d+)\.(?P<patch>\d+)\s*"')
_QUOTED = re.compile(r'"(?P<version>\d+\.\d+)"')


def _declared_floor() -> tuple[int, ...]:
    match = _REQUIRES_PYTHON.search(PYPROJECT.read_text())
    if match is None:
        raise AssertionError(
            f"could not read a '>=X.Y' requires-python floor out of {PYPROJECT.name}"
        )
    return (int(match.group("floor")), int(match.group("patch")))


def _matrix_versions() -> list[str]:
    match = _MATRIX.search(CI_WORKFLOW.read_text())
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

    def test_workflow_expands_the_matrix_in_setup_python(self) -> None:
        text = CI_WORKFLOW.read_text()
        self.assertIn(
            "${{ matrix.python-version }}",
            text,
            "ci.yml declares a matrix but setup-python still pins a literal "
            "version, so every job would run the same interpreter",
        )


if __name__ == "__main__":
    unittest.main()
