"""Tests for `harness.py validate` per-type metadata contracts.

Fixture-graded code specs (the v3 repo-checkout family) are graded by
`metadata.verify.command` inside a staged sandbox — the runner never reads
`module`/`tests` on that branch, so validate must not demand them. The
non-fixture branch keeps the old contract.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import yaml

import harness

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_spec(tasks_dir: Path, name: str, spec: dict) -> None:
    (tasks_dir / f"{name}.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")


def _validate(tasks_dir: Path, models_dir: Path) -> str:
    args = argparse.Namespace(tasks_dir=str(tasks_dir), models_dir=str(models_dir))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        harness.cmd_validate(args)
    return buf.getvalue()


def _code_spec(metadata: dict) -> dict:
    return {
        "id": "t-1",
        "title": "t",
        "blurb": "t",
        "type": "code",
        "prompt": "Do the thing.",
        "metadata": metadata,
    }


class TestFixtureRequiredMeta(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.tasks = root / "tasks"
        self.tasks.mkdir()
        self.models = root / "models"
        self.models.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inline_code_spec_still_requires_module_and_tests(self) -> None:
        _write_spec(self.tasks, "inline", _code_spec({}))
        out = _validate(self.tasks, self.models)
        self.assertIn("missing metadata module, tests", out)

    def test_fixture_spec_passes_with_verify_command(self) -> None:
        spec = _code_spec({"fixture": "repo-1.0", "verify": {"command": ["pytest", "-x"]}})
        _write_spec(self.tasks, "fx", spec)
        out = _validate(self.tasks, self.models)
        self.assertIn("ok   fx.yaml", out)
        self.assertNotIn("missing metadata", out)

    def test_fixture_spec_missing_verify_command_fails(self) -> None:
        _write_spec(self.tasks, "fx", _code_spec({"fixture": "repo-1.0"}))
        out = _validate(self.tasks, self.models)
        self.assertIn("missing metadata verify.command", out)

    def test_fixture_spec_empty_verify_command_fails(self) -> None:
        spec = _code_spec({"fixture": "repo-1.0", "verify": {"command": []}})
        _write_spec(self.tasks, "fx", spec)
        out = _validate(self.tasks, self.models)
        self.assertIn("missing metadata verify.command", out)

    def test_v3_shipped_specs_do_not_fail_required_meta(self) -> None:
        """Regression: the v3 fixture family graded via verify.command was
        failing `missing metadata module, tests` on a stale contract."""
        out = _validate(REPO_ROOT / "tasks", REPO_ROOT / "models")
        self.assertNotIn("missing metadata", out)


class TestMetaHas(unittest.TestCase):
    def test_dotted_key_traversal(self) -> None:
        self.assertTrue(harness._meta_has({"verify": {"command": ["a"]}}, "verify.command"))
        self.assertFalse(harness._meta_has({"verify": {"command": []}}, "verify.command"))
        self.assertFalse(harness._meta_has({"verify": {}}, "verify.command"))
        self.assertFalse(harness._meta_has({}, "verify.command"))
        self.assertFalse(harness._meta_has({"verify": "x"}, "verify.command"))


if __name__ == "__main__":
    unittest.main()
