"""Generate a static HTML drill-down report from the run index and per-run files."""

from __future__ import annotations

import html as html_module
import json
import shutil
from collections import defaultdict
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
  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 1rem; }
  .gallery .card { padding: 0; overflow: hidden; }
  .gallery .thumb { width: 100%; height: 240px; border: 0; border-bottom: 1px solid #e5e7eb; display: block; object-fit: cover; object-position: top; background: #fff; }
  .gallery .meta { padding: 0.75rem; font-size: 0.85rem; }
  .gallery .meta .tags { margin-top: 0.4rem; }
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
  <p>Click a run to drill into events, plan, cost, and artifact. <a href="gallery.html">Visual gallery</a> &middot; <a href="dashboard.html">Dashboard</a></p>
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
    (reports / "gallery.html").write_text(generate_gallery(runs, reports), encoding="utf-8")
    return reports / "index.html"


def _copy_for_gallery(src: Path, dest: Path) -> bool:
    try:
        shutil.copyfile(src, dest)
        return True
    except OSError:
        return False


def _gallery_card(run: Any, shots_dir: Path) -> str:
    run_dir = Path(run.run_dir)
    shot = run_dir / "screenshot.png"
    artifact = _find_artifact(run_dir)

    if shot.exists() and _copy_for_gallery(shot, shots_dir / f"{run.run_id}.png"):
        thumb = f"<img class='thumb' src='shots/{run.run_id}.png' loading='lazy' alt='screenshot'>"
    elif artifact and artifact.suffix == ".html" and _copy_for_gallery(artifact, shots_dir / f"{run.run_id}.html"):
        thumb = f"<iframe class='thumb' src='shots/{run.run_id}.html' sandbox loading='lazy'></iframe>"
    elif artifact and artifact.suffix == ".png" and _copy_for_gallery(artifact, shots_dir / f"{run.run_id}-artifact.png"):
        thumb = f"<img class='thumb' src='shots/{run.run_id}-artifact.png' loading='lazy' alt='artifact'>"
    elif artifact and artifact.suffix == ".mp4" and _copy_for_gallery(artifact, shots_dir / f"{run.run_id}-artifact.mp4"):
        thumb = f"<video class='thumb' src='shots/{run.run_id}-artifact.mp4' muted playsinline preload='metadata'></video>"
    else:
        thumb = "<div class='thumb'>no visual artifact</div>"

    pass_cls = "pass" if run.passes else "fail" if run.passes is False else ""
    pass_label = str(run.passes) if run.passes is not None else "-"
    score = f"{run.score:.2f}" if run.score is not None else "-"
    return (
        f"<div class='card'>"
        f"<a href='{run.run_id}.html'>{thumb}</a>"
        f"<div class='meta'>"
        f"<div><a href='{run.run_id}.html'>{run.run_id}</a></div>"
        f"<div>{_esc(run.orchestrator)} &rarr; {_esc(run.worker)}</div>"
        f"<div class='tags'><span class='tag {pass_cls}'>{pass_label}</span> "
        f"<span class='tag'>score {score}</span> "
        f"<span class='tag'>${run.total_cost_usd:.4f}</span></div>"
        f"</div></div>"
    )


def generate_gallery(runs: list[Any], reports_dir: Path, task_id: str | None = None) -> str:
    """Render a visual comparison grid: one card per run, grouped by task."""
    finished = [r for r in runs if r.status == "finished"]
    if task_id:
        finished = [r for r in finished if r.task_id == task_id]

    by_task: dict[str, list[Any]] = defaultdict(list)
    for r in finished:
        by_task[r.task_id].append(r)

    shots_dir = reports_dir / "shots"
    if finished:
        shots_dir.mkdir(exist_ok=True)
    sections = "".join(
        f"<div class='section'><h2>{_esc(tid)}</h2><div class='gallery'>"
        + "".join(_gallery_card(r, shots_dir) for r in by_task[tid])
        + "</div></div>"
        for tid in sorted(by_task)
    ) or "<p>No finished runs yet.</p>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>orchestral gallery</title>
  {STYLE}
</head>
<body>
  <h1>orchestral gallery</h1>
  <a href="index.html">&larr; all runs</a> &middot; <a href="dashboard.html">dashboard</a>
  {sections}
</body>
</html>
"""


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

    scatter = _scatter_svg(runs)
    history = model_history(runs)
    history_sections = "".join(
        _history_table_html(history[role], role)
        for role in ("orchestrator", "worker", "judge")
        if history[role]
    )

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
  <a href="index.html">&larr; per-run drill-down</a> &middot; <a href="gallery.html">gallery</a>

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
    <h2>Cost vs quality</h2>
    {scatter}
  </div>

  {history_sections}

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


def model_history(runs: list[Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Aggregate finished runs into per-model stats by role.

    Returns {"orchestrator": {slug: stats}, "worker": {...}, "judge": {...}}
    where stats carry runs, pass_rate, avg_score, avg_cost, total_cost, tokens.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {"orchestrator": {}, "worker": {}, "judge": {}}

    def acc(role: str, name: str, run: Any) -> None:
        g = out[role].setdefault(name, {"runs": 0, "passed": 0, "scores": [], "cost": 0.0, "tokens": 0})
        g["runs"] += 1
        g["passed"] += 1 if run.passes else 0
        if run.score is not None:
            g["scores"].append(run.score)
        g["cost"] += run.total_cost_usd
        g["tokens"] += run.total_input_tokens + run.total_output_tokens

    for r in runs:
        if r.status != "finished":
            continue
        acc("orchestrator", r.orchestrator, r)
        acc("worker", r.worker, r)
        judge = (r.config or {}).get("judge")
        if judge:
            acc("judge", judge, r)

    for table in out.values():
        for s in table.values():
            s["pass_rate"] = s["passed"] / s["runs"] if s["runs"] else None
            s["avg_score"] = sum(s["scores"]) / len(s["scores"]) if s["scores"] else None
            s["avg_cost"] = s["cost"] / s["runs"] if s["runs"] else 0.0
            s["total_cost"] = s["cost"]
            del s["passed"], s["scores"], s["cost"]
    return out


_SCATTER_PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#9333ea", "#0891b2", "#db2777", "#65a30d"]


def _scatter_svg(runs: list[Any]) -> str:
    """Cost-vs-quality SVG scatter: x = run cost, y = judge score or pass (1/0)."""
    pts = [r for r in runs if r.status == "finished"]
    if not pts:
        return "<p>No finished runs yet.</p>"
    w, h, pad_l, pad_r, pad_t, pad_b = 720, 340, 70, 20, 20, 50
    xs = [r.total_cost_usd for r in pts]
    x_max = max(xs) or 1.0

    def quality(r: Any) -> float:
        if r.score is not None:
            return r.score
        return 1.0 if r.passes else 0.0

    def px(v: float) -> float:
        return pad_l + (v / x_max) * (w - pad_l - pad_r)

    def py(q: float) -> float:
        return pad_t + (1 - q) * (h - pad_t - pad_b)

    pairing_colors: dict[str, str] = {}
    circles = []
    for r in pts:
        pairing = f"{r.orchestrator} → {r.worker}"
        color = pairing_colors.setdefault(pairing, _SCATTER_PALETTE[len(pairing_colors) % len(_SCATTER_PALETTE)])
        q = quality(r)
        judged = r.score is not None
        label = f"{_esc(pairing)} · {_esc(r.task_id)} · ${r.total_cost_usd:.4f} · {'score' if judged else 'pass'} {q:.2f}"
        circles.append(
            f"<circle cx='{px(r.total_cost_usd):.1f}' cy='{py(q):.1f}' r='5' fill='{color}'"
            f" fill-opacity='{0.85 if judged else 0.4}' stroke='{color}' stroke-width='1'>"
            f"<title>{label}</title></circle>"
        )

    ticks = []
    for i in range(5):
        yv = i / 4
        y = py(yv)
        ticks.append(
            f"<line x1='{pad_l}' y1='{y:.1f}' x2='{w - pad_r}' y2='{y:.1f}' stroke='#e5e7eb'/>"
            f"<text x='{pad_l - 8}' y='{y + 4:.1f}' text-anchor='end' font-size='11' fill='#6b7280'>{yv:.2f}</text>"
        )
    for i in range(6):
        xv = x_max * i / 5
        x = px(xv)
        ticks.append(f"<text x='{x:.1f}' y='{h - pad_b + 18}' text-anchor='middle' font-size='11' fill='#6b7280'>${xv:.3f}</text>")

    legend = "".join(
        f"<span class='tag' style='background:{c}22;color:{c}'>{_esc(pairing)}</span> "
        for pairing, c in pairing_colors.items()
    )
    return (
        f"<svg viewBox='0 0 {w} {h}' style='max-width:720px;width:100%;height:auto'>"
        + "".join(ticks)
        + f"<line x1='{pad_l}' y1='{pad_t}' x2='{pad_l}' y2='{h - pad_b}' stroke='#9ca3af'/>"
        + f"<line x1='{pad_l}' y1='{h - pad_b}' x2='{w - pad_r}' y2='{h - pad_b}' stroke='#9ca3af'/>"
        + "".join(circles)
        + f"<text x='{(pad_l + w - pad_r) / 2:.0f}' y='{h - 8}' text-anchor='middle' font-size='12' fill='#374151'>cost per run (USD)</text>"
        + "</svg>"
        + "<p style='font-size:0.85rem;color:#6b7280'>y = judge score; unjudged runs plotted as pass 1.0 / fail 0.0 (faded).</p>"
        + f"<div class='tags'>{legend}</div>"
    )


def _history_table_html(table: dict[str, dict[str, Any]], role: str) -> str:
    def _row(name: str, s: dict[str, Any]) -> str:
        pass_pct = f"{s['pass_rate'] * 100:.1f}%" if s["pass_rate"] is not None else "-"
        score = f"{s['avg_score']:.2f}" if s["avg_score"] is not None else "-"
        return (
            f"<tr><td>{_esc(name)}</td><td>{s['runs']}</td><td>{pass_pct}</td>"
            f"<td>{score}</td><td>${s['avg_cost']:.6f}</td><td>${s['total_cost']:.4f}</td></tr>"
        )

    rows = "".join(_row(name, s) for name, s in sorted(table.items(), key=lambda kv: -kv[1]["total_cost"]))
    return (
        f"<div class='section'><h2>{_esc(role.capitalize())} history</h2>"
        f"<table><tr><th>model</th><th>runs</th><th>pass rate</th><th>avg score</th><th>avg cost</th><th>total cost</th></tr>"
        f"{rows}</table></div>"
    )


def generate_dashboard(runs_dir: str | Path = "runs", reports_dir: str | Path = "reports") -> Path:
    """Generate a stats dashboard from all stored runs."""
    reports = Path(reports_dir)
    reports.mkdir(parents=True, exist_ok=True)

    store = RunStore(runs_dir)
    runs = store.list_runs(limit=None)
    summary = store.summary()

    (reports / "dashboard.html").write_text(_dashboard_html(runs, summary), encoding="utf-8")
    return reports / "dashboard.html"
