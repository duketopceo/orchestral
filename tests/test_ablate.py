"""Tests for the ablate subcommand: sweep parsing and per-value run markers."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import harness


def _args(**over):
    base = dict(
        task="landing-page-coffee",
        orchestrator="deepseek/deepseek-v4-flash-0731",
        worker="z-ai/glm-5.3-flash",
        sweep=None,
        planner="raw",
        judge=None,
        no_judge_cache=False,
        retry_limit=None,
        prompt_variant=None,
        jobs=1,
        dry_run=True,
        json=False,
        runs_dir=None,
        tasks_dir="tasks",
        models_dir="models",
    )
    base.update(over)
    import argparse
    return argparse.Namespace(**base)


class TestAblate(unittest.TestCase):
    def test_retry_limit_sweep_marks_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(sweep="retry_limit=0,1,2", runs_dir=tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                harness.cmd_ablate(args)
            text = out.getvalue()
            self.assertIn("Ablation: retry_limit", text)
            from orchestral.storage import RunStore
            runs = RunStore(tmp).list_runs(limit=None)
            self.assertEqual(len(runs), 3)
            markers = sorted(r.config["sweep"]["value"] for r in runs)
            self.assertEqual(markers, [0, 1, 2])

    def test_prompt_variant_sweep_marks_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(sweep="prompt_variant=terse,detailed", runs_dir=tmp)
            with redirect_stdout(io.StringIO()):
                harness.cmd_ablate(args)
            from orchestral.storage import RunStore
            runs = RunStore(tmp).list_runs(limit=None)
            self.assertEqual(len(runs), 2)
            variants = sorted(r.config["prompt_variant"] for r in runs)
            self.assertEqual(variants, ["detailed", "terse"])

    def test_unknown_knob_exits(self):
        args = _args(sweep="nonsense=1,2", runs_dir=tempfile.mkdtemp())
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                harness.cmd_ablate(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_unknown_prompt_variant_exits(self):
        args = _args(sweep="prompt_variant=nosuchvariant", runs_dir=tempfile.mkdtemp())
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                harness.cmd_ablate(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_too_many_values_exits(self):
        args = _args(sweep="retry_limit=0,1,2,3,4,5,6,7,8,9", runs_dir=tempfile.mkdtemp())
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                harness.cmd_ablate(args)
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
