"""Tests for Harbor task-package export (C1).

The package publishes the answer key by design — the tests pin the
guards: holdout refuses outright, --publish-keys is mandatory, graded
metadata never reaches task.toml, and guest paths survive the scrub.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

import harness
from orchestral.config import TaskSpec
from orchestral.harbor_export import export_task


def _fixture_spec() -> TaskSpec:
    return TaskSpec(
        id="v3-demo", type="code", title="demo repo task",
        prompt="Fix the bug in repo.",
        metadata={
            "fixture": "demo-fix",
            "workdir": "repo",
            "expected_paths": ["pkg/mod.py"],
            "setup_commands": [
                "python -m pip install --quiet --no-index "
                "--find-links /home/user/wheelhouse pytest",
            ],
            "verify": {"command": ["python", "-m", "pytest", "-x", "-q"],
                        "fail_to_pass": ["test_x"]},
            "test_files": {"tests/test_oracle.py": "def test_x(): pass\n"},
            "timeout_seconds": 240,
            "difficulty": "hard",
            "archetype": "bugfix",
            "contamination_risk": "medium",
            "reference": {"pkg/mod.py": "SECRET REFERENCE BODY"},
            "calls": [{"internal": "note"}],
        },
    )


def _mechanical_spec() -> TaskSpec:
    return TaskSpec(
        id="needle-demo", type="html", title="needle",
        prompt="Write a page.",
        metadata={
            "required": ["FALCON-4417"],
            "forbidden": ["lorem"],
            "expected_answer": "FALCON-4417",
            "difficulty": "easy",
        },
    )


class TestGuards(unittest.TestCase):
    def test_holdout_refuses(self):
        spec = TaskSpec(id="h", type="html", prompt="p",
                        metadata={"holdout": True, "required": ["k3y-999"]})
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError) as cm:
            export_task(spec, tmp, publish_keys=True)
        self.assertIn("holdout", str(cm.exception))

    def test_publish_keys_required(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError) as cm:
            export_task(_mechanical_spec(), tmp, publish_keys=False)
        self.assertIn("--publish-keys", str(cm.exception))

    def test_non_fixture_without_checks_refuses(self):
        spec = TaskSpec(id="bare", type="html", prompt="p")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError) as cm:
            export_task(spec, tmp, publish_keys=True)
        self.assertIn("nothing", str(cm.exception))

    def test_missing_fixture_blob_refuses(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError) as cm:
            export_task(_fixture_spec(), tmp, publish_keys=True,
                        fixtures_dir=Path(tmp) / "empty")
        self.assertIn("fixtures fetch", str(cm.exception))

    def test_fixture_without_verify_command_refused(self):
        # a fixture spec's verifier IS verify.command — without it the
        # package would emit a test.sh referencing files that don't exist
        spec = _fixture_spec()
        spec.metadata = {k: v for k, v in spec.metadata.items()
                         if k != "verify"}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError) as cm:
            export_task(spec, tmp, publish_keys=True)
        self.assertIn("verify.command", str(cm.exception))

    def test_oracle_path_traversal_refused(self):
        # test_files keys are spec-controlled member names — "../evil.py"
        # must never write outside tests/oracle/
        spec = _fixture_spec()
        spec.metadata["test_files"] = {"../evil.py": "x\n"}
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp) / "fixtures"
            fixture_root.mkdir()
            _write_fixture(fixture_root)
            with self.assertRaises(ValueError) as cm:
                export_task(spec, Path(tmp) / "out",
                            publish_keys=True, fixtures_dir=fixture_root)
        self.assertIn("unsafe member", str(cm.exception))

    def test_refusal_leaves_no_package_skeleton(self):
        # every guard runs before the first mkdir — a refused export
        # must not leave a half-written dist/<id>/ behind
        spec = _fixture_spec()
        spec.metadata = {k: v for k, v in spec.metadata.items()
                         if k != "verify"}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                export_task(spec, tmp, publish_keys=True)
            self.assertFalse((Path(tmp) / spec.id).exists())

    def test_dict_required_content_exports(self):
        # required_content is a {path: [tokens]} mapping — dict-shaped
        # values must flatten into checks.json, not drop silently
        spec = TaskSpec(id="rc", type="html", prompt="p", metadata={
            "required_content": {"index.html": ["FALCON-4417", "nav"]},
        })
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(spec, tmp, publish_keys=True)
            checks = json.loads((pkg / "tests/checks.json").read_text())
        self.assertIn("FALCON-4417", checks["required"])
        self.assertIn("nav", checks["required"])

    def test_secret_scan_defense_in_depth(self):
        # is_holdout() passed, but if a holdout key reaches the emitted
        # bytes the scan must still fire — guard-bypass detection
        spec = _mechanical_spec()
        spec.prompt = "p SECRET-CANARY-7 p"
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch(
                "orchestral.harbor_export.holdout_secrets",
                return_value=["SECRET-CANARY-7"],
            ), self.assertRaises(ValueError) as cm:
                export_task(spec, tmp, publish_keys=True)
        self.assertIn("secret", str(cm.exception).lower())


def _write_fixture(fixture_root: Path, fixture_id: str = "demo-fix") -> None:
    """A real tarball + lock — export goes through verified_fixture_bytes,
    the same grading-time contract codeexec/cubeexec rely on."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"print('ok')\n"
        info = tarfile.TarInfo("repo/main.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    (fixture_root / f"{fixture_id}.tar.gz").write_bytes(blob)
    (fixture_root / f"{fixture_id}.lock.json").write_text(
        json.dumps({"sha256": hashlib.sha256(blob).hexdigest()})
    )


class TestFixtureExport(unittest.TestCase):
    def _export(self, tmp: str) -> Path:
        fixture_root = Path(tmp) / "fixtures"
        fixture_root.mkdir()
        _write_fixture(fixture_root)
        return export_task(
            _fixture_spec(), Path(tmp) / "out",
            publish_keys=True, fixtures_dir=fixture_root,
        )

    def test_package_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._export(tmp)
            for rel in ("task.toml", "instruction.md",
                        "environment/Dockerfile", "environment/fixture.tar.gz",
                        "tests/test.sh", "tests/oracle/tests/test_oracle.py"):
                self.assertTrue((pkg / rel).exists(), rel)

    def test_task_toml_allowlist_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._export(tmp)
            toml = (pkg / "task.toml").read_text()
            self.assertIn('difficulty = "hard"', toml)
            self.assertIn('archetype = "bugfix"', toml)
            self.assertIn('verifier_mode = "suite"', toml)
            self.assertIn('network_mode = "none"', toml)
            self.assertIn("fail_to_pass", toml)
            # graded/internal metadata never serializes wholesale
            self.assertNotIn("SECRET REFERENCE BODY", toml)
            self.assertNotIn('"calls"', toml)
            self.assertNotIn("reference =", toml)

    def test_test_sh_carries_guest_paths_and_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._export(tmp)
            sh = (pkg / "tests/test.sh").read_text()
            # the guest root is a container path — scrub must not mangle it
            self.assertIn("/home/user/wheelhouse", sh)
            self.assertIn("/home/user/repo", sh)
            self.assertIn("python -m pytest -x -q", sh)
            self.assertIn("reward", sh)
            self.assertNotIn("REDACTED", sh)

    def test_dockerfile_stages_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._export(tmp)
            df = (pkg / "environment/Dockerfile").read_text()
            self.assertIn("fixture.tar.gz", df)
            self.assertIn("WORKDIR /home/user/repo", df)

    def test_instruction_adapts_fileset_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._export(tmp)
            instr = (pkg / "instruction.md").read_text()
            self.assertIn("/home/user/repo", instr)
            self.assertIn("modify files in place", instr)


class TestMechanicalExport(unittest.TestCase):
    def test_checks_json_carries_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(_mechanical_spec(), Path(tmp) / "out",
                              publish_keys=True)
            checks = json.loads((pkg / "tests/checks.json").read_text())
            # required + expected_answer dedupe — the same key stated
            # twice is one check
            self.assertEqual(checks["required"], ["FALCON-4417"])
            self.assertEqual(checks["forbidden"], ["lorem"])
            toml = (pkg / "task.toml").read_text()
            self.assertIn('verifier_mode = "mechanical"', toml)
            # but the key must not leak into task.toml metadata
            self.assertNotIn("FALCON-4417", toml)
            self.assertTrue((pkg / "tests/checks.py").exists())

    def test_test_sh_executable(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(_mechanical_spec(), Path(tmp) / "out",
                              publish_keys=True)
            self.assertTrue(os.access(pkg / "tests/test.sh", os.X_OK))


class TestCmdHarbor(unittest.TestCase):
    """CLI wiring — the ack flag and the exit path go through argparse."""

    def _tasks_dir(self, tmp: str) -> Path:
        tasks = Path(tmp) / "tasks"
        tasks.mkdir()
        (tasks / "needle-demo.yaml").write_text(
            "id: needle-demo\ntitle: needle\ntype: html\n"
            "prompt: Write a page.\nmetadata:\n"
            "  required: ['FALCON-4417']\n"
        )
        return tasks

    def _args(self, tmp: str, **kw):
        defaults = {"harbor_cmd": "export", "task": "needle-demo",
                    "publish_keys": False,
                    "tasks_dir": str(self._tasks_dir(tmp)),
                    "out_dir": str(Path(tmp) / "dist"),
                    "fixtures_dir": str(Path(tmp) / "fixtures")}
        defaults.update(kw)
        return harness.argparse.Namespace(**defaults)

    def test_export_without_ack_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                harness.cmd_harbor(self._args(tmp))
            self.assertFalse((Path(tmp) / "dist").exists())

    def test_export_with_ack_writes_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness.cmd_harbor(self._args(tmp, publish_keys=True))
            pkg = Path(tmp) / "dist" / "needle-demo"
            self.assertTrue((pkg / "task.toml").exists())
            self.assertTrue((pkg / "tests/checks.json").exists())


if __name__ == "__main__":
    unittest.main()
