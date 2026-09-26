"""Tests for disposable generated-code verifier backends."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestral.codeexec import run_unittest_suite
from orchestral.sandbox import (
    SandboxError,
    SandboxResult,
    _build_archive,
    _docker_run_args,
    run_docker_unittest,
)


class TestDockerArguments(unittest.TestCase):
    def test_verifier_container_has_no_host_mounts(self):
        args = _docker_run_args(
            "python:3.11-slim",
            "orchestral-test",
            memory="512m",
            cpus="1",
            pids_limit=128,
        )
        self.assertEqual(args[:2], ["run", "--name"])
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop", args)
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        self.assertIn("--security-opt", args)
        self.assertIn("no-new-privileges:true", args)
        self.assertIn("--pids-limit", args)
        self.assertIn("--memory", args)
        self.assertIn("--cpus", args)
        self.assertNotIn("--mount", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("-v", args)
        self.assertNotIn("--volume", args)

    def test_archive_rejects_parent_escape(self):
        with self.assertRaises(SandboxError):
            _build_archive({"../outside.py": "x = 1\n"}, "import unittest\n")


class TestSandboxReport(unittest.TestCase):
    def test_codeexec_uses_docker_result(self):
        result = SandboxResult(
            returncode=0,
            stdout=(
                b"Ran 1 test in 0.001s\n\nOK\n"
            ),
            stderr=b"",
            image="python:3.11-slim@test",
        )
        with patch("orchestral.codeexec.run_docker_unittest", return_value=result):
            report = run_unittest_suite(
                {"solution.py": "def answer():\n    return 42\n"},
                "import unittest\nfrom solution import answer\n"
                "class T(unittest.TestCase):\n"
                "    def test_answer(self):\n"
                "        self.assertEqual(answer(), 42)\n",
                sandbox="docker",
                sandbox_image="python:3.11-slim@test",
            )
        self.assertTrue(report["executed"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["sandbox"], "docker")
        self.assertEqual(report["sandbox_image"], "python:3.11-slim@test")
        self.assertEqual(report["tests_run"], 1)

    def test_codeexec_fails_closed_when_docker_is_unavailable(self):
        with patch(
            "orchestral.codeexec.run_docker_unittest",
            side_effect=SandboxError("Docker is unavailable"),
        ):
            report = run_unittest_suite(
                {"solution.py": "x = 1\n"},
                "import unittest\n",
                sandbox="docker",
            )
        self.assertFalse(report["executed"])
        self.assertTrue(report["sandbox_error"])
        self.assertIn("Docker", report["error"])


@unittest.skipUnless(
    os.environ.get("ORCHESTRAL_DOCKER_TESTS") == "1" and shutil.which("docker"),
    "set ORCHESTRAL_DOCKER_TESTS=1 to run Docker isolation integration tests",
)
class TestDockerIntegration(unittest.TestCase):
    def test_host_files_are_not_visible_and_container_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            host_secret = Path(tmp) / "other-run-secret.txt"
            host_secret.write_text("not visible", encoding="utf-8")
            tests = f"""
import os
import unittest
from solution import answer

class T(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 42)

    def test_other_run_is_not_visible(self):
        self.assertFalse(os.path.exists({str(host_secret)!r}))
"""
            result = run_docker_unittest(
                {"solution.py": "def answer():\n    return 42\n"},
                tests,
                timeout_seconds=10,
                image=os.environ.get("ORCHESTRAL_DOCKER_IMAGE", "python:3.11-slim"),
            )
            self.assertEqual(result.returncode, 0, result.output.decode(errors="replace"))
            self.assertFalse(result.timed_out)
            self.assertNotIn(b"not visible", result.output)
            listed = subprocess.run(
                [
                    "docker", "ps", "-a",
                    "--filter", "label=orchestral.sandbox=code-verifier",
                    "--format", "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertNotIn(result.container_name, listed.stdout.splitlines())

    def test_timeout_is_reported_and_container_is_removed(self):
        tests = """
import time
import unittest

class T(unittest.TestCase):
    def test_sleep(self):
        time.sleep(30)
"""
        result = run_docker_unittest(
            {"solution.py": "x = 1\n"},
            tests,
            timeout_seconds=1,
            image=os.environ.get("ORCHESTRAL_DOCKER_IMAGE", "python:3.11-slim"),
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)
        listed = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "label=orchestral.sandbox=code-verifier",
                "--format", "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(result.container_name, listed.stdout.splitlines())

    def test_container_exit_125_is_not_misclassified_as_docker_failure(self):
        tests = """
import os
import unittest

class T(unittest.TestCase):
    def test_exit(self):
        os._exit(125)
"""
        result = run_docker_unittest(
            {"solution.py": "x = 1\n"},
            tests,
            timeout_seconds=10,
            image=os.environ.get("ORCHESTRAL_DOCKER_IMAGE", "python:3.11-slim"),
        )
        self.assertEqual(result.returncode, 125)
        self.assertFalse(result.timed_out)
        listed = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "label=orchestral.sandbox=code-verifier",
                "--format", "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(result.container_name, listed.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
