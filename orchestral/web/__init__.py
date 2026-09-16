"""Local web observatory for orchestral runs.

Stdlib only — the base stack stays pyyaml/httpx/rich. ``run_server`` binds
localhost exclusively; there is deliberately no host flag.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path


def run_server(
    runs_dir: str | Path = "runs",
    tasks_dir: str | Path = "tasks",
    models_dir: str | Path = "models",
    port: int = 8787,
    open_browser: bool = False,
) -> None:
    from orchestral.web.server import serve

    url = f"http://127.0.0.1:{port}"
    print(f"orchestral observatory: {url}")
    print("localhost only — Ctrl-C to stop")
    if open_browser:
        webbrowser.open(url)
    serve(Path(runs_dir), Path(tasks_dir), Path(models_dir), port)
