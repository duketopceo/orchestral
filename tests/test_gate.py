"""`harness.py gate` — the CI eval gate over compare_payload verdicts."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert SPEC and SPEC.loader
corpus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus)

_TMP: tempfile.TemporaryDirectory[str] | None = None
_CORPUS: Path


def setUpModule():
    global _TMP, _CORPUS
    _TMP = tempfile.TemporaryDirectory()
    _CORPUS = Path(_TMP.name) / "corpus"
    corpus.build_corpus(_CORPUS, "full")


def tearDownModule():
    if _TMP:
        _TMP.cleanup()


def _gate(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GROQ_API_KEY", "OPENROUTER_API_KEY")}
    return subprocess.run(
        [sys.executable, str(ROOT / "harness.py"), "--runs-dir", str(_CORPUS),
         "gate", *args],
        capture_output=True, text=True, env=env, cwd=ROOT)


class TestGate(unittest.TestCase):
    def test_regressed_cell_fails(self):
        """corpus-main:r0 -> r1 has a real Wilson-regressed cell (bugfix-lru
        on orch-b|worker-hot) — the gate must name it and exit 1."""
        r = _gate("--baseline", "corpus-main:r0", "--candidate", "corpus-main:r1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("regressed", r.stdout)
        self.assertIn("corpus-bugfix-lru", r.stdout)
        self.assertIn("gate: FAILED", r.stdout)

    def test_clean_compare_passes(self):
        r = _gate("--baseline", "corpus-main:r1", "--candidate", "corpus-main:r2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gate: passed", r.stdout)

    def test_empty_comparison_fails(self):
        """No shared cells is a gate that sees nothing — it must not pass."""
        r = _gate("--baseline", "corpus-dry", "--candidate", "corpus-failed")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("shared cell", r.stdout)

    def test_same_group_is_blocked(self):
        r = _gate("--baseline", "corpus-dry", "--candidate", "corpus-dry")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_json_carries_failures(self):
        r = _gate("--baseline", "corpus-main:r0", "--candidate", "corpus-main:r1",
                  "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["verdicts"].get("regressed"), 1)
        self.assertTrue(payload["gate_failures"])
        self.assertEqual(payload["fail_on"], ["regressed"])

    def test_max_cost_increase(self):
        """r1 -> r2 passes on verdicts but its ~3% cost drift trips a zero
        tolerance budget."""
        ok = _gate("--baseline", "corpus-main:r1", "--candidate", "corpus-main:r2")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        r = _gate("--baseline", "corpus-main:r1", "--candidate", "corpus-main:r2",
                  "--max-cost-increase", "0")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cost increase", r.stdout)

    def test_fail_on_widens_the_net(self):
        """--fail-on one-sided makes an all-one-sided compare a failure for
        a different stated reason than min-shared."""
        r = _gate("--baseline", "corpus-dry", "--candidate", "corpus-failed",
                  "--fail-on", "one-sided", "--json")
        self.assertEqual(r.returncode, 1)
        payload = json.loads(r.stdout)
        kinds = [f for f in payload["gate_failures"] if "one-sided" in f]
        self.assertTrue(kinds)


if __name__ == "__main__":
    unittest.main()
