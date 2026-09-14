"""Interactive TUI for orchestral runs (textual).

Optional extra: `pip install 'orchestral[tui]'` — the base stack stays
pyyaml/httpx/rich. `run_tui` keeps the old signature and adds
tasks_dir/models_dir so the launch modal can enumerate them.
"""

from __future__ import annotations

from pathlib import Path


def run_tui(
    runs_dir: str | Path = "runs",
    tasks_dir: str | Path = "tasks",
    models_dir: str | Path = "models",
    refresh: bool = False,
) -> None:
    try:
        from orchestral.tui.app import OrchestralApp
    except ImportError:
        raise SystemExit(
            "The TUI needs textual. Install with: pip install 'orchestral[tui]'"
        ) from None
    app = OrchestralApp(
        runs_dir=Path(runs_dir),
        tasks_dir=Path(tasks_dir),
        models_dir=Path(models_dir),
    )
    app.run()
