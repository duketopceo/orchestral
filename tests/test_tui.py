"""Tests for the TUI: pure domain state, runner cancellation, and
textual pilot interaction tests."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.tui.state import (
    Job,
    JobStatus,
    filter_runs,
    fmt_cost,
    fmt_ms,
    fmt_tokens,
    pass_label,
)


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role, input_price_per_mtok=0.03, output_price_per_mtok=0.10)


def _seed_run(runs_dir: str, **runner_kwargs) -> str:
    store = RunStore(runs_dir)
    meta = Runner(dry_run=True, runs_dir=runs_dir, store=store, **runner_kwargs).run(
        TaskSpec(id="t-task", type="html", prompt="p"),
        _model("o/model", "orchestrator"), _model("w/model", "worker"),
    )
    return meta.run_id


class TestJobLifecycle(unittest.TestCase):
    def test_happy_path(self):
        j = Job(label="x")
        j.transition(JobStatus.RUNNING)
        j.transition(JobStatus.SUCCEEDED, "done")
        self.assertFalse(j.active)

    def test_illegal_transition_rejected(self):
        j = Job(label="x")
        with self.assertRaises(ValueError):
            j.transition(JobStatus.SUCCEEDED)  # queued can't skip running
        j.transition(JobStatus.RUNNING)
        j.transition(JobStatus.FAILED)
        with self.assertRaises(ValueError):
            j.transition(JobStatus.RUNNING)  # terminal is terminal

    def test_cancel(self):
        j = Job(label="x")
        self.assertTrue(j.cancel())
        self.assertTrue(j.cancel_event.is_set())
        j.transition(JobStatus.RUNNING)
        j.transition(JobStatus.CANCELLED)
        self.assertFalse(j.cancel())  # already terminal


class TestFiltering(unittest.TestCase):
    def test_matches_relevant_fields(self):
        store_dir = tempfile.mkdtemp()
        rid = _seed_run(store_dir, run_group="exp1")
        runs = RunStore(store_dir).list_runs(limit=None)
        self.assertEqual(filter_runs(runs, "t-task"), runs)
        self.assertEqual(filter_runs(runs, "exp1"), runs)
        self.assertEqual(filter_runs(runs, "w/model"), runs)
        self.assertEqual(filter_runs(runs, "finished"), runs)
        self.assertEqual(filter_runs(runs, rid[:8]), runs)
        self.assertEqual(filter_runs(runs, "nope"), [])
        self.assertEqual(filter_runs(runs, ""), runs)


class TestFormatters(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(fmt_ms(None), "-")
        self.assertEqual(fmt_ms(500), "500ms")
        self.assertEqual(fmt_ms(12_000), "12.0s")
        self.assertEqual(fmt_cost(0.01234), "$0.0123")
        self.assertEqual(fmt_tokens(999), "999")
        self.assertEqual(fmt_tokens(1500), "1.5k")
        self.assertEqual(pass_label(True), ("pass", "ok"))
        self.assertEqual(pass_label(None), ("-", "muted"))


class TestRunnerCancel(unittest.TestCase):
    def test_pre_cancelled_run_records_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = threading.Event()
            ev.set()
            store = RunStore(tmp)
            meta = Runner(dry_run=True, runs_dir=tmp, store=store, cancel_event=ev).run(
                TaskSpec(id="t", type="html", prompt="p"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            self.assertEqual(meta.status, "cancelled")
            self.assertEqual(meta.failure_reason, "cancelled")
            # metrics.json still written for cancelled runs
            self.assertTrue((Path(meta.run_dir) / "metrics.json").exists())


try:
    import textual  # noqa: F401

    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed (pip install 'orchestral[tui]')")
class TestAppPilot(unittest.IsolatedAsyncioTestCase):
    async def _pump(self, app, pilot, until, timeout=5.0):
        """Wait for a condition with real time for thread workers."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await pilot.pause()
            await asyncio.sleep(0.02)
            if until():
                return True
        return False

    async def test_mount_and_table(self):
        from textual.widgets import DataTable

        from orchestral.tui.app import OrchestralApp

        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp)
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                table = app.query_one("#runs-table", DataTable)
                ok = await self._pump(app, pilot, lambda: table.row_count == 1)
                self.assertTrue(ok, "runs table never populated")

    async def test_filter_and_detail(self):
        from textual.widgets import DataTable

        from orchestral.tui.app import OrchestralApp
        from orchestral.tui.screens import RunDetailScreen

        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp)
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                table = app.query_one("#runs-table", DataTable)
                self.assertTrue(await self._pump(app, pilot, lambda: table.row_count == 1))
                # filter narrows
                await pilot.press("/")
                box = app.query_one("#filter-box")
                box.value = "nomatch"
                await pilot.pause()
                self.assertEqual(table.row_count, 0)
                box.value = ""
                await pilot.pause()
                self.assertEqual(table.row_count, 1)
                # esc closes filter, enter opens detail
                await pilot.press("escape")
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, RunDetailScreen)

    async def test_help_and_groups(self):
        from orchestral.tui.app import OrchestralApp
        from orchestral.tui.screens import GroupsScreen, HelpScreen

        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp, run_group="g1", replicate=1)
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                await pilot.press("?")
                await pilot.pause()
                self.assertIsInstance(app.screen, HelpScreen)
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("g")
                await pilot.pause()
                self.assertIsInstance(app.screen, GroupsScreen)

    async def test_launch_dry_run_job(self):
        from textual.widgets import DataTable

        from orchestral.tui.app import OrchestralApp
        from orchestral.tui.screens import LaunchScreen

        with tempfile.TemporaryDirectory() as tmp:
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                await pilot.press("n")
                await pilot.pause()
                self.assertIsInstance(app.screen, LaunchScreen)
                # defaults: first task, first orch/worker, dry-run on
                btn = app.screen.query_one("#launch-go")
                btn.press()
                await pilot.pause()
                ok = await self._pump(
                    app, pilot,
                    lambda: app.jobs and not app.jobs[0].active,
                )
                self.assertTrue(ok, "job never finished")
                self.assertEqual(app.jobs[0].status, JobStatus.SUCCEEDED)
                table = app.query_one("#runs-table", DataTable)
                self.assertTrue(await self._pump(app, pilot, lambda: table.row_count == 1))


if __name__ == "__main__":
    unittest.main()
