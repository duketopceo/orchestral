"""Global directory flags must reach every subcommand, in either position.

argparse copies subparser defaults onto the shared namespace after the
top-level value is parsed, so a subparser re-declaring `--runs-dir` with
`default="runs"` silently discarded `harness.py --runs-dir X scrub`. These
tests drive the real parser because a test that builds its own Namespace
cannot see the defect at all.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import harness

# Derived from the source of truth in harness.py so the invariant test cannot
# drift from the flags the CLI actually declares.
GLOBAL_DIR_DESTS = tuple(f.lstrip("-").replace("-", "_") for f in harness.GLOBAL_DIR_FLAGS)


def _subparsers(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    out: list[argparse.ArgumentParser] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            out.extend(action.choices.values())
    return out


class TestNoSubparserShadowsGlobalDirFlags(unittest.TestCase):
    """Structural invariant: no subparser may default a global dir flag.

    A shadowing subparser sets the dest to its own default on every run, so a
    top-level value can never win. Catches the class of bug, not one instance.
    """

    def test_subparser_redeclarations_use_suppress(self):
        top = harness.build_parser()
        shadowing: list[str] = []
        for sp in _subparsers(top):
            for action in sp._actions:
                if action.dest in GLOBAL_DIR_DESTS and action.default is not argparse.SUPPRESS:
                    shadowing.append(f"{sp.prog} --{action.dest.replace('_', '-')}")
        self.assertEqual(shadowing, [], f"subparsers shadow a global dir flag: {shadowing}")

    def test_shadowed_commands_still_accept_the_subparser_flag(self):
        # The fix must not break the spelling that already worked.
        args = harness.build_parser().parse_args(
            ["scrub", "--runs-dir", "X", "--scrub-dir", "Y"]
        )
        self.assertEqual(args.runs_dir, "X")
        self.assertEqual(args.scrub_dir, "Y")


class TestTopLevelDirFlagsReachSubcommands(unittest.TestCase):
    """Every subcommand must see the top-level value, in either position."""

    # (argv, dest) — the subcommand plus the global dir flag it must honour
    CASES = (
        (["scrub"], "runs_dir"),
        (["calibrate", "--labels", "l.yaml"], "runs_dir"),
        (["export"], "runs_dir"),
        (["shots"], "runs_dir"),
        (["dashboard"], "runs_dir"),
        (["tui"], "runs_dir"),
        (["report"], "runs_dir"),
        (["prices"], "runs_dir"),
        (["history"], "runs_dir"),
        (["init"], "runs_dir"),
        (["serve"], "runs_dir"),
    )

    def test_top_level_flag_is_honoured(self):
        p = harness.build_parser()
        for argv, dest in self.CASES:
            with self.subTest(cmd=argv[0]):
                args = p.parse_args(["--runs-dir", "GLOBAL", *argv])
                self.assertEqual(getattr(args, dest), "GLOBAL")

    def test_default_applies_when_neither_form_is_given(self):
        p = harness.build_parser()
        for argv, dest in self.CASES:
            with self.subTest(cmd=argv[0]):
                self.assertEqual(getattr(p.parse_args(argv), dest), "runs")

    def test_subparser_flag_wins_over_top_level(self):
        p = harness.build_parser()
        args = p.parse_args(["--runs-dir", "GLOBAL", "scrub", "--runs-dir", "LOCAL"])
        self.assertEqual(args.runs_dir, "LOCAL")

    def test_tasks_dir_and_models_dir_reach_the_run_commands(self):
        p = harness.build_parser()
        base = ["run", "--task", "t", "--orchestrator", "o", "--worker", "w", "--dry-run"]
        args = p.parse_args(["--tasks-dir", "T", "--models-dir", "M", *base])
        self.assertEqual(args.tasks_dir, "T")
        self.assertEqual(args.models_dir, "M")


def _write_run(runs_dir: Path, name: str) -> None:
    run_dir = runs_dir / "2026" / "01" / name
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": name, "status": "finished", "total_cost_usd": 0.01}),
        encoding="utf-8",
    )
    (run_dir / "worker-0.json").write_text("{}", encoding="utf-8")


def _cli(*argv: str) -> tuple[int, str, str]:
    """Run harness.main() with argv, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with patch.object(harness.sys, "argv", ["harness.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            harness.main()
        except SystemExit as stop:
            return stop.code or 0, out.getvalue(), err.getvalue()
    return 0, out.getvalue(), err.getvalue()


class TestScrubUsesTheNamedRunsDir(unittest.TestCase):
    def test_top_level_flag_form_scrubs_the_named_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "actual-runs"
            _write_run(source, "runA")
            _write_run(source, "runB")
            out_dir = root / "pub"

            code, out, _ = _cli("--runs-dir", str(source), "scrub", "--scrub-dir", str(out_dir))

            self.assertEqual(code, 0)
            self.assertIn("Scrubbed 2 runs", out)
            # the two runs landed under the tree mirror, not a default runs/
            self.assertTrue((out_dir / "2026" / "01" / "runA" / "run.json").is_file())
            self.assertTrue((out_dir / "2026" / "01" / "runB" / "run.json").is_file())
            self.assertEqual(len(json.loads((out_dir / "manifest.json").read_text())), 2)

    def test_subparser_flag_form_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "actual-runs"
            _write_run(source, "runA")
            out_dir = root / "pub"

            code, out, _ = _cli("scrub", "--runs-dir", str(source), "--scrub-dir", str(out_dir))

            self.assertEqual(code, 0)
            self.assertIn("Scrubbed 1 run", out)
            self.assertTrue((out_dir / "2026" / "01" / "runA" / "run.json").is_file())


class TestScrubFailsLoudlyOnAnUnusableSource(unittest.TestCase):
    """"Scrubbed 0 runs" is indistinguishable from the shadowing bug."""

    def test_missing_source_directory_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code, out, err = _cli(
                "--runs-dir", str(root / "nope"), "scrub", "--scrub-dir", str(root / "pub")
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("runs directory not found", err)
            self.assertIn("Nothing was written", err)

    def test_empty_source_directory_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "empty").mkdir()
            code, out, err = _cli(
                "--runs-dir", str(root / "empty"), "scrub", "--scrub-dir", str(root / "pub")
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("no runs found", err)

    def test_a_bad_source_does_not_clear_the_previous_publication(self):
        # scrub_all wipes the output directory before copying. A mistyped source
        # must not destroy the last good runs-pub/.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "actual-runs"
            _write_run(source, "runA")
            out_dir = root / "pub"
            self.assertEqual(_cli("scrub", "--runs-dir", str(source), "--scrub-dir", str(out_dir))[0], 0)
            previous = out_dir / "2026" / "01" / "runA" / "run.json"
            self.assertTrue(previous.is_file())

            code, _, _ = _cli("--runs-dir", str(root / "nope"), "scrub", "--scrub-dir", str(out_dir))

            self.assertEqual(code, 1)
            self.assertTrue(previous.is_file(), "a failed scrub destroyed the previous publication")


if __name__ == "__main__":
    unittest.main()
