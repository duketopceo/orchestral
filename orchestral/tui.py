"""Terminal UI for orchestral runs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orchestral.storage import RunStore


BANNER = r"""
   ____  ____  ____  ____  ____  ____  ____  ____  
  / __ \/ __ \/ __ \/ __ \/ __ \/ __ \/ __ \/ __ \ 
 / / / / / / / / / / /_/ / /_/ / / / / / / / /_/ /
/ / / / /_/ / /_/ / / / / / / / /_/ / / / / / / / 
\__/_/\____/\____/_/ /_/_/\__/\____/_/ /_/_/\____/
"""


def _run_row(run: Any) -> tuple[str, ...]:
    planner = run.config.get("planner", "raw") if run.config else "raw"
    pass_style = "green" if run.passes else "red" if run.passes is False else "dim"
    pass_label = Text(str(run.passes) if run.passes is not None else "-", style=pass_style)
    tokens = str(run.total_input_tokens + run.total_output_tokens)
    return (
        run.run_id,
        planner,
        run.orchestrator,
        run.worker,
        f"${run.total_cost_usd:.4f}",
        tokens,
        pass_label,
    )


def render_tui(runs_dir: str | Path = "runs", refresh: bool = False) -> None:
    console = Console()
    store = RunStore(runs_dir)
    runs = store.list_runs(limit=None)
    summary = store.summary()

    console.clear()
    console.print(Text(BANNER, style="bold cyan"))
    console.print(Panel(f"Total runs: {summary['runs']} | Total cost: ${summary['total_cost_usd']:.4f} | Total tokens: {summary['total_tokens']}", title="Stats", border_style="green"))

    table = Table(title="Runs", show_header=True, header_style="bold magenta")
    table.add_column("Run ID", style="dim")
    table.add_column("Planner")
    table.add_column("Orchestrator")
    table.add_column("Worker")
    table.add_column("Cost", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Pass", justify="center")

    for run in runs:
        table.add_row(*_run_row(run))

    console.print(table)
    console.print("\nDrill down: python3 harness.py report --html && open reports/index.html")
    console.print("Stats dashboard: python3 harness.py dashboard && open reports/dashboard.html")

    if refresh:
        console.print("\nRefreshing every 5s. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(5)
                render_tui(runs_dir=runs_dir, refresh=False)
        except KeyboardInterrupt:
            pass


def run_tui(runs_dir: str | Path = "runs", refresh: bool = False) -> None:
    render_tui(runs_dir=runs_dir, refresh=refresh)
