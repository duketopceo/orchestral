"""Small widgets for the orchestral TUI."""

from __future__ import annotations

from textual.widgets import Static

from orchestral.tui.state import Job


class StatusBar(Static):
    """One-line status strip: run counts, active jobs, and errors."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $surface-darken-1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__("loading…")
        self._runs = 0
        self._cost = 0.0
        self._jobs: list[Job] = []
        self._message = ""

    def set_counts(self, runs: int, total_cost: float) -> None:
        self._runs = runs
        self._cost = total_cost
        self._render_text()

    def set_jobs(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self._render_text()

    def set_message(self, message: str) -> None:
        self._message = message
        self._render_text()

    def _render_text(self) -> None:
        parts = [f"{self._runs} runs", f"${self._cost:.4f}"]
        active = [j for j in self._jobs if j.active]
        if active:
            labels = ", ".join(f"{j.label} ({j.status.value})" for j in active[:3])
            if len(active) > 3:
                labels += f" +{len(active) - 3} more"
            parts.append(f"jobs: {labels}")
        if self._message:
            parts.append(self._message)
        self.update("  ·  ".join(parts))
