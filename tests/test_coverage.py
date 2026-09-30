"""Tests for the coverage ledger (orchestral/coverage.py) and publish marks."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import harness
from orchestral.coverage import coverage_rows, coverage_summary
from orchestral.experiment import Matrix
from orchestral.storage import RunMeta, RunStore

MATRIX = Matrix(name="m", orchestrators=["o/m"], workers=["w/m"], tasks=["t"])
CELL_KEY = "t:o/m:w/m"


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t", "worker": "w/m",
        "status": "finished", "started_at": "2026-01-01T00:00:00",
        "total_cost_usd": 0.01, "latency_ms": 100.0, "passes": True,
        "score": 0.8, "run_group": "g", "config": {},
    }
    base.update(kw)
    return RunMeta(**base)


def _seed(store: RunStore, arm: str, n: int, *, passes: bool = True,
          group_prefix: str = "m", offset: int = 0) -> None:
    jev = arm == "jev"
    for k in range(n):
        i = offset + k + 1
        store.index_meta(_meta(
            run_id=f"{group_prefix}-{arm}-{i}-{passes}", passes=passes,
            run_group=f"{group_prefix}:{CELL_KEY}:{arm}",
            replicate=i, config={"jev_assist": jev},
        ))


class TestCoverageRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_cell(self):
        rows = coverage_rows(self.store, MATRIX)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.state, r.verdict, r.posted), ("pending", "pending", False))
        self.assertIn("no billing history", r.warnings[0])

    def test_partial_then_done(self):
        _seed(self.store, "baseline", 2)
        _seed(self.store, "jev", 2)
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertEqual(r.state, "partial")
        self.assertEqual((r.baseline_n, r.jev_n), (2, 2))
        _seed(self.store, "baseline", 3, offset=2)
        _seed(self.store, "jev", 3, offset=2)
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertEqual(r.state, "done")  # 5 pairs hits the floor target

    def test_arm_split_reads_config_jev_assist(self):
        _seed(self.store, "baseline", 3, passes=False)
        _seed(self.store, "jev", 3, passes=True)
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertEqual((r.baseline_passes, r.jev_passes), (0, 3))
        self.assertEqual(r.verdict, "lift")
        self.assertIsNotNone(r.diff)
        self.assertGreater(r.diff[0], 0)

    def test_aborted_is_persisted_not_derived(self):
        _seed(self.store, "baseline", 1)
        _seed(self.store, "jev", 1)
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertEqual(r.state, "partial")  # a crash-partial stays partial
        self.store.set_annotation("cell-state", f"m:{CELL_KEY}", "aborted",
                                  note="3/5 runs errored")
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertEqual(r.state, "aborted")
        self.assertEqual(r.note, "3/5 runs errored")

    def test_dry_run_rows_excluded(self):
        self.store.index_meta(_meta(
            run_id="dry", run_group=f"m:{CELL_KEY}:baseline", dry_run=True,
            config={"jev_assist": False},
        ))
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertEqual(r.state, "pending")

    def test_foreign_group_runs_dont_leak(self):
        _seed(self.store, "baseline", 5, group_prefix="other-matrix")
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertEqual(r.state, "pending")  # other experiment, not this one


class TestPublishMark(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, **kw):
        base = {"runs_dir": self.tmp.name, "target": CELL_KEY,
                "url": None, "note": "", "clear": False}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_mark_and_read(self):
        _seed(self.store, "baseline", 5)
        _seed(self.store, "jev", 5)
        harness.cmd_publish_mark(self._args(url="https://x.test/post/1"))
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertTrue(r.posted)
        self.assertIn("x.test/post/1", r.posted_note)

    def test_clear(self):
        harness.cmd_publish_mark(self._args(url="u"))
        harness.cmd_publish_mark(self._args(clear=True))
        (r,) = coverage_rows(self.store, MATRIX)
        self.assertFalse(r.posted)

    def test_summary_counts(self):
        _seed(self.store, "baseline", 5)
        _seed(self.store, "jev", 5)
        harness.cmd_publish_mark(self._args())
        summ = coverage_summary(coverage_rows(self.store, MATRIX))
        self.assertEqual(summ["posted"], 1)
        self.assertEqual(summ["states"]["done"], 1)

    def test_bad_kind_still_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_annotation("nonsense", "x", "posted")


class TestCoverageCLI(unittest.TestCase):
    def test_empty_matrix_prints_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            mpath = Path(tmp) / "m.yaml"
            mpath.write_text(
                "name: m\norchestrators: [o/m]\nworkers: [w/m]\ntasks: [t]\n",
                encoding="utf-8")
            args = argparse.Namespace(
                matrix=str(mpath), runs_dir=str(Path(tmp) / "runs"),
                budget=0.0, diff_eps=0.15, json=False,
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                harness.cmd_coverage(args)
            out = buf.getvalue()
            self.assertIn("pending", out)
            self.assertIn(CELL_KEY, out)


if __name__ == "__main__":
    unittest.main()
