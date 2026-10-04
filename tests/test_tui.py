"""Tests for the TUI: pure domain state, runner cancellation, live-view
event derivation, leaderboard sorting, and textual pilot interaction tests."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar

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
        self.assertEqual(fmt_cost(0.01234), "$0.012")  # tiers: $0.0072 / $0.187 / $12.40
        self.assertEqual(fmt_cost(0.0072), "$0.0072")
        self.assertEqual(fmt_cost(None), "-")
        self.assertEqual(fmt_tokens(999), "999")
        self.assertEqual(fmt_tokens(1500), "1.5k")
        self.assertEqual(pass_label(True, ascii_only=False), ("■", "pass", "pass"))
        self.assertEqual(pass_label(False, ascii_only=False), ("□", "fail", "fail"))
        self.assertEqual(pass_label(None, ascii_only=False), ("", "-", "ink-3"))


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


class TestGateEnvironment(unittest.TestCase):
    """A missing [tui] extra is a broken gate environment, not a skipped test.

    `textual` is part of the environment every gate runs in (CI installs
    `.[dev,tui]`), so a run that cannot import it is not a green run with fewer
    tests — it is a run that measured less than it reported. Skipping quietly
    made `unittest discover` print OK while the pilot suite never executed, and
    the same blind spot hid the `StatusBar._message` defect from mypy (without
    `textual` the base class resolves to `Any`).
    """

    def test_textual_extra_is_importable(self):
        if not HAS_TEXTUAL:
            self.fail(
                "the [tui] extra is missing, so every Textual test in this file was "
                "skipped and the run under-reports. CI installs '.[dev,tui]'; match it "
                "with scripts/bootstrap-venv.sh <dir> (or pip install -e '.[dev,tui]'). "
                "Do not re-add a skipUnless guard: silence here is a false green."
            )


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed (pip install 'orchestral[tui]')")
class TestStatusBar(unittest.TestCase):
    """`_render_text` must never read an attribute the class does not assign.

    17f6c543 removed the `set_message` writer and its `self._message = ""`
    initialiser but left the reader behind, so every status-strip refresh
    raised `AttributeError` and reddened main for six days. These assert on
    rendered text so a reader-without-a-writer cannot reach main again. The
    `note` and `jobs` cases pin the segment order of `_render_text` as well.
    """

    def _bar(self, runs=0, cost=0.0, jobs=None, note=""):
        from orchestral.tui.widgets import StatusBar

        bar = StatusBar()
        bar.set_counts(runs, cost)
        bar.set_jobs(jobs or [])
        if note:
            bar.set_note(note)
        return bar

    def _text(self, bar):
        return str(bar.visual)

    def test_counts_render_without_a_note(self):
        self.assertEqual(self._text(self._bar(runs=3, cost=0.1234)), "3 runs, $0.123")

    def test_note_renders_after_counts(self):
        bar = self._bar(runs=1, cost=0.5, note="2 queued")
        self.assertEqual(self._text(bar), "1 runs, $0.500, 2 queued")

    def test_note_renders_after_jobs(self):
        # test_note_renders_after_counts builds a bar with no jobs, so it pins the
        # note against the counts and nothing else. Hoisting the note append above
        # the jobs block leaves that test green, so pin the two-segment order
        # explicitly: note last, jobs present and before it.
        running = Job(label="t on o/m")
        running.transition(JobStatus.RUNNING)
        text = self._text(self._bar(runs=1, cost=0.5, jobs=[running], note="2 queued"))
        self.assertTrue(text.endswith("2 queued"), text)
        self.assertLess(text.index("jobs:"), text.index("2 queued"))

    def test_active_jobs_are_listed_and_terminal_ones_are_not(self):
        running = Job(label="t on o/m")
        running.transition(JobStatus.RUNNING)
        done = Job(label="old")
        done.transition(JobStatus.RUNNING)
        done.transition(JobStatus.SUCCEEDED)
        bar = self._bar(jobs=[running, done])
        self.assertEqual(self._text(bar), "0 runs, $0.00, jobs: t on o/m (running)")

    def test_active_jobs_are_capped_at_three_with_a_count(self):
        jobs = []
        for i in range(5):
            job = Job(label=f"j{i}")
            job.transition(JobStatus.RUNNING)
            jobs.append(job)
        text = self._text(self._bar(jobs=jobs))
        self.assertIn("jobs: j0 (running), j1 (running), j2 (running) +2 more", text)
        self.assertNotIn("j3", text)


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

    async def test_paid_launch_needs_the_estimate_and_a_typed_run(self):
        from textual.widgets import Checkbox, Input, Static

        from orchestral.tui.app import OrchestralApp
        from orchestral.tui.screens import LaunchScreen

        with tempfile.TemporaryDirectory() as tmp:
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            started: list[dict] = []
            app._start_job = started.append  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.press("n")
                await pilot.pause()
                self.assertIsInstance(app.screen, LaunchScreen)
                screen = app.screen
                confirm = screen.query_one("#launch-confirm", Input)
                self.assertFalse(confirm.display)  # dry run: nothing to confirm
                screen.query_one("#launch-dry", Checkbox).value = False
                await pilot.pause()
                self.assertTrue(confirm.display)
                text = str(screen.query_one("#launch-estimate", Static).render())
                self.assertIn("Estimated cost", text)
                self.assertIn("run", confirm.placeholder)
                # Enter on the button without typing starts nothing
                screen.query_one("#launch-go").press()
                await pilot.pause()
                self.assertEqual(started, [])
                self.assertIsInstance(app.screen, LaunchScreen)
                confirm.value = "RUN please"
                screen.query_one("#launch-go").press()
                await pilot.pause()
                self.assertEqual(started, [])
                confirm.value = "run"
                screen.query_one("#launch-go").press()
                await pilot.pause()
                self.assertEqual(len(started), 1)
                self.assertFalse(started[0]["dry_run"])
                self.assertNotIn("confirm", started[0])

    async def test_dry_run_launch_needs_no_confirm(self):
        from orchestral.tui.app import OrchestralApp

        with tempfile.TemporaryDirectory() as tmp:
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            started: list[dict] = []
            app._start_job = started.append  # type: ignore[method-assign]
            async with app.run_test() as pilot:
                await pilot.press("n")
                await pilot.pause()
                app.screen.query_one("#launch-go").press()
                await pilot.pause()
                self.assertEqual(len(started), 1)
                self.assertTrue(started[0]["dry_run"])

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


# ---------------------------------------------------------------------------
# U17: Score theme, glyph parity, spend parity
# ---------------------------------------------------------------------------

import importlib.util  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
from unittest.mock import patch  # noqa: E402

from orchestral import design_tokens  # noqa: E402
from orchestral.format import fmt_money  # noqa: E402
from orchestral.glyphs import GLYPHS  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_TUI_SRC = sorted((_ROOT / "orchestral" / "tui").glob("*.py"))
_CORPUS_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", _ROOT / "scripts" / "build-fixture-corpus.py")
assert _CORPUS_SPEC and _CORPUS_SPEC.loader
_corpus = importlib.util.module_from_spec(_CORPUS_SPEC)
_CORPUS_SPEC.loader.exec_module(_corpus)
_FIXTURE_MODELS = _ROOT / "tests" / "fixtures" / "observatory" / "models"


class TestNoPaletteInTuiSource(unittest.TestCase):
    def test_no_hex_literals_in_tui_modules(self):
        for path in _TUI_SRC:
            hits = re.findall(r"#[0-9a-fA-F]{6}\b", path.read_text(encoding="utf-8"))
            self.assertEqual(hits, [], f"{path.name} hard-codes a colour; derive it from design_tokens")


class TestVerdictLabels(unittest.TestCase):
    def test_pass_label_is_glyph_word_role(self):
        from orchestral.tui.state import pass_label

        self.assertEqual(pass_label(True, ascii_only=False), (GLYPHS["pass"].unicode, "pass", "pass"))
        self.assertEqual(pass_label(False, ascii_only=False), (GLYPHS["fail"].unicode, "fail", "fail"))
        self.assertEqual(pass_label(True, ascii_only=True), ("[+]", "pass", "pass"))

    def test_status_label_uses_the_glyph_table(self):
        from orchestral.tui.state import status_label

        self.assertEqual(status_label("running", ascii_only=False), ("◔", "running", "live"))
        self.assertEqual(status_label("failed", ascii_only=False), ("□", "failed", "fail"))
        self.assertEqual(status_label("cancelled", ascii_only=False)[0], "┄")
        self.assertEqual(status_label("finished", ascii_only=False)[1], "finished")

    def test_ascii_rules_mirror_the_cli(self):
        from orchestral.tui.state import use_ascii

        class Enc:
            encoding = "utf-8"

        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), patch("sys.stdout", Enc()):
            self.assertFalse(use_ascii())
            with patch.dict(os.environ, {"ORCH_ASCII": "1"}):
                self.assertTrue(use_ascii())
            with patch.dict(os.environ, {"TERM": "dumb"}):
                self.assertTrue(use_ascii())
            with patch.dict(os.environ, {"NO_COLOR": "1"}):
                self.assertFalse(use_ascii())  # NO_COLOR drops colour only
        Enc.encoding = "latin-1"
        with patch.dict(os.environ, {"TERM": "xterm"}, clear=True), patch("sys.stdout", Enc()):
            self.assertTrue(use_ascii())

    def test_cell_text_colours_by_role_unless_no_color(self):
        from orchestral.tui.state import verdict_cell

        stage = design_tokens.theme("stage")
        with patch.dict(os.environ, {}, clear=True):
            cell = verdict_cell(("■", "pass", "pass"), stage)
            self.assertEqual(cell.plain, "■ pass")
            self.assertIn(stage["--pass-text"].lower(), str(cell.style).lower())
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            cell = verdict_cell(("■", "pass", "pass"), stage)
            self.assertEqual(cell.plain, "■ pass")
            self.assertEqual(str(cell.style), "")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(verdict_cell(("", "-", "ink-3"), stage).plain, "-")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed (pip install 'orchestral[tui]')")
class TestScoreTheme(unittest.IsolatedAsyncioTestCase):
    def test_both_themes_derive_every_role_from_tokens(self):
        from orchestral.tui.theme import THEME_NAMES, build_theme

        self.assertEqual(THEME_NAMES, ("orchestral-paper", "orchestral-stage"))
        for key in design_tokens.THEMES:
            t, tok = build_theme(f"orchestral-{key}"), design_tokens.theme(key)
            self.assertEqual(t.background.upper(), tok["--canvas"].upper())
            self.assertEqual(t.surface.upper(), tok["--surface"].upper())
            self.assertEqual(t.foreground.upper(), tok["--ink"].upper())
            self.assertEqual(t.success.upper(), tok["--pass-text"].upper())
            self.assertEqual(t.error.upper(), tok["--fail-text"].upper())
            self.assertEqual(t.warning.upper(), tok["--live-text"].upper())
            self.assertEqual(t.primary.upper(), tok["--judge-text"].upper())
            self.assertEqual(t.dark, key == "stage")

    async def test_default_is_stage_and_toggle_switches_background(self):
        from textual.color import Color

        from orchestral.tui.app import OrchestralApp

        stage, paper = design_tokens.theme("stage"), design_tokens.theme("paper")
        with tempfile.TemporaryDirectory() as tmp:
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                self.assertEqual(app.theme, "orchestral-stage")
                self.assertEqual(app.screen.styles.background.hex, Color.parse(stage["--canvas"]).hex)
                await pilot.press("t")
                await pilot.pause()
                self.assertEqual(app.theme, "orchestral-paper")
                self.assertEqual(app.screen.styles.background.hex, Color.parse(paper["--canvas"]).hex)
                await pilot.press("t")
                await pilot.pause()
                self.assertEqual(app.theme, "orchestral-stage")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed (pip install 'orchestral[tui]')")
class TestScoreTables(unittest.IsolatedAsyncioTestCase):
    async def _table(self, app, pilot):
        from textual.widgets import DataTable

        table = app.query_one("#runs-table", DataTable)
        for _ in range(250):
            await pilot.pause()
            await asyncio.sleep(0.02)
            if table.row_count:
                break
        return table

    def _verdict_cells(self, table):
        cols = [str(c.label) for c in table.columns.values()]
        row = table.get_row_at(0)
        return row[cols.index("Pass")], row[cols.index("Status")]

    def _seed(self, tmp, passes):
        store = RunStore(tmp)
        meta = Runner(dry_run=True, runs_dir=tmp, store=store).run(
            TaskSpec(id="t-task", type="html", prompt="p"),
            _model("o/model", "orchestrator"), _model("w/model", "worker"))
        updated = store.get_run(meta.run_id)
        assert updated is not None
        updated.passes = passes
        store.index_meta(updated)

    async def test_cells_show_glyph_and_word(self):
        from orchestral.tui.app import OrchestralApp

        for passes, expected in ((True, "■ pass"), (False, "□ fail"), (None, "-")):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TERM": "xterm", "LC_ALL": "C.UTF-8"}):
                os.environ.pop("ORCH_ASCII", None)
                os.environ.pop("NO_COLOR", None)
                self._seed(tmp, passes)
                app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
                async with app.run_test() as pilot:
                    table = await self._table(app, pilot)
                    pass_cell, status_cell = self._verdict_cells(table)
                    self.assertEqual(getattr(pass_cell, "plain", pass_cell), expected)
                    self.assertIn("finished", getattr(status_cell, "plain", status_cell))

    async def test_no_color_keeps_glyph_and_word_for_every_verdict(self):
        from orchestral.tui.app import OrchestralApp

        for passes, expected in ((True, "■ pass"), (False, "□ fail")):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}):
                os.environ.pop("ORCH_ASCII", None)
                self._seed(tmp, passes)
                app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
                async with app.run_test() as pilot:
                    table = await self._table(app, pilot)
                    pass_cell, _ = self._verdict_cells(table)
                    self.assertEqual(pass_cell.plain, expected)
                    self.assertEqual(str(pass_cell.style), "")

    async def test_ascii_fallback_in_the_table(self):
        from orchestral.tui.app import OrchestralApp

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ORCH_ASCII": "1"}):
            self._seed(tmp, True)
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                table = await self._table(app, pilot)
                self.assertEqual(self._verdict_cells(table)[0].plain, "[+] pass")

    async def test_colour_follows_the_theme_toggle(self):
        from orchestral.tui.app import OrchestralApp

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TERM": "xterm"}):
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("ORCH_ASCII", None)
            self._seed(tmp, True)
            app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
            async with app.run_test() as pilot:
                table = await self._table(app, pilot)
                stage = design_tokens.theme("stage")["--pass-text"].lower()
                paper = design_tokens.theme("paper")["--pass-text"].lower()
                self.assertIn(stage, str(self._verdict_cells(table)[0].style).lower())
                await pilot.press("t")
                await pilot.pause()
                await pilot.pause()
                table = app.query_one("#runs-table", type(table))
                self.assertIn(paper, str(self._verdict_cells(table)[0].style).lower())


class TestStatusBarCopy(unittest.TestCase):
    @unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
    def test_status_bar_text_has_no_middle_dot(self):
        from orchestral.tui.widgets import StatusBar

        bar = StatusBar()
        bar.set_counts(4, 1.5)
        bar.set_jobs([Job(label="a on b", status=JobStatus.RUNNING)])
        bar.set_note("2 queued")
        self.assertNotIn("·", str(bar.visual))


class TestSpendParity(unittest.TestCase):
    """The TUI confirm panel renders the very payload the web dialog renders,
    on the U23 corpus: same billed numbers, same range, same unknown handling."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = _corpus.build_corpus(cls.root, "full")
        cls.store = RunStore(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    KNOWN: ClassVar[dict[str, str]] = {"task": "corpus-landing-page", "orchestrator": "corpus/orch-a",
             "worker": "corpus/worker-cheap", "replicates": "3"}
    UNKNOWN: ClassVar[dict[str, str]] = {"task": "corpus-landing-page", "orchestrator": "corpus/orch-b",
               "worker": "corpus/worker-local", "replicates": "2"}

    def _est(self, spec):
        from orchestral.web import state as web_state

        return web_state.launch_estimate(self.store, _FIXTURE_MODELS, spec)

    def test_known_pairing_rows_match_the_web_dialog(self):
        from orchestral.tui.state import format_spend_estimate

        est = self._est(self.KNOWN)
        self.assertIsNotNone(est["total_usd"])
        lines = format_spend_estimate(est).split("\n")
        self.assertEqual(lines[0], "Runs: 3")
        self.assertEqual(lines[1], f"Estimated cost: about {fmt_money(est['total_usd'])}")
        self.assertEqual(lines[2], f"Range: {fmt_money(est['total_low_usd'])} to {fmt_money(est['total_high_usd'])}")
        self.assertEqual(lines[3], f"Estimate basis: {est['basis_label']}")
        self.assertEqual(
            lines[4],
            f"Spent this month: ${est['month_to_date_billed_usd']:.2f} of "
            f"${est['monthly_cap_usd']:.0f} eval cap (billed spend recorded in this index)")
        self.assertEqual(lines[5], est["caveat"])
        self.assertEqual(len(lines), 6)

    def test_unknown_is_never_zero(self):
        from orchestral.tui.state import format_spend_estimate

        est = self._est(self.UNKNOWN)
        self.assertIsNone(est["total_usd"])
        text = format_spend_estimate(est)
        self.assertIn("Estimated cost: Unknown", text)
        self.assertIn("Range: Unknown", text)
        self.assertNotIn("$0.00", text.split("Spent this month")[0])
        self.assertIn(est["basis_label"], text)

    def test_dry_run_is_zero_not_unknown(self):
        from orchestral.tui.state import format_spend_estimate

        est = self._est({**self.KNOWN, "dry_run": True})
        text = format_spend_estimate(est)
        self.assertIn("Estimated cost: $0.00", text)
        self.assertNotIn("about", text)

    def test_tui_estimate_hook_is_the_web_payload(self):
        from orchestral.tui.app import OrchestralApp

        app = OrchestralApp(self.root, Path("tasks"), _FIXTURE_MODELS)
        self.assertEqual(app._launch_estimate(dict(self.KNOWN)), self._est(dict(self.KNOWN)))

    def test_headline_spend_is_the_billed_total_the_web_sums(self):
        from orchestral.tui.app import OrchestralApp

        app = OrchestralApp(self.root, Path("tasks"), _FIXTURE_MODELS)
        runs = self.store.list_runs(limit=None)
        web_total = sum(r.display_cost_usd or 0.0 for r in runs)
        self.assertAlmostEqual(app._summary()["total_cost_usd"], web_total, places=6)
        failed = self.store.get_run(self.manifest["failed_run_id"])
        assert failed is not None
        from orchestral.tui.state import fmt_cost
        self.assertEqual(fmt_cost(failed.display_cost_usd), fmt_money(0.74))  # billed, not the recorded $0.11

    @unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
    def test_launch_screen_panel_shows_the_same_numbers(self):
        async def go():
            from textual.widgets import Checkbox, Select, Static

            from orchestral.tui.app import OrchestralApp
            from orchestral.tui.screens import LaunchScreen
            from orchestral.tui.state import format_spend_estimate

            app = OrchestralApp(self.root, _FIXTURE_MODELS.parent / "tasks", _FIXTURE_MODELS)
            async with app.run_test() as pilot:
                await pilot.press("n")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, LaunchScreen)
                screen.query_one("#launch-task", Select).value = self.KNOWN["task"]
                screen.query_one("#launch-orch", Select).value = self.KNOWN["orchestrator"]
                screen.query_one("#launch-worker", Select).value = self.KNOWN["worker"]
                screen.query_one("#launch-dry", Checkbox).value = False
                await pilot.pause()
                shown = str(screen.query_one("#launch-estimate", Static).render())
                spec = {**self.KNOWN, "replicates": "1", "judge": screen.query_one("#launch-judge", Select).value or None}
                self.assertEqual(shown, format_spend_estimate(self._est(spec)))

        asyncio.run(go())


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed (pip install 'orchestral[tui]')")
class TestScreenshots(unittest.IsolatedAsyncioTestCase):
    """Render each theme at 80 and 120 columns; SVG goes to ORCH_TUI_SHOTS when set."""

    async def test_renders_at_both_widths_and_themes(self):
        from orchestral.tui.app import OrchestralApp

        out = os.environ.get("ORCH_TUI_SHOTS")
        for cols in (80, 120):
            for theme in ("orchestral-stage", "orchestral-paper"):
                with tempfile.TemporaryDirectory() as tmp:
                    store = RunStore(tmp)
                    for passes in (True, False, None):
                        meta = Runner(dry_run=True, runs_dir=tmp, store=store).run(
                            TaskSpec(id="t-task", type="html", prompt="p"),
                            _model("o/model", "orchestrator"), _model("w/model", "worker"))
                        row = store.get_run(meta.run_id)
                        assert row is not None
                        row.passes = passes
                        store.index_meta(row)
                    app = OrchestralApp(Path(tmp), Path("tasks"), Path("models"))
                    async with app.run_test(size=(cols, 30)) as pilot:
                        app.theme = theme
                        for _ in range(100):
                            await pilot.pause()
                            await asyncio.sleep(0.02)
                            if app.query_one("#runs-table").row_count == 3:  # type: ignore[attr-defined]
                                break
                        await pilot.pause()
                        svg = app.export_screenshot()
                        self.assertIn("<svg", svg)
                        if out:
                            Path(out).mkdir(parents=True, exist_ok=True)
                            (Path(out) / f"tui-{theme.split('-')[1]}-{cols}.svg").write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
