"""Fixtures: registry contract, fetch integrity, drift checks, audit gate."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestral.audit import check_fixture_contract
from orchestral.config import TaskSpec
from orchestral.fixtures import (
    FixtureError,
    check_fixtures,
    fetch_fixture,
    load_registry,
    screen_members,
)

_COMMIT = "a" * 40


def _registry_text(**entry_overrides):
    entry = {
        "repo": "org/tool",
        "commit": _COMMIT,
        "license": "MIT",
        "license_url": "https://github.com/org/tool/blob/main/LICENSE",
        "verify_deps": [],
        "notes": "test",
    }
    entry.update(entry_overrides)
    import yaml
    return yaml.safe_dump({"fixtures": {"tool-1.0": entry}})


def _repo_tarball(members: dict[str, bytes], top: str = "tool-abcdef") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in members.items():
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size = len(body)
            tf.addfile(ti, io.BytesIO(body))
    return buf.getvalue()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fixtures-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, text: str) -> None:
        (self.root / "registry.yaml").write_text(text, encoding="utf-8")

    def test_load_valid_entry(self):
        self._write(_registry_text())
        reg = load_registry(self.root)
        spec = reg["tool-1.0"]
        self.assertEqual(spec.repo, "org/tool")
        self.assertIn("codeload.github.com/org/tool/tar.gz/" + _COMMIT, spec.codeload_url)

    def test_missing_registry_is_empty(self):
        self.assertEqual(load_registry(self.root), {})

    def test_license_gate_rejects_copyleft(self):
        self._write(_registry_text(license="GPL-3.0"))
        with self.assertRaises(FixtureError) as ctx:
            load_registry(self.root)
        self.assertIn("allowlist", str(ctx.exception))

    def test_short_commit_rejected(self):
        self._write(_registry_text(commit="abcdef1"))
        with self.assertRaises(FixtureError):
            load_registry(self.root)

    def test_missing_license_url_rejected(self):
        self._write(_registry_text(license_url=""))
        with self.assertRaises(FixtureError):
            load_registry(self.root)


class MemberScreenTests(unittest.TestCase):
    def test_absolute_and_parent_rejected(self):
        bad = screen_members(["/etc/passwd", "../evil", "ok/file.py"])
        self.assertEqual(sorted(bad), ["../evil", "/etc/passwd"])

    def test_clean_members_pass(self):
        self.assertEqual(screen_members(["repo/a.py", "repo/sub/b.py"]), [])


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fixtures-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "registry.yaml").write_text(_registry_text(), encoding="utf-8")

    def test_fetch_packs_repo_and_lock(self):
        payload = _repo_tarball({"README.md": b"hi", "src/tool.py": b"x=1\n"})
        with mock.patch("orchestral.fixtures._download", return_value=payload), \
             mock.patch("orchestral.fixtures._pip_download") as wh:
            res = fetch_fixture("tool-1.0", self.root)
        wh.assert_not_called()  # no verify_deps → no wheelhouse work
        self.assertTrue(res.tarball.exists())
        lock = json.loads(res.lockfile.read_text())
        self.assertEqual(lock["commit"], _COMMIT)
        self.assertEqual(lock["sha256"], hashlib.sha256(res.tarball.read_bytes()).hexdigest())
        # members re-rooted under repo/ with the codeload prefix stripped
        with tarfile.open(res.tarball) as tf:
            names = tf.getnames()
        self.assertIn("repo/src/tool.py", names)
        self.assertNotIn("tool-abcdef/src/tool.py", names)

    def test_fetch_rejects_unsafe_members(self):
        payload = _repo_tarball({"../../evil.py": b"x"})
        with (
            mock.patch("orchestral.fixtures._download", return_value=payload),
            self.assertRaises(FixtureError) as ctx,
        ):
            fetch_fixture("tool-1.0", self.root)
        self.assertIn("unsafe member", str(ctx.exception))

    def test_fetch_unregistered_fails_loud(self):
        with self.assertRaises(FixtureError):
            fetch_fixture("ghost-9", self.root)

    def test_check_reports_missing_then_consistent(self):
        problems = check_fixtures(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("missing", problems[0])
        payload = _repo_tarball({"a.py": b"1"})
        with mock.patch("orchestral.fixtures._download", return_value=payload):
            fetch_fixture("tool-1.0", self.root)
        self.assertEqual(check_fixtures(self.root), [])

    def test_check_catches_tarball_drift(self):
        payload = _repo_tarball({"a.py": b"1"})
        with mock.patch("orchestral.fixtures._download", return_value=payload):
            res = fetch_fixture("tool-1.0", self.root)
        res.tarball.write_bytes(b"tampered")
        problems = check_fixtures(self.root)
        self.assertTrue(any("drifted" in p for p in problems))


class AuditGateTests(unittest.TestCase):
    def _spec(self, **meta):
        return TaskSpec(
            id="v3-test", title="t", blurb="b", type="code",
            prompt="fix it", metadata=meta,
        )

    def test_unregistered_fixture_is_error(self):
        findings = check_fixture_contract(
            self._spec(fixture="not-registered", verify={"command": ["pytest", "x.py"]})
        )
        self.assertTrue(any(f.rule == "fixture_unregistered" for f in findings))

    def test_missing_verify_command_is_error(self):
        findings = check_fixture_contract(self._spec(fixture="x"))
        self.assertTrue(any(f.rule == "fixture_no_verify_command" for f in findings))

    def test_network_commands_flagged(self):
        findings = check_fixture_contract(
            self._spec(
                fixture="x",
                setup_commands=["pip install requests"],
                verify={"command": ["pytest"]},
            )
        )
        self.assertTrue(any(f.rule == "fixture_needs_network" for f in findings))

    def test_no_index_pip_is_allowed(self):
        findings = check_fixture_contract(
            self._spec(
                fixture="x",
                setup_commands=["pip install --no-index --find-links wheelhouse pytest"],
                verify={"command": ["python", "-m", "pytest", "tests/"]},
            )
        )
        rules = {f.rule for f in findings}
        self.assertNotIn("fixture_needs_network", rules)

    def test_no_fixture_no_findings(self):
        self.assertEqual(check_fixture_contract(self._spec(module="m.py")), [])


if __name__ == "__main__":
    unittest.main()
