"""Tests for the TUI: pure domain state, runner cancellation, live-view
event derivation, leaderboard sorting, and textual pilot interaction tests."""

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
    event_row,
    filter_runs,
    fmt_cost,
    fmt_elapsed,
    fmt_ms,
    fmt_tokens,
    live_totals,
    pass_label,
    run_phase,
    sort_leaderboard,
    tail_events,
    worker_states,
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


class TestTailEvents(unittest.TestCase):
    def test_incremental_and_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text('{"type": "a", "sequence": 1}\n', encoding="utf-8")
            events, offset = tail_events(p, 0)
            self.assertEqual([e["type"] for e in events], ["a"])
            # appending a partial line leaves it unread
            with p.open("a", encoding="utf-8") as fh:
                fh.write('{"type": "b", "sequen')
            events2, offset2 = tail_events(p, offset)
            self.assertEqual(events2, [])
            self.assertEqual(offset2, offset)
            # completing the line makes it readable
            with p.open("a", encoding="utf-8") as fh:
                fh.write('ce": 2}\n{"type": "c", "sequence": 3}\n')
            events3, _ = tail_events(p, offset2)
            self.assertEqual([e["type"] for e in events3], ["b", "c"])

    def test_missing_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            events, offset = tail_events(Path(tmp) / "nope.jsonl", 0)
            self.assertEqual((events, offset), ([], 0))
            p = Path(tmp) / "bad.jsonl"
            p.write_text('{"type": "ok"}\nnot json\n', encoding="utf-8")
            events, _ = tail_events(p, 0)
            self.assertEqual([e["type"] for e in events], ["ok", "malformed"])


class TestEventDerivation(unittest.TestCase):
    def _lifecycle(self):
        return [
            {"type": "run.created", "sequence": 1},
            {"type": "orchestrator.started", "sequence": 2},
            {"type": "orchestrator.completed", "sequence": 3},
            {"type": "delegation.created", "sequence": 4, "output": {"subtasks": 2}},
            {"type": "worker.started", "sequence": 5, "worker_id": "worker-0", "output": {"description": "inspect"}},
            {"type": "worker.started", "sequence": 6, "worker_id": "worker-1", "output": {"description": "fix"}},
            {"type": "worker.completed", "sequence": 7, "worker_id": "worker-0", "output": {"attempts": 1}},
            {"type": "worker.failed", "sequence": 8, "worker_id": "worker-1", "output": {"error_category": "provider_error"}},
            {"type": "synthesis.started", "sequence": 9},
            {"type": "evaluation.completed", "sequence": 10, "output": {"passes": True, "score": 0.9}},
            {"type": "run.completed", "sequence": 11, "output": {"status": "finished"}},
        ]

    def test_run_phase(self):
        events = self._lifecycle()
        self.assertEqual(run_phase(events[:2]), "planning")
        self.assertEqual(run_phase(events[:8]), "delegating")
        self.assertEqual(run_phase(events), "finished")
        self.assertEqual(run_phase([]), "starting")

    def test_worker_states(self):
        states = worker_states(self._lifecycle())
        self.assertEqual(states, {"worker-0": "done", "worker-1": "failed"})

    def test_live_totals(self):
        events = [
            {"type": "llm_call", "cost": {"usd": 0.001, "input_tokens": 100, "output_tokens": 50}},
            {"type": "llm_call", "cost": {"usd": 0.002, "input_tokens": 200, "output_tokens": 60}},
            {"type": "worker.started"},
        ]
        cost, toks = live_totals(events)
        self.assertAlmostEqual(cost, 0.003)
        self.assertEqual(toks, 410)

    def test_event_row(self):
        ts, etype, wid, detail = event_row(
            {"type": "worker.started", "timestamp": "2026-09-15T01:02:03Z", "worker_id": "worker-0", "output": {"description": "inspect the failing test"}}
        )
        self.assertEqual((ts, etype, wid), ("01:02:03", "worker.started", "worker-0"))
        self.assertIn("inspect", detail)
        _ts, _t, _w, detail = event_row({"type": "delegation.created", "output": {"subtasks": 3}})
        self.assertIn("3 subtasks", detail)

    def test_fmt_elapsed(self):
        self.assertEqual(fmt_elapsed("2026-09-15T01:00:00+00:00", "2026-09-15T01:06:42+00:00"), "06:42")
        self.assertEqual(fmt_elapsed("2026-09-15T01:00:00+00:00", "2026-09-15T02:06:42+00:00"), "1:06:42")
        self.assertEqual(fmt_elapsed(None), "-")
        self.assertEqual(fmt_elapsed("garbage"), "-")


