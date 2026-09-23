"""Screens for the orchestral TUI. Views render state and dispatch intents
only — all data access goes through loader functions and RunStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from orchestral.calibrate import calibration_status
from orchestral.export import leaderboard_csv, run_audit_markdown
from orchestral.judge import DEFAULT_JUDGE
from orchestral.stats import aggregate, pairing_leaderboard
from orchestral.storage import RunStore
from orchestral.tui.state import (
    LB_SORTS,
    TERMINAL_PHASES,
    event_detail,
    event_row,
    fmt_cost,
    fmt_elapsed,
    fmt_ms,
    fmt_tokens,
    live_totals,
    run_phase,
    sort_leaderboard,
    tail_events,
    worker_states,
)


def _load_detail(store: RunStore, run_id: str) -> dict[str, Any]:
    """Everything the detail screen needs, loaded off the UI thread.
    Malformed files degrade to a placeholder string, never a crash."""
    meta = store.get_run(run_id)
    data: dict[str, Any] = {"meta": meta, "calls": [], "events": [], "files": {}}
    try:
        data["calls"] = store.calls_for_run(run_id)
    except Exception as exc:
        data["calls_error"] = str(exc)
    if meta is None:
        return data
    run_dir = Path(meta.run_dir)
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                data["events"].append(json.loads(line))
            except json.JSONDecodeError:
                data["events"].append({"type": "malformed", "raw": line[:200]})
    for name in ("metrics", "report", "plan", "cost", "manifest"):
        p = run_dir / f"{name}.json"
        if p.exists():
            try:
                data["files"][name] = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                data["files"][name] = f"<unparseable {name}.json>"
    return data


class RunDetailScreen(Screen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back"),
        Binding("e", "export_run", "Export"),
    ]

    def __init__(self, store: RunStore, run_id: str, reports_dir: Path | None = None) -> None:
        super().__init__()
        self._store = store
        self._run_id = run_id
        self._reports_dir = reports_dir or Path("reports")

    def compose(self) -> ComposeResult:
        yield Static(f"run {self._run_id} — loading…", id="detail-summary")
        with TabbedContent():
            with TabPane("Events"):
                yield RichLog(id="detail-events", highlight=False, markup=False)
            with TabPane("Calls"):
                yield DataTable(id="detail-calls")
            with TabPane("Metrics"):
                yield Static("loading…", id="detail-metrics")
            with TabPane("Report"):
                yield Static("loading…", id="detail-report")
            with TabPane("Plan"):
                yield Static("loading…", id="detail-plan")
            with TabPane("Manifest"):
                yield Static("loading…", id="detail-manifest")
        yield Footer()

    def on_mount(self) -> None:
        def work() -> None:
            data = _load_detail(self._store, self._run_id)
            self.app.call_from_thread(self._populate, data)
        self.app.run_worker(work, thread=True, name="detail")

    def _populate(self, data: dict[str, Any]) -> None:
        meta = data["meta"]
        if meta is None:
            self.query_one("#detail-summary", Static).update(f"run {self._run_id} not found in index")
            return
        lines = [
            f"[b]{meta.run_id}[/b]  {meta.status}  ·  {meta.task_id}",
            f"{meta.orchestrator} → {meta.worker}",
            f"cost {fmt_cost(meta.total_cost_usd)} · tokens {fmt_tokens(meta.total_input_tokens + meta.total_output_tokens)} · latency {fmt_ms(meta.latency_ms)}",
            f"pass {meta.passes} · score {meta.score} · failure {meta.failure_reason or '-'}",
            f"group {meta.run_group or '-'} · rep {meta.replicate or '-'} · {meta.started_at}",
            f"[dim]{meta.run_dir}[/dim]",
        ]
        self.query_one("#detail-summary", Static).update("\n".join(lines))

        log = self.query_one("#detail-events", RichLog)
        for ev in data["events"]:
            ts = ev.get("timestamp", "")[11:19]
            phase = ev.get("phase", "-")
            etype = ev.get("type", "?")
            detail = ev.get("reasoning") or ev.get("error") or ""
            log.write(f"{ts} [{phase}] {etype} {detail}")

        table = self.query_one("#detail-calls", DataTable)
        table.add_columns("phase", "role", "model", "in", "out", "cost", "api cost", "pricing", "ms", "attempt", "err")
        for c in data["calls"]:
            table.add_row(
                c.get("phase") or "-", c.get("role") or "-", c.get("model") or "-",
                str(c.get("input_tokens") or 0), str(c.get("output_tokens") or 0),
                f"${(c.get('cost_usd') or 0):.5f}",
                f"${c['api_cost_usd']:.5f}" if c.get("api_cost_usd") is not None else "-",
                c.get("pricing_source") or "-",
                fmt_ms(c.get("latency_ms")),
                str(c.get("attempt") or "-"),
                (c.get("error_category") or "")[:12],
            )

        for name, wid in (("metrics", "#detail-metrics"), ("report", "#detail-report"), ("plan", "#detail-plan"), ("manifest", "#detail-manifest")):
            content = data["files"].get(name, f"<no {name}.json>")
            text = content if isinstance(content, str) else json.dumps(content, indent=2, default=str)
            self.query_one(wid, Static).update(text)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_export_run(self) -> None:
        meta = self._store.get_run(self._run_id)
        if meta is None:
            self.app.notify("run not found", severity="error")
            return
        try:
            out = self._reports_dir / f"audit-{self._run_id}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(run_audit_markdown(meta.run_dir), encoding="utf-8")
            self.app.notify(f"exported {out}")
        except Exception as exc:
            self.app.notify(f"export failed: {exc}", severity="error")


class GroupsScreen(Screen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [Binding("escape", "back", "Back")]

    def __init__(self, store: RunStore) -> None:
        super().__init__()
        self._store = store

    def compose(self) -> ComposeResult:
        yield Static("Replicate groups — variance per (group, task, orchestrator, worker) cell")
        yield DataTable(id="groups-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#groups-table", DataTable)
        table.add_columns("group", "task", "orchestrator", "worker", "n", "pass%", "score±sd", "cost±sd", "p50", "p95", "succ/$", "failures")
        for c in aggregate(self._store.list_runs(limit=None)):
            score = f"{c.score_mean:.2f}±{c.score_sd:.2f}" if c.score_mean is not None else "-"
            cost = f"{c.cost_mean:.4f}±{c.cost_sd:.4f}"
            spd = f"{c.successes_per_dollar:.0f}" if c.successes_per_dollar is not None else "-"
            fails = ",".join(f"{k.split(':')[-1]}×{v}" for k, v in sorted(c.failures.items()))[:24]
            table.add_row(
                c.run_group or "-", c.task_id, c.orchestrator, c.worker,
                str(c.runs), f"{(c.pass_rate or 0) * 100:.0f}%", score, cost,
                fmt_ms(c.latency_p50), fmt_ms(c.latency_p95), spd, fails or "-",
            )

    def action_back(self) -> None:
        self.app.pop_screen()


class LiveRunScreen(Screen):
    """Follow one run by tailing its events.jsonl — the observatory view.

    All reads happen on a poll worker; the UI only renders what the harness
    has already persisted. Cancel only works for runs this TUI launched —
    the job's cancel_event is the only safe handle.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back"),
        Binding("c", "cancel_run", "Cancel"),
        Binding("e", "export_run", "Export"),
    ]

    CSS = """
    #live-info { height: auto; max-height: 8; padding: 0 1; border-bottom: solid $primary; }
    #live-main { height: 1fr; }
    #live-left { width: 40%; min-width: 30; border-right: solid $primary; }
    #live-workers { height: auto; padding: 0 1; border-bottom: dashed $primary; }
    #live-detail { height: 1fr; padding: 0 1; }
    #live-events { height: 1fr; }
    """

    def __init__(self, store: RunStore, run_id: str, reports_dir: Path | None = None) -> None:
        super().__init__()
        self._store = store
        self._run_id = run_id
        self._reports_dir = reports_dir or Path("reports")
        self._offset = 0
        self._events: list[dict[str, Any]] = []
        self._done = False
        self._finished_at: str | None = None
        self._meta: Any = None

    def compose(self) -> ComposeResult:
        yield Static(f"run {self._run_id} — loading…", id="live-info")
        with Horizontal(id="live-main"):
            with Vertical(id="live-left"):
                yield Static("workers: —", id="live-workers")
                yield Static("select an event to inspect", id="live-detail")
            yield DataTable(id="live-events", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#live-events", DataTable).add_columns("time", "event", "worker", "detail")
        self.query_one("#live-events", DataTable).focus()
        self._poll()
        self._timer = self.set_interval(0.75, self._poll)

    def _poll(self) -> None:
        if self._done:
            return
        self.app.run_worker(self._poll_work, thread=True, name="live-poll")

    def _poll_work(self) -> None:
        """File reads off the UI thread; returns data for _render."""
        meta = self._store.get_run(self._run_id)
        if meta is None:
            self.app.call_from_thread(self._missing)
            return
        run_dir = Path(meta.run_dir)
        new_events, new_offset = tail_events(run_dir / "events.jsonl", self._offset)
        manifest = None
        mp = run_dir / "manifest.json"
        if mp.exists():
            try:
                manifest = json.loads(mp.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                manifest = None
        run_json = None
        rp = run_dir / "run.json"
        if rp.exists():
            try:
                run_json = json.loads(rp.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                run_json = None
        self.app.call_from_thread(self._apply_poll, meta, new_events, new_offset, manifest, run_json)

    def _missing(self) -> None:
        self.query_one("#live-info", Static).update(f"run {self._run_id} not found in index")
        self._done = True
        self._timer.stop()

    def _apply_poll(
        self,
        meta: Any,
        new_events: list[dict[str, Any]],
        new_offset: int,
        manifest: dict[str, Any] | None,
        run_json: dict[str, Any] | None,
    ) -> None:
        self._meta = meta
        self._offset = new_offset
        self._events.extend(new_events)
        table = self.query_one("#live-events", DataTable)
        for ev in new_events:
            ts, etype, wid, detail = event_row(ev)
            table.add_row(ts, etype, wid, detail, key=str(ev.get("sequence", len(self._events))))
        if new_events:
            table.move_cursor(row=table.row_count - 1)

        phase = run_phase(self._events)
        cost, toks = live_totals(self._events)
        started = (manifest or {}).get("started_at") or meta.started_at
        self._finished_at = (run_json or {}).get("finished_at") or (manifest or {}).get("finished_at")
        elapsed = fmt_elapsed(started, self._finished_at)
        info = [
            f"[b]{self._run_id}[/b]  phase {phase}  ·  {meta.task_id}",
            f"{meta.orchestrator} → {meta.worker}",
            f"cost {fmt_cost(cost)} · tokens {fmt_tokens(toks)} · elapsed {elapsed}",
        ]
        if manifest:
            info.append(f"task hash {manifest.get('task_hash', '-')[:16]} · config {manifest.get('config_hash', '-')[:16]}")
        self.query_one("#live-info", Static).update("\n".join(info))

        states = worker_states(self._events)
        chips = "  ".join(f"{wid} {st}" for wid, st in sorted(states.items())) or "—"
        if phase in ("planning",):
            chips = f"orchestrator running · {chips}"
        elif phase in ("assembling",):
            chips += " · synthesizer"
        elif phase in ("evaluating",):
            chips += " · evaluator"
        self.query_one("#live-workers", Static).update(f"workers: {chips}")

        if phase in TERMINAL_PHASES or meta.status != "running":
            self._done = True
            self._timer.stop()
            self.query_one("#live-info", Static).update(
                "\n".join(info) + f"\n[dim]run {meta.status} — Esc back, e export[/dim]"
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "live-events" or event.cursor_row is None:
            return
        if event.cursor_row < len(self._events):
            self.query_one("#live-detail", Static).update(event_detail(self._events[event.cursor_row]))

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cancel_run(self) -> None:
        jobs: list[Any] = getattr(self.app, "jobs", [])
        job = next(
            (j for j in jobs if j.active and self._run_id in j.run_ids),
            None,
        )
        if job is None:
            self.app.notify("only runs launched from this TUI can be cancelled", severity="warning")
            return
        if job.cancel():
            self.app.notify(f"cancelling {self._run_id} — stops between subtasks")
        else:
            self.app.notify("run already finished", severity="warning")

    def action_export_run(self) -> None:
        if self._meta is None:
            self.app.notify("run not loaded yet", severity="warning")
            return
        try:
            out = self._reports_dir / f"audit-{self._run_id}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(run_audit_markdown(self._meta.run_dir), encoding="utf-8")
            self.app.notify(f"exported {out}")
        except Exception as exc:
            self.app.notify(f"export failed: {exc}", severity="error")


class LeaderboardScreen(Screen):
    """Pairing rollup — cost-per-pass primary rank, sample-size disclosure."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "back", "Back"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("e", "export_csv", "Export"),
    ]

    def __init__(self, store: RunStore, reports_dir: Path | None = None, min_samples: int = 10) -> None:
        super().__init__()
        self._store = store
        self._reports_dir = reports_dir or Path("reports")
        self._min_samples = min_samples
        self._sort_i = 0
        self._rows: list[Any] = []
        self._judge_bits: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("Leaderboard — loading…", id="lb-header")
        yield DataTable(id="lb-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#lb-table", DataTable)
        table.add_columns(
            "orchestrator", "worker", "n", "tasks", "pass%", "judge",
            "med cost", "med dur", "fail%", "$/pass", "conf",
        )
        self.app.run_worker(self._load, thread=True, name="leaderboard")

    def _load(self) -> None:
        metas = self._store.list_runs(limit=None)
        rows = pairing_leaderboard(metas, min_samples=self._min_samples)
        judge_bits = []
        for slug in self._store.judge_slugs({m.task_id for m in metas}):
            st = calibration_status(self._reports_dir, slug)
            if st["calibrated"]:
                judge_bits.append(f"{slug} κ={st['kappa']:.2f}")
            else:
                judge_bits.append(f"{slug} uncalibrated")
        self.app.call_from_thread(self._populate, rows, judge_bits)

    def _populate(self, rows: list[Any], judge_bits: list[str] | None = None) -> None:
        self._rows = rows
        self._judge_bits = judge_bits or []
        key = LB_SORTS[self._sort_i]
        header = self.query_one("#lb-header", Static)
        low = sum(1 for r in rows if r.low_sample)
        judge_note = f" · judge {'; '.join(judge_bits)}" if judge_bits else ""
        header.update(
            f"Pairing leaderboard — sort {key} (s cycles) · "
            f"{len(rows)} pairings · {low} below {self._min_samples} samples "
            "[dim](low-sample ranks are anecdote, not evidence)[/dim]"
            f"{judge_note}"
        )
        table = self.query_one("#lb-table", DataTable)
        table.clear()
        for r in sort_leaderboard(rows, key):
            table.add_row(
                r.orchestrator, r.worker, str(r.runs), str(r.tasks_covered),
                f"{(r.pass_rate or 0) * 100:.0f}%",
                f"{r.judge_score_median:.2f}" if r.judge_score_median is not None else "-",
                fmt_cost(r.cost_median),
                fmt_ms(r.duration_median_ms),
                f"{(r.failure_rate or 0) * 100:.0f}%",
                fmt_cost(r.cost_per_pass),
                "low-n" if r.low_sample else "ok",
            )

    def action_cycle_sort(self) -> None:
        self._sort_i = (self._sort_i + 1) % len(LB_SORTS)
        if self._rows:
            self._populate(self._rows, self._judge_bits)

    def action_export_csv(self) -> None:
        try:
            out = self._reports_dir / "leaderboard.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(leaderboard_csv(sort_leaderboard(self._rows, LB_SORTS[self._sort_i])), encoding="utf-8")
            self.app.notify(f"exported {out}")
        except Exception as exc:
            self.app.notify(f"export failed: {exc}", severity="error")

    def action_back(self) -> None:
        self.app.pop_screen()


HELP_TEXT = """\
[b]orchestral tui[/b] — experiment observatory

  1              live run (tails events.jsonl for the newest running run)
  2              run history (this table)
  3              pairing leaderboard (s cycles sort, e exports CSV)
  j / ↓, k / ↑   move selection
  Enter          open run — live view while running, detail once finished
  /              filter runs (task, model, group, status, failure)
  g              replicate-group variance table
  n              launch a run (or replicate batch)
  x              cancel the active job
  c              cancel the run being watched (live view)
  e              export — audit markdown (detail/live), CSV (history/board)
  r              refresh run list
  ?              this help
  Esc            back / close
  q              quit

Detail tabs: events stream, per-call index, metrics, report, plan, manifest.
Jobs run on background threads — the UI stays responsive; cancelling
stops between subtasks and records status=cancelled.
"""


class HelpScreen(ModalScreen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Vertical(Static(HELP_TEXT, id="help-text"), id="help-box")


class LaunchScreen(ModalScreen):
    """Collect a run spec; dismisses with a dict or None on cancel."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        task_ids: list[str],
        orchestrators: list[str],
        workers: list[str],
        judges: list[str],
    ) -> None:
        super().__init__()
        self._task_ids = task_ids
        self._orchestrators = orchestrators
        self._workers = workers
        self._judges = judges

    def compose(self) -> ComposeResult:
        with Vertical(id="launch-box"):
            yield Label("Launch run")
            yield Label("Task")
            yield Select([(t, t) for t in self._task_ids], id="launch-task", allow_blank=not self._task_ids)
            yield Label("Orchestrator")
            yield Select([(m, m) for m in self._orchestrators], id="launch-orch", allow_blank=not self._orchestrators)
            yield Label("Worker")
            yield Select([(m, m) for m in self._workers], id="launch-worker", allow_blank=not self._workers)
            yield Label("Judge (optional — default is the decisions engine)")
            yield Select(
                [("(none)", ""), *[(m, m) for m in self._judges]],
                id="launch-judge",
                value=DEFAULT_JUDGE if DEFAULT_JUDGE in self._judges else "",
            )
            yield Label("Replicates")
            yield Input(value="1", id="launch-reps", type="integer")
            yield Label("Seed (optional)")
            yield Input(placeholder="e.g. 42", id="launch-seed", type="integer")
            yield Checkbox("Dry run (no API calls)", value=True, id="launch-dry")
            with Vertical():
                yield Button("Launch", id="launch-go", variant="primary")
                yield Button("Cancel", id="launch-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch-cancel":
            self.dismiss(None)
            return
        task = self.query_one("#launch-task", Select).value
        orch = self.query_one("#launch-orch", Select).value
        worker = self.query_one("#launch-worker", Select).value
        judge = self.query_one("#launch-judge", Select).value or None
        if task is Select.BLANK or orch is Select.BLANK or worker is Select.BLANK:
            self.app.notify("task, orchestrator and worker are required", severity="error")
            return
        reps_raw = self.query_one("#launch-reps", Input).value
        seed_raw = self.query_one("#launch-seed", Input).value
        try:
            reps = max(1, int(reps_raw)) if reps_raw else 1
        except ValueError:
            self.app.notify("replicates must be an integer", severity="error")
            return
        try:
            seed = int(seed_raw) if seed_raw else None
        except ValueError:
            self.app.notify("seed must be an integer", severity="error")
            return
        self.dismiss({
            "task": task,
            "orchestrator": orch,
            "worker": worker,
            "judge": judge,
            "replicates": reps,
            "seed": seed,
            "dry_run": self.query_one("#launch-dry", Checkbox).value,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)
