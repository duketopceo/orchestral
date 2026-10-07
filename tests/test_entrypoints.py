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

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "audit_entrypoints", ROOT / "scripts" / "audit_entrypoints.py"
)
assert _spec and _spec.loader
ae = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ae
_spec.loader.exec_module(ae)

REGISTRY = ROOT / "orchestral" / "entrypoints.yaml"


class TestEntrypointRegistry(unittest.TestCase):
    def test_registry_matches_scan(self) -> None:
        scanned = {(e.name, e.kind) for e in ae.scan()}
        claimed = ae.parse_registry(REGISTRY.read_text())
        missing = scanned - claimed
        extra = claimed - scanned
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

    def test_every_cli_command_registered(self) -> None:
        claimed = ae.parse_registry(REGISTRY.read_text())
        cmds = {e.name for e in ae.scan() if e.kind == "cli_command"}
        self.assertTrue(cmds, "scan found no cli commands — scanner broken")
        self.assertEqual(cmds, {n for n, k in claimed if k == "cli_command"})

    def test_routes_registered(self) -> None:
        claimed = ae.parse_registry(REGISTRY.read_text())
        routes = {e.name for e in ae.scan() if e.kind == "http_route"}
        for required in ("/api/runs", "/api/overview", "/api/meta"):
            self.assertIn((required, "http_route"), claimed)
        self.assertTrue(routes <= {n for n, k in claimed if k == "http_route"})

    def test_synthetic_command_fails_drift(self) -> None:
        # A new cmd_* handler the registry does not know must not match.
        scanned = {(e.name, e.kind) for e in ae.scan()}
        forged = scanned | {("harness.cmd_fake", "cli_command")}
        self.assertNotEqual(ae.parse_registry(REGISTRY.read_text()), forged)

    def test_patterns_documented(self) -> None:
        text = REGISTRY.read_text()
        for kind in (
            "dunder_protocol",
            "unittest_discovery",
            "callback_argument",
            "getattr_proxy",
        ):
            self.assertIn(f"kind: {kind}", text)


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
            entries = ae._cli_commands(path)
        finally:
            path.unlink()
        self.assertEqual(
            [("harness.cmd_wibble", 'subparser "wibble"')],
            [(e.name, e.via) for e in entries],
        )


if __name__ == "__main__":
    unittest.main()
