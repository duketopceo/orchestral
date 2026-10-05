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
import unittest.mock
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
        validation=["non_empty", "has_required", "no_forbidden",
                    "exact_answer"],
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
        # an unexportable requested check must refuse rather than ship a
        # verifier weaker than the spec's own contract
        spec = TaskSpec(id="bare", type="image", prompt="p")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError) as cm:
            export_task(spec, tmp, publish_keys=True)
        self.assertIn("no Harbor", str(cm.exception))

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
        # required_content is a {path: [tokens]} mapping — the exported
        # check must keep the path binding, not flatten to global tokens
        spec = TaskSpec(id="rc", type="html", prompt="p",
                        validation=["non_empty", "has_content"],
                        metadata={
                            "required_content": {
                                "index.html": ["FALCON-4417", "nav"]
                            },
                        })
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(spec, tmp, publish_keys=True)
            checks = json.loads((pkg / "tests/checks.json").read_text())
        self.assertEqual(
            checks["has_content"], {"index.html": ["FALCON-4417", "nav"]}
        )

    def test_unrequested_metadata_keys_do_not_export(self):
        # a metadata.required key the spec never asked to check must not
        # silently become a verifier — validation gates every family
        spec = TaskSpec(id="unreq", type="html", prompt="p",
                        validation=["non_empty"],
                        metadata={"required": ["FALCON-4417"]})
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(spec, tmp, publish_keys=True)
            checks = json.loads((pkg / "tests/checks.json").read_text())
        self.assertNotIn("has_required", checks)
        self.assertEqual(checks, {"non_empty": True})

    def test_unsafe_spec_id_refused(self):
        for bad_id in ("../escape", "a/b", "..", "/abs", 'q"uote', "n\nl"):
            spec = TaskSpec(id=bad_id, type="html", prompt="p",
                            validation=["non_empty"])
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError) as cm:
                    export_task(spec, tmp, publish_keys=True)
                self.assertIn("unsafe task id", str(cm.exception))
                import os
                self.assertEqual(os.listdir(tmp), [])

    def test_reexport_replaces_package_wholesale(self):
        # stale files from an earlier export must not survive into a
        # re-export — the package is a published artifact
        with tempfile.TemporaryDirectory() as tmp:
            spec = _mechanical_spec()
            pkg = export_task(spec, tmp, publish_keys=True)
            stale = pkg / "tests" / "oracle" / "old.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("retired key\n")
            export_task(spec, tmp, publish_keys=True)
            self.assertFalse(stale.exists())
            self.assertTrue((pkg / "tests" / "checks.json").exists())

    def test_verifier_payloads_not_scrubbed(self):
        # _guest_scrub mangles non-/home/user paths — verifier bytes must
        # match what the real harness writes, verbatim
        spec = _fixture_spec()
        spec.metadata["test_files"] = {
            "tests/test_oracle.py": "P = '/home/jenkins/lib'\ndef test_x(): pass\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp) / "fixtures"
            fixture_root.mkdir()
            _write_fixture(fixture_root)
            pkg = export_task(spec, Path(tmp) / "out",
                              publish_keys=True, fixtures_dir=fixture_root)
            body = (pkg / "tests/oracle/tests/test_oracle.py").read_text()
        self.assertIn("/home/jenkins/lib", body)

    def test_checks_py_exact_answer_semantics(self):
        # expected_answer is exact full-artifact equality in the harness —
        # a substring inside a larger workspace file must fail
        import subprocess
        spec = TaskSpec(
            id="exact", type="html", prompt="p",
            validation=["exact_answer"],
            metadata={"expected_answer": "FALCON-4417"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(spec, tmp, publish_keys=True)
            work = Path(tmp) / "work"
            work.mkdir()
            (work / "out.txt").write_text("prefix FALCON-4417 suffix\n")
            rc = subprocess.run(
                ["python3", str(pkg / "tests/checks.py"),
                 str(work), str(pkg / "tests/checks.json")],
                capture_output=True, text=True,
            )
            self.assertEqual(rc.returncode, 1)
            (work / "out.txt").write_text("FALCON-4417")
            rc = subprocess.run(
                ["python3", str(pkg / "tests/checks.py"),
                 str(work), str(pkg / "tests/checks.json")],
                capture_output=True, text=True,
            )
            self.assertEqual(rc.returncode, 0, rc.stderr)

    def test_checks_py_path_scoped_content(self):
        # has_content binds tokens to their declared path — the same
        # token in a different file must fail
        import subprocess
        spec = TaskSpec(
            id="scoped", type="html", prompt="p",
            validation=["has_content"],
            metadata={"required_content": {"a.txt": ["NEEDLE-1"]}},
        )
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(spec, tmp, publish_keys=True)
            work = Path(tmp) / "work"
            work.mkdir()
            (work / "b.txt").write_text("NEEDLE-1\n")
            rc = subprocess.run(
                ["python3", str(pkg / "tests/checks.py"),
                 str(work), str(pkg / "tests/checks.json")],
                capture_output=True, text=True,
            )
            self.assertEqual(rc.returncode, 1)
            (work / "a.txt").write_text("needle-1\n")  # case-insensitive
            rc = subprocess.run(
                ["python3", str(pkg / "tests/checks.py"),
                 str(work), str(pkg / "tests/checks.json")],
                capture_output=True, text=True,
            )
            self.assertEqual(rc.returncode, 0, rc.stderr)

    def test_generated_test_sh_is_valid_bash(self):
        import subprocess
        for spec, kwargs in (
            (_mechanical_spec(), {}),
            (_fixture_spec(), {"fixtures_dir": Path}),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                if kwargs:
                    fixture_root = Path(tmp) / "fixtures"
                    fixture_root.mkdir()
                    _write_fixture(fixture_root)
                    kwargs = {"fixtures_dir": fixture_root}
                pkg = export_task(spec, Path(tmp) / "out",
                                  publish_keys=True, **kwargs)
                rc = subprocess.run(
                    ["bash", "-n", str(pkg / "tests/test.sh")],
                    capture_output=True, text=True,
                )
                self.assertEqual(rc.returncode, 0, rc.stderr)

    def test_secret_scan_defense_in_depth(self):
        # is_holdout() passed, but if a holdout key reaches the emitted
        # bytes the scan must still fire — guard-bypass detection
        spec = _mechanical_spec()
        spec.prompt = "p SECRET-CANARY-7 p"
        with (tempfile.TemporaryDirectory() as tmp, unittest.mock.patch(
            "orchestral.harbor_export.holdout_secrets",
            return_value=["SECRET-CANARY-7"],
        ), self.assertRaises(ValueError) as cm):
            export_task(spec, tmp, publish_keys=True)
        self.assertIn("secret", str(cm.exception).lower())
        # scan refusals run pre-write — no half-written package skeleton
        self.assertFalse((Path(tmp) / spec.id).exists())


def _write_fixture(fixture_root: Path, fixture_id: str = "demo-fix") -> None:
    """A real tarball + lock — export goes through verified_fixture_bytes,
    the same grading-time contract codeexec/cubeexec rely on."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (
            ("repo/main.py", b"print('ok')\n"),
            ("repo/tests/test_suite.py", b"def test_y(): pass\n"),
        ):
            info = tarfile.TarInfo(name)
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

    def test_protected_manifest_covers_test_members(self):
        # the exported test-path denylist mirrors run_repo_suite: every
        # test-owned fixture member is pinned by sha256 so agent edits
        # or agent-created conftest/toolchain files fail verification
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._export(tmp)
            manifest = (pkg / "tests/protected.sha256").read_text()
            self.assertIn("  tests/test_suite.py", manifest)
            self.assertNotIn("main.py", manifest)
            sh = (pkg / "tests/test.sh").read_text()
            self.assertIn("protected.sha256", sh)
            self.assertIn("unauthorized test/toolchain file", sh)


class TestMechanicalExport(unittest.TestCase):
    def test_checks_json_carries_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = export_task(_mechanical_spec(), Path(tmp) / "out",
                              publish_keys=True)
            checks = json.loads((pkg / "tests/checks.json").read_text())
            # check-name-keyed payloads, gated on spec.validation —
            # exact_answer stays a distinct check, not a substring token
            self.assertEqual(checks["has_required"], ["FALCON-4417"])
            self.assertEqual(checks["no_forbidden"], ["lorem"])
            self.assertEqual(checks["exact_answer"], "FALCON-4417")
            self.assertEqual(checks["non_empty"], True)
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
