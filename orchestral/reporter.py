"""Generate a static HTML drill-down report from the run index and per-run files."""

from __future__ import annotations

import html as html_module
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestral.storage import RunStore


STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border: 1px solid #d1d5db; padding: 0.5rem; text-align: left; font-size: 0.9rem; }
  th { background: #f3f4f6; position: sticky; top: 0; }
  tr:hover { background: #f9fafb; }
  .tag { display: inline-block; background: #e5e7eb; border-radius: 999px; padding: 0.1rem 0.5rem; font-size: 0.75rem; }
  .pass { color: #15803d; background: #dcfce7; }
  .fail { color: #b91c1c; background: #fee2e2; }
  .grid { display: grid; grid-template-columns: 220px 1fr; gap: 1rem; }
  .metric { font-size: 1.25rem; font-weight: 600; }
  pre, code { font-family: ui-monospace, monospace; font-size: 0.85rem; }
  pre { background: #1f2937; color: #f3f4f6; padding: 1rem; border-radius: 0.5rem; overflow-x: auto; }
  .artifact { border: 1px solid #d1d5db; border-radius: 0.5rem; padding: 1rem; background: #fff; }
  .section { margin-top: 2rem; }
  .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem; }
  .card { border: 1px solid #e5e7eb; border-radius: 0.5rem; padding: 1rem; background: #fafafa; }
</style>
"""


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _find_artifact(run_dir: Path) -> Path | None:
    for ext in (".html", ".json", ".png", ".mp4", ".zip", ".txt"):
        p = run_dir / f"artifact{ext}"
        if p.exists():
            return p
    return None


def _esc(s: Any) -> str:
    return html_module.escape(str(s) if s is not None else "-")


def _run_card(run: Any, run_dir: Path) -> str:
    meta = run
    plan = _load_json(run_dir / "plan.json")
    cost = _load_json(run_dir / "cost.json")
    report = _load_json(run_dir / "report.json")
    events = _load_jsonl(run_dir / "events.jsonl")
    artifact_path = _find_artifact(run_dir)
    artifact = ""
    if artifact_path:
        try:
            artifact = artifact_path.read_text(encoding="utf-8")
        except Exception:
            artifact = f"<binary artifact: {artifact_path.name}>"

    pass_cls = "pass" if meta.passes else "fail" if meta.passes is False else ""
    pass_label = str(meta.passes) if meta.passes is not None else "-"

    events_rows = ""
    for ev in events:
        cost_usd = ev.get("cost", {}).get("usd", 0.0)
        tokens = ev.get("cost", {}).get("input_tokens", 0) + ev.get("cost", {}).get("output_tokens", 0)
        raw = json.dumps(ev, indent=2)
        events_rows += (
            "<tr>"
            f"<td>{_esc(ev.get('timestamp', ''))}</td>"
            f"<td><span class='tag'>{_esc(ev.get('phase',''))}</span></td>"
            f"<td>{_esc(ev.get('step',''))}</td>"
            f"<td>{_esc(ev.get('role',''))}</td>"
            f"<td>{_esc(ev.get('model',''))}</td>"
            f"<td>{_esc(ev.get('type',''))}</td>"
            f"<td>{tokens}</td>"
            f"<td>${cost_usd:.6f}</td>"
            f"<td>{_esc(ev.get('reasoning','')[:120])}</td>"
            f"<td><details><summary>raw</summary><pre>{_esc(raw)}</pre></details></td>"
            "</tr>"
        )

    artifact_section = ""
    if artifact:
        if artifact_path and artifact_path.suffix == ".html":
            artifact_section = (
                f"<h3>Artifact</h3>"
                f"<div class='artifact'>"
                f"<iframe srcdoc=\"{_esc(artifact)}\" width='100%' height='400px' sandbox='allow-same-origin'></iframe>"
                f"</div>"
            )
        else:
            artifact_section = f"<h3>Artifact</h3><pre>{_esc(artifact)}</pre>"

    cost_rows = "".join(
        f"<tr><td>{_esc(c.get('phase'))}</td><td>{_esc(c.get('model'))}</td>"
        f"<td>{c.get('input_tokens',0)}</td><td>{c.get('output_tokens',0)}</td>"
        f"<td>${c.get('cost_usd',0):.6f}</td></tr>"
        for c in cost
    ) if cost else "<tr><td colspan='5'>No cost breakdown</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>orchestral run {meta.run_id}</title>
  {STYLE}
</head>
<body>
  <a href="index.html">&larr; all runs</a>
  <h1>Run {meta.run_id}</h1>

  <div class="summary">
    <div class="card"><div class="metric">{meta.orchestrator}</div><small>orchestrator</small></div>
    <div class="card"><div class="metric">{meta.task_id}</div><small>task</small></div>
    <div class="card"><div class="metric">{meta.worker}</div><small>worker</small></div>
    <div class="card"><div class="metric">{pass_label}</div><small>pass</small></div>
    <div class="card"><div class="metric">${meta.total_cost_usd:.6f}</div><small>cost</small></div>
    <div class="card"><div class="metric">{meta.total_input_tokens + meta.total_output_tokens}</div><small>tokens</small></div>
  </div>

  <div class="section">
    <h2>Plan</h2>
    <pre>{_esc(json.dumps(plan, indent=2))}</pre>
  </div>

  <div class="section">
    <h2>Cost breakdown</h2>
    <table>
      <tr><th>phase</th><th>model</th><th>in tokens</th><th>out tokens</th><th>cost</th></tr>
      {cost_rows}
    </table>
  </div>

  <div class="section">
    <h2>Validation</h2>
    <pre>{_esc(json.dumps(report, indent=2))}</pre>
  </div>

  {artifact_section}

  <div class="section">
    <h2>Events ({len(events)})</h2>
    <table>
      <tr>
        <th>timestamp</th><th>phase</th><th>step</th><th>role</th><th>model</th><th>type</th>
        <th>tokens</th><th>cost</th><th>reasoning</th><th>raw</th>
      </tr>
      {events_rows}
    </table>
  </div>
</body>
</html>
"""


def _index_html(runs: list[Any]) -> str:
    rows = ""
    for r in runs:
        pass_cls = "pass" if r.passes else "fail" if r.passes is False else ""
        pass_label = str(r.passes) if r.passes is not None else "-"
        score = f"{r.score:.2f}" if r.score is not None else "-"
        rows += (
            f"<tr>"
            f"<td><a href='{r.run_id}.html'>{r.run_id}</a></td>"
            f"<td>{_esc(r.started_at)}</td>"
            f"<td>{_esc(r.orchestrator)}</td>"
            f"<td>{_esc(r.task_id)}</td>"
            f"<td>{_esc(r.worker)}</td>"
            f"<td>${r.total_cost_usd:.6f}</td>"
            f"<td>{score}</td>"
            f"<td><span class='tag {pass_cls}'>{pass_label}</span></td>"
            f"</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>orchestral runs</title>
  {STYLE}
</head>
<body>
  <h1>orchestral runs</h1>
  <p>Click a run to drill into events, plan, cost, and artifact.</p>
  <table>
    <tr>
      <th>run_id</th><th>started</th><th>orchestrator</th><th>task</th><th>worker</th>
      <th>cost</th><th>score</th><th>pass</th>
    </tr>
    {rows}
  </table>
</body>
</html>
"""


def generate_html_report(runs_dir: str | Path = "runs", reports_dir: str | Path = "reports") -> Path:
    """Generate index.html and one html page per run in reports_dir."""
    reports = Path(reports_dir)
    reports.mkdir(parents=True, exist_ok=True)

    store = RunStore(runs_dir)
    runs = store.list_runs(limit=None)

    for r in runs:
        run_dir = Path(r.run_dir)
        (reports / f"{r.run_id}.html").write_text(_run_card(r, run_dir), encoding="utf-8")

    (reports / "index.html").write_text(_index_html(runs), encoding="utf-8")
    return reports / "index.html"


def _bar_html(label: str, value: float, max_value: float) -> str:
    pct = (value / max_value * 100) if max_value else 0
    return (
        f"<tr><td>{_esc(label)}</td>"
        f"<td style='width:200px'><div style='width:{pct:.1f}%;background:#2563eb;height:1rem;border-radius:0.25rem;'></div></td>"
        f"<td>${value:.6f}</td></tr>"
    )


def _dashboard_html(runs: list[Any], summary: dict[str, Any]) -> str:
    total = summary["runs"]
    total_cost = summary["total_cost_usd"]
    total_tokens = summary["total_tokens"]
    passes = [r for r in runs if r.passes is True]
    pass_rate = (len(passes) / total * 100) if total else 0

    # aggregates
    by_planner: dict[str, dict[str, float]] = {}
    by_orchestrator: dict[str, dict[str, float]] = {}
    by_worker: dict[str, dict[str, float]] = {}
    for r in runs:
        planner = r.config.get("planner", "raw") if r.config else "raw"
        _bucket(by_planner, planner, r)
        _bucket(by_orchestrator, r.orchestrator, r)
        _bucket(by_worker, r.worker, r)

    recent_rows = ""
    for r in runs[:20]:
        planner = r.config.get("planner", "raw") if r.config else "raw"
        pass_label = str(r.passes) if r.passes is not None else "-"
        tokens = r.total_input_tokens + r.total_output_tokens
        recent_rows += (
            f"<tr><td><a href='{r.run_id}.html'>{r.run_id}</a></td>"
            f"<td>{_esc(planner)}</td><td>{_esc(r.orchestrator)}</td>"
            f"<td>{_esc(r.worker)}</td><td>${r.total_cost_usd:.6f}</td>"
            f"<td>{tokens}</td><td>{pass_label}</td></tr>"
        )

    def rows(table: dict[str, dict[str, float]]) -> str:
        max_cost = max((v["cost"] for v in table.values()), default=0.0)
        return "".join(_bar_html(k, v["cost"], max_cost) for k, v in sorted(table.items(), key=lambda x: -x[1]["cost"]))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>orchestral dashboard</title>
  {STYLE}
</head>
<body>
  <h1>orchestral dashboard</h1>
  <a href="index.html">&larr; per-run drill-down</a>

  <div class="summary">
    <div class="card"><div class="metric">{total}</div><small>runs</small></div>
    <div class="card"><div class="metric">${total_cost:.4f}</div><small>total cost</small></div>
    <div class="card"><div class="metric">{total_tokens}</div><small>total tokens</small></div>
    <div class="card"><div class="metric">{pass_rate:.1f}%</div><small>pass rate</small></div>
  </div>

  <div class="section">
    <h2>Cost by planner</h2>
    <table>{rows(by_planner)}</table>
  </div>

  <div class="section">
    <h2>Cost by orchestrator</h2>
    <table>{rows(by_orchestrator)}</table>
  </div>

  <div class="section">
    <h2>Cost by worker</h2>
    <table>{rows(by_worker)}</table>
  </div>

  <div class="section">
    <h2>Recent runs</h2>
    <table>
      <tr><th>run_id</th><th>planner</th><th>orchestrator</th><th>worker</th><th>cost</th><th>tokens</th><th>pass</th></tr>
      {recent_rows}
    </table>
  </div>
</body>
</html>
"""


def _bucket(table: dict[str, dict[str, float]], key: str, run: Any) -> None:
    if key not in table:
        table[key] = {"cost": 0.0, "tokens": 0, "runs": 0}
    table[key]["cost"] += run.total_cost_usd
    table[key]["tokens"] += run.total_input_tokens + run.total_output_tokens
    table[key]["runs"] += 1


def generate_dashboard(runs_dir: str | Path = "runs", reports_dir: str | Path = "reports") -> Path:
    """Generate a stats dashboard from all stored runs."""
    reports = Path(reports_dir)
    reports.mkdir(parents=True, exist_ok=True)

    store = RunStore(runs_dir)
    runs = store.list_runs(limit=None)
    summary = store.summary()

    (reports / "dashboard.html").write_text(_dashboard_html(runs, summary), encoding="utf-8")
    return reports / "dashboard.html"
