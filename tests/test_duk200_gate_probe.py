"""TEMPORARY probe for DUK-200. Delete after the CI run is captured.

This test fails on purpose so the ci workflow can be observed with a red
`test` job while `lint` and `types` still report. It is deliberately
ruff-clean so the `lint` job stays green in the same run.
"""

from __future__ import annotations

import unittest


class TestDeliberateRedProbe(unittest.TestCase):
    def test_intentionally_fails_to_prove_gate_independence(self):
        self.assertTrue(
            False,
            "DUK-200 probe: this failure must not stop the lint or types jobs",
        )


if __name__ == "__main__":
    unittest.main()
