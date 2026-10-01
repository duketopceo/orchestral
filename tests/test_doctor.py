"""Doctor preflight + the provider-env gate's isolated-runtime check."""

from __future__ import annotations

import argparse
import os
import unittest
from unittest.mock import patch

import harness
from orchestral.doctor import _env_checks, _Result, run_doctor


class TestIsolatedRuntimeEnvGate(unittest.TestCase):
    """_check_provider_envs must demand the E2B contract when the isolated
    runtime is configured — it fails closed mid-run otherwise."""

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(dry_run=False, allow_agent_exec=False)

    def test_isolated_runtime_requires_e2b_env(self) -> None:
        env = {"ORCHESTRAL_CODE_RUNTIME": "isolated"}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            harness._check_provider_envs(self._args())

    def test_isolated_runtime_with_contract_passes(self) -> None:
        env = {
            "ORCHESTRAL_CODE_RUNTIME": "isolated",
            "E2B_DOMAIN": "cube.app",
            "E2B_API_KEY": "k",
        }
        with patch.dict(os.environ, env, clear=True):
            harness._check_provider_envs(self._args())  # no raise

    def test_runtime_unset_does_not_require_e2b_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            harness._check_provider_envs(self._args())  # no raise

    def test_dry_run_skips_the_gate(self) -> None:
        env = {"ORCHESTRAL_CODE_RUNTIME": "isolated"}
        with patch.dict(os.environ, env, clear=True):
            harness._check_provider_envs(
                argparse.Namespace(dry_run=True, allow_agent_exec=False)
            )


class TestDoctorEnvChecks(unittest.TestCase):
    def test_empty_env_fails_required_checks(self) -> None:
        result = _Result()
        with patch.dict(os.environ, {}, clear=True):
            _env_checks(result)
        failed = {c.name for c in result.failed}
        self.assertIn("ORCHESTRAL_CODE_RUNTIME", failed)
        self.assertIn("E2B_DOMAIN", failed)
        self.assertIn("E2B_API_KEY", failed)

    def test_self_hosted_domain_requires_ssl_cert_file(self) -> None:
        env = {
            "ORCHESTRAL_CODE_RUNTIME": "isolated",
            "E2B_DOMAIN": "cube.app",
            "E2B_API_KEY": "k",
        }
        result = _Result()
        with patch.dict(os.environ, env, clear=True):
            _env_checks(result)
        self.assertIn("SSL_CERT_FILE", {c.name for c in result.failed})

    def test_hosted_domain_does_not_require_ssl_cert_file(self) -> None:
        env = {
            "ORCHESTRAL_CODE_RUNTIME": "isolated",
            "E2B_DOMAIN": "e2b.dev",
            "E2B_API_KEY": "k",
        }
        result = _Result()
        with patch.dict(os.environ, env, clear=True):
            _env_checks(result)
        self.assertNotIn("SSL_CERT_FILE", {c.name for c in result.failed})
        self.assertFalse(result.failed)

    def test_doctor_exit_code(self) -> None:
        # No env + no probe request: every required env check fails -> 1.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run_doctor(probe=False, egress=False), 1)


if __name__ == "__main__":
    unittest.main()