class TestLeaderboardSort(unittest.TestCase):
    def test_sort_leaderboard(self):
        rows = [
            {"orchestrator": "a", "worker": "x", "cost_per_pass": 0.01, "pass_rate": 0.5, "judge_score_median": None},
            {"orchestrator": "b", "worker": "y", "cost_per_pass": None, "pass_rate": 0.9, "judge_score_median": 0.8},
            {"orchestrator": "c", "worker": "z", "cost_per_pass": 0.005, "pass_rate": 0.7, "judge_score_median": 0.5},
        ]
        by_cost = [r["orchestrator"] for r in sort_leaderboard(rows, "cost_per_pass")]
        self.assertEqual(by_cost, ["c", "a", "b"])  # None (never passed) last
        by_pass = [r["orchestrator"] for r in sort_leaderboard(rows, "pass_rate")]
        self.assertEqual(by_pass, ["b", "c", "a"])
        by_score = [r["orchestrator"] for r in sort_leaderboard(rows, "judge_score_median")]
        self.assertEqual(by_score, ["b", "c", "a"])

    def test_sort_leaderboard_partitions_low_sample(self):
        """A thin row tails every ordering — even when its metric wins."""
        rows = [
            {"orchestrator": "a", "worker": "x", "cost_per_pass": 0.50,
             "pass_rate": 0.2, "judge_score_median": 0.1, "cost_median": 0.5,
             "duration_median_ms": 9000, "low_sample": False},
            {"orchestrator": "b", "worker": "y", "cost_per_pass": 0.001,
             "pass_rate": 1.0, "judge_score_median": 1.0, "cost_median": 0.001,
             "duration_median_ms": 1, "low_sample": True},
        ]
        for key in ("cost_per_pass", "pass_rate", "judge_score_median",
                    "cost_median", "duration_median_ms"):
            order = [r["orchestrator"] for r in sort_leaderboard(rows, key)]
            self.assertEqual(order, ["a", "b"], key)


