"""OrchestralApp — the Textual application shell.

Views render state and dispatch intents; all DB access and run execution
happen on thread workers, so input and rendering never block.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Input

from orchestral.agentexec import ExecutorPreflightError, launch_gate
from orchestral.config import (
    ModelConfig,
    TaskSpec,
    find_task,
    load_models,
    load_task,
    load_yaml,
    resolve_judge,
    resolve_model,
)
from orchestral.export import runs_csv
from orchestral.judge import judge_choices
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.tui.screens import (
    GroupsScreen,
    HelpScreen,
    LaunchScreen,
    LeaderboardScreen,
    LiveRunScreen,
    RunDetailScreen,
)
from orchestral.tui.state import Job, JobStatus, filter_runs, fmt_cost, fmt_tokens, pass_label, status_label
from orchestral.tui.widgets import StatusBar


def _task_ids(tasks_dir: Path) -> list[str]:
    ids: list[str] = []
    for f in sorted(tasks_dir.rglob("*.yaml")):
        try:
            data = load_yaml(f)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("id"):
            ids.append(data["id"])
    return ids


def _model_slugs(models_dir: Path, role: str) -> list[str]:
    try:
        return [m.slug for m in load_models(models_dir) if m.role == role]
    except Exception:
        return []


class OrchestralApp(App):
    """Browse, inspect, compare, and launch orchestral runs."""

    TITLE = "orchestral"
    SUB_TITLE = "run explorer"

    CSS = """
    #runs-table { height: 1fr; }
    #filter-box { dock: top; }
    .ok { color: $success; }
    .err { color: $error; }
    .warn { color: $warning; }
    .muted { color: $text-muted; }
    #help-box, #launch-box {
        width: 60%;
        max-width: 90;
        height: auto;
        max-height: 90%;
        margin: 2 4;
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    #launch-box Label { margin-top: 1; }
    #detail-summary { padding: 0 1; height: auto; max-height: 8; }
    #groups-table { height: 1fr; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "filter", "Filter"),
        Binding("1", "show_live", "Live"),
        Binding("2", "show_history", "History"),
        Binding("3", "show_leaderboard", "Board"),
        Binding("g", "groups", "Groups"),
        Binding("n", "new_run", "New run"),
        Binding("x", "cancel_job", "Cancel job"),
        Binding("e", "export", "Export"),
        Binding("?", "help", "Help"),
    ]

    def __init__(
        self,
        runs_dir: Path,
        tasks_dir: Path,
        models_dir: Path,
        reports_dir: Path | None = None,
        allow_agent_exec: bool = False,
    ) -> None:
        super().__init__()
        self.runs_dir = runs_dir
        self.tasks_dir = tasks_dir
        self.models_dir = models_dir
        self.reports_dir = reports_dir or Path("reports")
        self.allow_agent_exec = allow_agent_exec
        self.store = RunStore(runs_dir)
        self.jobs: list[Job] = []
        self._runs: list[Any] = []
        self._query = ""
        self._auto_live_done = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter runs — task, model, group, status, failure (esc to close)", id="filter-box")
        yield DataTable(id="runs-table", cursor_type="row", zebra_stripes=True)
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#filter-box", Input).display = False
        self.query_one("#runs-table", DataTable).focus()
        self.action_refresh()
        self.set_interval(5.0, self._tick)

    # -- data loading (thread worker → call_from_thread to touch UI) --

    def _fetch(self) -> tuple[list[Any], dict[str, Any]]:
        return self.store.list_runs(limit=None), self.store.summary()

    def _reload(self) -> None:
        def work() -> None:
            runs, summary = self._fetch()
            self.call_from_thread(self._populate, runs, summary)
        self.run_worker(work, thread=True, name="fetch")

    def _populate(self, runs: list[Any], summary: dict[str, Any]) -> None:
        self._runs = runs
        table = self.query_one("#runs-table", DataTable)
        selected_id = None
        if table.row_count and table.cursor_row is not None:
            try:
                selected_id = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            except Exception:
                selected_id = None
        table.clear(columns=True)
        table.add_columns("run", "group", "rep", "task", "orchestrator", "worker", "cost", "tokens", "score", "pass", "status", "started")
        for r in filter_runs(runs, self._query):
            pl, _pc = pass_label(r.passes)
            sl, _sc = status_label(r.status)
            table.add_row(
                r.run_id,
                r.run_group or "-",
                str(r.replicate) if r.replicate is not None else "-",
                r.task_id,
                r.orchestrator,
                r.worker,
                fmt_cost(r.total_cost_usd),
                fmt_tokens(r.total_input_tokens + r.total_output_tokens),
                f"{r.score:.2f}" if r.score is not None else "-",
                pl,
                sl,
                (r.started_at or "")[:19],
                key=r.run_id,
            )
        if selected_id:
            with contextlib.suppress(Exception):
                for i, r in enumerate(filter_runs(runs, self._query)):
                    if r.run_id == selected_id:
                        table.move_cursor(row=i)
                        break
        bar = self.query_one(StatusBar)
        bar.set_counts(summary.get("runs", 0), summary.get("total_cost_usd", 0.0))
        bar.set_jobs(self.jobs)
        # observatory default: an in-flight run opens on the live view, once
        if not self._auto_live_done:
            self._auto_live_done = True
            running = [r for r in runs if r.status == "running"]
            if running and len(self.screen_stack) == 1:
                self.push_screen(LiveRunScreen(self.store, running[-1].run_id, self.reports_dir))

    def _tick(self) -> None:
        """Auto-refresh while jobs are active or any run is still running."""
        if any(j.active for j in self.jobs) or any(r.status == "running" for r in self._runs):
            self._reload()
            self.query_one(StatusBar).set_jobs(self.jobs)

    # -- actions --

    def action_refresh(self) -> None:
        self._reload()

    def action_filter(self) -> None:
        box = self.query_one("#filter-box", Input)
        box.display = True
        box.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-box":
            self._query = event.value
            self._populate(self._runs, self.store.summary())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter-box":
            self._close_filter()

    def _close_filter(self) -> None:
        box = self.query_one("#filter-box", Input)
        box.display = False
        self.query_one("#runs-table", DataTable).focus()

    def key_escape(self) -> None:
        box = self.query_one("#filter-box", Input)
        if box.display and box.has_focus:
            box.value = ""
            self._close_filter()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "runs-table" or not event.row_key.value:
            return
        run_id = str(event.row_key.value)
        run = next((r for r in self._runs if r.run_id == run_id), None)
        if run is not None and run.status == "running":
            self.push_screen(LiveRunScreen(self.store, run_id, self.reports_dir))
        else:
            self.push_screen(RunDetailScreen(self.store, run_id, self.reports_dir))

    def action_show_live(self) -> None:
        if isinstance(self.screen, LiveRunScreen):
            return
        running = [r for r in self._runs if r.status == "running"]
        if not running:
            self.notify("no run in flight — press n to launch one", severity="warning")
            return
        self.push_screen(LiveRunScreen(self.store, running[-1].run_id, self.reports_dir))

    def action_show_history(self) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()

    def action_show_leaderboard(self) -> None:
        if isinstance(self.screen, LeaderboardScreen):
            return
        self.push_screen(LeaderboardScreen(self.store, self.reports_dir))

    def action_export(self) -> None:
        """On the history view: write the currently filtered runs to CSV."""
        try:
            out = self.reports_dir / "runs.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(runs_csv(filter_runs(self._runs, self._query)), encoding="utf-8")
            self.notify(f"exported {out}")
        except Exception as exc:
            self.notify(f"export failed: {exc}", severity="error")

    def action_groups(self) -> None:
        self.push_screen(GroupsScreen(self.store))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_cancel_job(self) -> None:
        active = [j for j in self.jobs if j.active]
        if not active:
            self.notify("no active job", severity="warning")
            return
        active[-1].cancel()
        self.query_one(StatusBar).set_jobs(self.jobs)

    def action_new_run(self) -> None:
        tasks = _task_ids(self.tasks_dir)
        orchestrators = _model_slugs(self.models_dir, "orchestrator")
        workers = _model_slugs(self.models_dir, "worker")
        judges = judge_choices([m.slug for m in self._all_models()])
        if not tasks or not orchestrators or not workers:
            self.notify("need at least one task, orchestrator, and worker configured", severity="error")
            return
        self.push_screen(
            LaunchScreen(tasks, orchestrators, workers, judges),
            self._start_job,
        )

    def _all_models(self) -> list[ModelConfig]:
        try:
            return load_models(self.models_dir)
        except Exception:
            return []

    def _start_job(self, spec: dict[str, Any] | None) -> None:
        if not spec:
            return
        # Launch-gate parity with the web registry: a pairing/config error
        # is a notification, not a failed run. A missing task spec is left
        # for _execute's own error path (job failure with detail).
        try:
            task_path = find_task(spec["task"], str(self.tasks_dir))
            if task_path is not None:
                worker_cfg = resolve_model(spec["worker"], self.models_dir, role="worker")
                launch_gate(
                    worker_cfg, load_task(task_path),
                    allow_agent_exec=self.allow_agent_exec,
                    probe=not spec.get("dry_run"),
                )
        except ExecutorPreflightError as exc:
            self.notify(str(exc), severity="error")
            return
        label = f"{spec['task']}·{spec['worker'].split('/')[-1]}"
        if spec["replicates"] > 1:
            label += f"×{spec['replicates']}"
        job = Job(label=label)
        self.jobs.append(job)
        self.query_one(StatusBar).set_jobs(self.jobs)
        self.run_worker(lambda: self._execute(job, spec), thread=True, name="_job")

    def _execute(self, job: Job, spec: dict[str, Any]) -> None:
        """Runs on a worker thread — no widget access except call_from_thread."""
        try:
            task_path = find_task(spec["task"], str(self.tasks_dir))
            if task_path is None:
                raise FileNotFoundError(f"task '{spec['task']}' not found in {self.tasks_dir}")
            task: TaskSpec = load_task(task_path)
            orchestrator = resolve_model(spec["orchestrator"], self.models_dir, role="orchestrator")
            worker = resolve_model(spec["worker"], self.models_dir, role="worker")
            judge = resolve_judge(spec["judge"], self.models_dir) if spec.get("judge") else None
        except Exception as exc:
            self._job_done(job, JobStatus.FAILED, f"setup failed: {exc}")
            return

        job.transition(JobStatus.RUNNING)
        group = f"tui-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}" if spec["replicates"] > 1 else None
        try:
            for i in range(1, spec["replicates"] + 1):
                if job.cancel_event.is_set():
                    self._job_done(job, JobStatus.CANCELLED, "cancelled before all replicates")
                    return
                meta = Runner(
                    dry_run=spec["dry_run"],
                    runs_dir=str(self.runs_dir),
                    store=self.store,
                    run_group=group,
                    replicate=i if spec["replicates"] > 1 else None,
                    seed=(spec["seed"] + i - 1) if spec.get("seed") is not None else None,
                    cancel_event=job.cancel_event,
                    on_run_created=job.run_ids.append,
                    allow_agent_exec=self.allow_agent_exec,
                ).run(task, orchestrator, worker, judge)
                job.run_ids.append(meta.run_id)
                job.run_ids = list(dict.fromkeys(job.run_ids))
                if meta.status == "cancelled":
                    self._job_done(job, JobStatus.CANCELLED, f"run {meta.run_id} cancelled")
                    return
            self._job_done(job, JobStatus.SUCCEEDED, f"{len(job.run_ids)} run(s)")
        except Exception as exc:
            self._job_done(job, JobStatus.FAILED, str(exc)[:120])

    def _job_done(self, job: Job, status: JobStatus, detail: str) -> None:
        with contextlib.suppress(ValueError):
            job.transition(status, detail)
        self.call_from_thread(self._after_job)

    def _after_job(self) -> None:
        self.query_one(StatusBar).set_jobs(self.jobs)
        self._reload()
