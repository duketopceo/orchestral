"""Code-reduction U2: entry-point registry parity.

Pins ``orchestral/entrypoints.yaml`` against a fresh scan: every argparse
handler, HTTP route/handler, audit rule, Textual convention method, JS view
import, console script and ``__main__`` guard the registry claims must exist,
and every one the scanner finds must be claimed. Drift in either direction
fails, so the minus-list the dead-code report subtracts cannot silently lag
the code. Mirrors the enumerate-both-sides shape of test_icons.py.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "audit_entrypoints", ROOT / "scripts" / "audit_entrypoints.py"
)
assert _spec and _spec.loader
ae = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ae
_spec.loader.exec_module(ae)


class TestEntrypointRegistry(unittest.TestCase):
    scanned: set[tuple[str, str]]
    claimed: set[tuple[str, str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.scanned = {(e.name, e.kind) for e in ae.scan()}
        cls.claimed = ae.parse_registry(ae.REGISTRY.read_text())

    def test_registry_matches_scan(self) -> None:
        missing = self.scanned - self.claimed
        extra = self.claimed - self.scanned
        self.assertEqual(
            set(),
            missing,
            "unregistered entry points — run scripts/audit_entrypoints.py --write: "
            + ", ".join(sorted(n for n, _ in missing)),
        )
        self.assertEqual(
            set(),
            extra,
            "registry names symbols the scanner no longer finds: "
            + ", ".join(sorted(n for n, _ in extra)),
        )

    def test_scanner_finds_minimum_surface(self) -> None:
        # The parity test above is vacuous if a scanner silently starts
        # returning nothing — pin every kind non-empty so a silent-miss
        # regression (regex/AST drift) is loud at its own kind, not a smaller
        # registry that still passes parity.
        for kind in (
            "cli_command",
            "http_route",
            "http_handler",
            "audit_rule",
            "textual_handler",
            "js_view",
            "getattr_table",
            "main_guard",
            "module_exports",
            "console_script",
        ):
            self.assertTrue(
                any(k == kind for _, k in self.scanned),
                f"scan found no {kind} entries — scanner for that kind is broken",
            )
        for required in ("/api/runs", "/api/overview", "/api/meta"):
            self.assertIn((required, "http_route"), self.claimed)
        self.assertIn(
            ("orchestral/runner.py:_validate_sql", "getattr_table"),
            self.scanned,
            "_CANDIDATE_TASKS getattr dispatch lost its entry",
        )

    def test_drift_detection_fails_on_forged_registry(self) -> None:
        # Exercises the detector path: an entry the scan cannot produce must
        # make main() report staleness.
        forged = ae.render(
            [*ae.scan(), ae.Entry("harness.cmd_fake", "cli_command", 'subparser "fake"')]
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False
        ) as f, mock.patch.object(ae, "REGISTRY", Path(f.name)):
            f.write(forged)
            f.flush()
            self.assertEqual(ae.main([]), 1)
        Path(f.name).unlink(missing_ok=True)

    def test_main_returns_zero_on_faithful_registry(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False
        ) as f, mock.patch.object(ae, "REGISTRY", Path(f.name)):
            f.write(ae.render(ae.scan()))
            f.flush()
            self.assertEqual(ae.main([]), 0)
        Path(f.name).unlink(missing_ok=True)

    def test_write_round_trips(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False
        ) as f, mock.patch.object(ae, "REGISTRY", Path(f.name)):
            self.assertEqual(ae.main(["--write"]), 0)
            written = Path(f.name).read_text()
        Path(f.name).unlink(missing_ok=True)
        self.assertEqual(ae.parse_registry(written), self.scanned)

    def test_patterns_documented(self) -> None:
        text = ae.REGISTRY.read_text()
        for p in ae.PATTERNS:
            self.assertIn(f"kind: {p['kind']}", text)


class TestScanner(unittest.TestCase):
    def test_cli_scanner_reads_synthetic_source(self) -> None:
        src = (
            "import argparse\n"
            "sub = argparse.ArgumentParser().add_subparsers()\n"
            "wibble = sub.add_parser('wibble')\n"
            "wibble.set_defaults(func=cmd_wibble)\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(src)
            path = Path(f.name)
        try:
            ae._parse.cache_clear()
            entries = ae._cli_commands(path)
        finally:
            path.unlink()
        self.assertEqual(
            [("harness.cmd_wibble", 'subparser "wibble"')],
            [(e.name, e.via) for e in entries],
        )

    def test_main_guard_excludes_tests_and_untracked(self) -> None:
        # tests/*.py __main__ blocks are covered by unittest_discovery —
        # without the skip they'd flood the registry; runs/ artifacts are
        # gitignored and unreachable via git ls-files.
        names = {e.name for e in ae._main_guards(ROOT)}
        self.assertTrue(all(not n.startswith("tests/") for n in names))
        self.assertTrue(all(not n.startswith("runs/") for n in names))


if __name__ == "__main__":
    unittest.main()