class TestOnRunCreated(unittest.TestCase):
    def test_callback_fires_with_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen: list[str] = []
            meta = Runner(
                dry_run=True, runs_dir=tmp, store=RunStore(tmp),
                on_run_created=seen.append,
            ).run(
                TaskSpec(id="t", type="html", prompt="p"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            self.assertEqual(seen, [meta.run_id])


try:
    import textual  # noqa: F401

    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed (pip install 'orchestral[tui]')")
class TestStatusBar(unittest.TestCase):
    """_render_text must not read an attribute the class never assigns.

    17f6c543 removed the `set_message` writer and its `self._message = ""`
    initialiser but left the reader behind, so every status-strip refresh
    raised AttributeError. Asserting on rendered text keeps a reader-without-
    a-writer from reaching main again.
    """

    def _bar(self, runs=0, cost=0.0, jobs=None):
        from orchestral.tui.widgets import StatusBar

        bar = StatusBar()
        bar.set_counts(runs, cost)
        bar.set_jobs(jobs or [])
        return bar

    def _text(self, bar):
        return str(bar.visual)

    def test_counts_render_without_a_message_attribute(self):
        bar = self._bar(runs=3, cost=0.1234)
        self.assertEqual(self._text(bar), "3 runs  ·  $0.1234")

    def test_active_jobs_are_listed_and_terminal_ones_are_not(self):
        running = Job(label="o/m·t")
        running.transition(JobStatus.RUNNING)
        done = Job(label="old")
        done.transition(JobStatus.RUNNING)
        done.transition(JobStatus.SUCCEEDED)
        bar = self._bar(jobs=[running, done])
        self.assertEqual(self._text(bar), "0 runs  ·  $0.0000  ·  jobs: o/m·t (running)")

    def test_active_job_list_caps_at_three(self):
        jobs = []
        for i in range(5):
            j = Job(label=f"j{i}")
            j.transition(JobStatus.RUNNING)
            jobs.append(j)
        text = self._text(self._bar(jobs=jobs))
        self.assertIn("jobs: j0 (running), j1 (running), j2 (running) +2 more", text)


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

    async def test_leaderboard_screen(self):
        from textual.widgets import DataTable

        from orchestral.tui.app import OrchestralApp
        from orchestral.tui.screens import LeaderboardScreen

        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp)
            reports = Path(tmp) / "reports"
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"), reports_dir=reports)
            async with app.run_test() as pilot:
                await pilot.press("3")
                await pilot.pause()
                self.assertIsInstance(app.screen, LeaderboardScreen)
                table = app.screen.query_one("#lb-table", DataTable)
                ok = await self._pump(app, pilot, lambda: table.row_count == 1)
                self.assertTrue(ok, "leaderboard never populated")
                await pilot.press("s")  # cycle sort
                await pilot.pause()
                await pilot.press("e")  # export CSV
                await pilot.pause()
                self.assertTrue((reports / "leaderboard.csv").exists())
                await pilot.press("2")  # back to history
                await pilot.pause()
                self.assertNotIsInstance(app.screen, LeaderboardScreen)

    async def test_auto_live_when_run_in_flight(self):
        from orchestral.tui.app import OrchestralApp
        from orchestral.tui.screens import LiveRunScreen

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _run_id, run_dir = store.new_run("o/m", "t-task", "w/m")
            (run_dir / "events.jsonl").write_text(
                '{"type": "run.started", "sequence": 1, "timestamp": "2026-09-15T01:00:00+00:00"}\n'
                '{"type": "worker.started", "sequence": 2, "worker_id": "worker-0", "output": {"description": "work"}}\n',
                encoding="utf-8",
            )
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                # a run still "running" in the index opens the live view on launch
                ok = await self._pump(app, pilot, lambda: isinstance(app.screen, LiveRunScreen))
                self.assertTrue(ok, "live view never opened")
                from textual.widgets import DataTable
                table = app.screen.query_one("#live-events", DataTable)
                ok = await self._pump(app, pilot, lambda: table.row_count >= 2)
                self.assertTrue(ok, "live trace never populated")
                # appending to events.jsonl is picked up by the poll
                with (run_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write('{"type": "worker.completed", "sequence": 3, "worker_id": "worker-0"}\n')
                ok = await self._pump(app, pilot, lambda: table.row_count >= 3)
                self.assertTrue(ok, "tail never picked up appended event")
                # cancel: run not launched from TUI → warning, stays running
                await pilot.press("c")
                await pilot.pause()
                self.assertTrue(all(not j.active for j in app.jobs))
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, LiveRunScreen)

    async def test_live_view_on_finished_run(self):
        from orchestral.tui.app import OrchestralApp
        from orchestral.tui.screens import LiveRunScreen, RunDetailScreen

        with tempfile.TemporaryDirectory() as tmp:
            run_id = _seed_run(tmp)
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                from textual.widgets import DataTable
                table = app.query_one("#runs-table", DataTable)
                self.assertTrue(await self._pump(app, pilot, lambda: table.row_count == 1))
                # finished run → Enter opens detail, not live
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, RunDetailScreen)
                await pilot.press("escape")
                await pilot.pause()
                # direct push of the live screen on a finished run renders
                # the archived events then stops polling
                app.push_screen(LiveRunScreen(app.store, run_id))
                await pilot.pause()
                live = app.screen
                self.assertIsInstance(live, LiveRunScreen)
                events_table = live.query_one("#live-events", DataTable)
                ok = await self._pump(app, pilot, lambda: live._done and events_table.row_count > 0)
                self.assertTrue(ok, "live view never rendered archived events")

    async def test_export_runs_csv(self):
        from textual.widgets import DataTable

        from orchestral.tui.app import OrchestralApp

        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp)
            reports = Path(tmp) / "reports"
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"), reports_dir=reports)
            async with app.run_test() as pilot:
                table = app.query_one("#runs-table", DataTable)
                self.assertTrue(await self._pump(app, pilot, lambda: table.row_count == 1))
                await pilot.press("e")
                await pilot.pause()
                csv = (reports / "runs.csv").read_text(encoding="utf-8")
                self.assertIn("run_id", csv.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
