"""Failure-taxonomy tests: category labels and retry policy (U1)."""

from __future__ import annotations

import subprocess
import unittest

from orchestral.taxonomy import CATEGORIES, classify_exception, retryable


class _NamedError(Exception):
    """Base for class-name-matched test errors."""


class ExecutorPreflightError(_NamedError):
    pass


class ExecutorExitError(_NamedError):
    pass


class ExecutorTimeoutError(_NamedError):
    pass


class ExecutorNoOutputError(_NamedError):
    pass


class SpawnFailedError(_NamedError):
    pass


class WorkspaceError(_NamedError):
    pass


class TestExecutorCategories(unittest.TestCase):
    def test_dedicated_exception_names(self):
        cases = {
            ExecutorPreflightError("missing binary"): "executor_preflight",
            ExecutorExitError("exit 1"): "executor_exit",
            ExecutorTimeoutError("timed out"): "executor_timeout",
            ExecutorNoOutputError("no diff"): "executor_no_output",
            SpawnFailedError("spawn failed"): "spawn_failed",
            WorkspaceError("symlink"): "workspace",
        }
        for exc, expected in cases.items():
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(classify_exception(exc), expected)

    def test_subprocess_timeout_expired(self):
        exc = subprocess.TimeoutExpired(cmd=["cli"], timeout=5)
        self.assertEqual(classify_exception(exc), "executor_timeout")

    def test_subprocess_called_process_error(self):
        exc = subprocess.CalledProcessError(returncode=1, cmd=["cli"])
        self.assertEqual(classify_exception(exc), "executor_exit")

    def test_categories_registered(self):
        for cat in ("executor_preflight", "executor_exit", "executor_timeout",
                    "executor_no_output", "spawn_failed", "workspace"):
            self.assertIn(cat, CATEGORIES)


class TestRetryPolicy(unittest.TestCase):
    def test_preflight_never_retries(self):
        self.assertFalse(retryable("executor_preflight"))

    def test_spawn_failed_never_retries(self):
        self.assertFalse(retryable("spawn_failed"))

    def test_config_never_retries(self):
        self.assertFalse(retryable("config"))

    def test_submitted_job_never_retries(self):
        self.assertFalse(retryable("submitted_job"))

    def test_transient_categories_retry(self):
        for cat in ("timeout", "rate_limit", "transport", "executor_exit",
                    "executor_timeout", "executor_no_output", "workspace",
                    "empty_output", "malformed_output", "unknown"):
            with self.subTest(cat=cat):
                self.assertTrue(retryable(cat))


if __name__ == "__main__":
    unittest.main()
