"""Screens for the orchestral TUI. Views render state and dispatch intents
only — all data access goes through loader functions and RunStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
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

from orchestral.stats import aggregate
from orchestral.storage import RunStore
from orchestral.tui.state import fmt_cost, fmt_ms, fmt_tokens


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
    for name in ("metrics", "report", "plan", "cost"):
        p = run_dir / f"{name}.json"
        if p.exists():
            try:
                data["files"][name] = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                data["files"][name] = f"<unparseable {name}.json>"
    return data


class RunDetailScreen(Screen):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [Binding("escape", "back", "Back")]

    def __init__(self, store: RunStore, run_id: str) -> None:
        super().__init__()
        self._store = store
        self._run_id = run_id

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

        for name, wid in (("metrics", "#detail-metrics"), ("report", "#detail-report"), ("plan", "#detail-plan")):
            content = data["files"].get(name, f"<no {name}.json>")
            text = content if isinstance(content, str) else json.dumps(content, indent=2, default=str)
            self.query_one(wid, Static).update(text)

    def action_back(self) -> None:
        self.app.pop_screen()


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


HELP_TEXT = """\
[b]orchestral tui[/b]

  j / ↓, k / ↑   move selection
  Enter          open run detail
  /              filter runs (task, model, group, status, failure)
  g              replicate-group variance table
  n              launch a run (or replicate batch)
  x              cancel the active job
  r              refresh run list
  ?              this help
  Esc            back / close
  q              quit

Detail tabs: events stream, per-call index, metrics, report, plan.
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
            yield Label("Judge (optional)")
            yield Select([("(none)", ""), *[(m, m) for m in self._judges]], id="launch-judge", value="")
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
            "task_id": task,
            "orchestrator": orch,
            "worker": worker,
            "judge": judge,
            "replicates": reps,
            "seed": seed,
            "dry_run": self.query_one("#launch-dry", Checkbox).value,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)
