"""Generate a static HTML drill-down report from the run index and per-run files."""

from __future__ import annotations

import html as html_module
import json
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from orchestral import design_tokens
from orchestral.storage import RunStore

_CSS = """
  *, *::before, *::after { box-sizing: border-box; }
  html { background: var(--canvas); }
  body { font-family: var(--font-sans); background: var(--canvas); color: var(--ink); margin: 2rem; line-height: 1.5; }
  @media (max-width: 600px) { body { margin: 1rem; } table { display: block; overflow-x: auto; } }
  h1, h2, h3 { font-weight: 600; }
  a { color: var(--judge-text); text-decoration: underline; text-underline-offset: 2px; }
  a:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; box-shadow: 0 0 0 4px var(--focus-halo); }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border: 1px solid var(--rule); padding: 0.5rem; text-align: left; font-size: 0.9rem; }
  th { background: var(--sunken); position: sticky; top: 0; }
  tr:hover { background: var(--sunken); }
  .tag { display: inline-block; background: var(--sunken); border: 1px solid var(--rule); padding: 0.1rem 0.5rem; font-size: 0.75rem; }
  .pass { color: var(--pass-text); background: var(--pass-wash); }
  .fail { color: var(--fail-text); background: var(--fail-wash); }
  .grid { display: grid; grid-template-columns: 220px 1fr; gap: 1rem; }
  .metric { font-size: 1.25rem; font-weight: 600; }
  pre, code { font-family: var(--font-mono); font-size: 0.85rem; }
  pre { background: var(--sunken); color: var(--ink); border: 1px solid var(--rule); padding: 1rem; overflow-x: auto; }
  .artifact { border: 1px solid var(--rule); padding: 1rem; background: var(--surface); }
  .section { margin-top: 2rem; }
  .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem; }
  .card { border: 1px solid var(--rule); padding: 1rem; background: var(--surface); }
  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr)); gap: 1rem; }
  .gallery .card { padding: 0; overflow: hidden; }
  .gallery .thumb { width: 100%; height: 240px; border: 0; border-bottom: 1px solid var(--rule); display: block; object-fit: cover; object-position: top; background: var(--surface); }
  .gallery .meta { padding: 0.75rem; font-size: 0.85rem; }
  .gallery .meta .tags { margin-top: 0.4rem; }
  .bar { background: var(--judge-fill); height: 1rem; }
  .note { font-size: 0.85rem; color: var(--ink-2); }
  .nullglyph { display: inline-block; width: 10px; height: 0; border-top: 2px solid var(--ink-3); vertical-align: middle; }
  .chart { max-width: 720px; width: 100%; height: auto; font-family: var(--font-sans); }
  .chart .grid-line { stroke: var(--rule); }
  .chart .axis { stroke: var(--rule-strong); }
  .chart .tick { font-size: 11px; fill: var(--ink-3); }
  .chart .axis-title { font-size: 12px; fill: var(--ink-2); }
  .chart .series-label { font-size: 11px; fill: var(--ink); }
"""

# Reports are paper only and single-file: the token block is read from
# ui/tokens.css at import and inlined, so nothing is fetched.
STYLE = "<style>\n" + design_tokens.css_block(("paper",), categorical=True) + "\n" + _CSS + "</style>"
NULL = "<span class='nullglyph' role='img' aria-label='no data'></span>"


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
            artifact = _binary_artifact_label(artifact_path)

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
  <a href="index.html">All runs</a>
  <h1>Run {meta.run_id}</h1>

  <div class="summary">
    <div class="card"><div class="metric">{meta.orchestrator}</div><small>Orchestrator</small></div>
    <div class="card"><div class="metric">{meta.task_id}</div><small>Task</small></div>
    <div class="card"><div class="metric">{meta.worker}</div><small>Worker</small></div>
    <div class="card"><div class="metric">{pass_label}</div><small>Pass</small></div>
    <div class="card"><div class="metric">${meta.total_cost_usd:.6f}</div><small>Cost</small></div>
    <div class="card"><div class="metric">{meta.total_input_tokens + meta.total_output_tokens}</div><small>Tokens</small></div>
  </div>

  <div class="section">
    <h2>Plan</h2>
    <pre>{_esc(json.dumps(plan, indent=2))}</pre>
  </div>

  <div class="section">
    <h2>Cost Breakdown</h2>
    <table>
      <tr><th>Phase</th><th>Model</th><th>Input tokens</th><th>Output tokens</th><th>Cost</th></tr>
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
        <th>Timestamp</th><th>Phase</th><th>Step</th><th>Role</th><th>Model</th><th>Type</th>
        <th>Tokens</th><th>Cost</th><th>Reasoning</th><th>Raw</th>
      </tr>
      {events_rows}
    </table>
  </div>
</body>
</html>
"""


def _groups_table_html(runs: list[Any]) -> str:
    """Replicate-group variance table; empty when no run carries a group."""
    from orchestral.stats import aggregate

    cells = [c for c in aggregate(runs) if c.run_group]
    if not cells:
        return ""
    rows = ""
    for c in cells:
        score = f"{c.score_mean:.2f} &plusmn; {c.score_sd:.2f}" if c.score_mean is not None else "-"
        jscore = (f"{c.judge_score_mean:.2f} &plusmn; {c.judge_score_sd:.2f}"
                  if c.judge_score_mean is not None else "-")
        cost = f"${c.cost_mean:.4f} &plusmn; ${c.cost_sd:.4f}"
        spd = f"{c.successes_per_dollar:.0f}" if c.successes_per_dollar is not None else "-"
        fails = ", ".join(f"{_esc(k.split(':')[-1])}&times;{v}" for k, v in sorted(c.failures.items()))
        pass_pct = f"{c.pass_rate * 100:.0f}%" if c.pass_rate is not None else "-"
        rows += (
            f"<tr>"
            f"<td>{_esc(c.run_group)}</td>"
            f"<td>{_esc(c.task_id)}</td>"
            f"<td>{_esc(c.orchestrator)}</td>"
            f"<td>{_esc(c.worker)}</td>"
            f"<td>{c.runs}</td>"
            f"<td>{pass_pct}</td>"
            f"<td>{score}</td>"
            f"<td>{jscore}</td>"
            f"<td>{cost}</td>"
            f"<td>{c.latency_p50:.0f} / {c.latency_p95:.0f}</td>"
            f"<td>{spd}</td>"
            f"<td>{fails or '-'}</td>"
            f"</tr>"
        )
    return f"""
  <h2>Replicate Groups</h2>
  <table>
    <tr>
      <th>Group</th><th>Task</th><th>Orchestrator</th><th>Worker</th>
      <th>n</th><th>Pass %</th><th>Mech. &plusmn; SD</th><th>Judge &plusmn; SD</th><th>Cost &plusmn; SD</th>
      <th>p50 / p95 latency</th><th>Pass / $</th><th>Failures</th>
    </tr>
    {rows}
  </table>
"""


def _index_html(runs: list[Any]) -> str:
    rows = ""
    for r in runs:
        pass_cls = "pass" if r.passes else "fail" if r.passes is False else ""
        pass_label = str(r.passes) if r.passes is not None else "-"
        score = f"{r.score:.2f}" if r.score is not None else "-"
        jscore = f"{r.judge_score:.2f}" if r.judge_score is not None else "-"
        group_cell = _esc(r.run_group) if r.run_group else "-"
        rep_cell = str(r.replicate) if r.replicate is not None else "-"
        rows += (
            f"<tr>"
            f"<td><a href='{r.run_id}.html'>{r.run_id}</a></td>"
            f"<td>{_esc(r.started_at)}</td>"
            f"<td>{_esc(r.orchestrator)}</td>"
            f"<td>{_esc(r.task_id)}</td>"
            f"<td>{_esc(r.worker)}</td>"
            f"<td>{group_cell}</td>"
            f"<td>{rep_cell}</td>"
            f"<td>${r.total_cost_usd:.6f}</td>"
            f"<td>{score}</td>"
            f"<td>{jscore}</td>"
            f"<td><span class='tag {pass_cls}'>{pass_label}</span></td>"
            f"</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>orchestral: Runs</title>
  {STYLE}
</head>
<body>
  <h1>Orchestral: Runs</h1>
  <p>Click a run to drill into events, plan, cost, and artifact. <a href="gallery.html">Visual gallery</a> &middot; <a href="dashboard.html">Dashboard</a></p>
  {_groups_table_html(runs)}
  <table>
    <tr>
      <th>Run ID</th><th>Started</th><th>Orchestrator</th><th>Task</th><th>Worker</th>
      <th>Group</th><th>Replicate</th><th>Cost</th><th>Mechanical</th><th>Judge</th><th>Pass</th>
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


def _binary_artifact_label(artifact_path: Path) -> str:
    """Describe a binary artifact; for a zip, list its members (names only).

    Names are returned raw; the caller escapes once for HTML.
    """
    if artifact_path.suffix == ".zip":
        names = _zip_names(artifact_path)
        if names:
            return "archive members: " + ", ".join(names)
    return f"<binary artifact: {artifact_path.name}>"


def _zip_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return sorted(i.filename for i in archive.infolist())
    except (OSError, zipfile.BadZipFile):
        return []


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
    elif artifact and artifact.suffix == ".zip" and _copy_for_gallery(artifact, shots_dir / f"{run.run_id}-artifact.zip"):
        names = _zip_names(artifact)
        listing = "<br>".join(_esc(n) for n in names) or "empty archive"
        thumb = f"<div class='thumb'><a href='shots/{run.run_id}-artifact.zip'>{listing}</a></div>"
    else:
        thumb = "<div class='thumb'>no visual artifact</div>"

    pass_cls = "pass" if run.passes else "fail" if run.passes is False else ""
    pass_label = str(run.passes) if run.passes is not None else "-"
    score = f"{run.score:.2f}" if run.score is not None else "-"
    jscore_tag = (f"<span class='tag'>judge {run.judge_score:.2f}</span> "
                  if run.judge_score is not None else "")
    return (
        f"<div class='card'>"
        f"<a href='{run.run_id}.html'>{thumb}</a>"
        f"<div class='meta'>"
        f"<div><a href='{run.run_id}.html'>{run.run_id}</a></div>"
        f"<div>{_esc(run.orchestrator)} &rarr; {_esc(run.worker)}</div>"
        f"<div class='tags'><span class='tag {pass_cls}'>{pass_label}</span> "
        f"<span class='tag'>mech {score}</span> {jscore_tag}"
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
  <title>orchestral: Gallery</title>
  {STYLE}
</head>
<body>
  <h1>Orchestral: Gallery</h1>
  <a href="index.html">All runs</a> &middot; <a href="dashboard.html">Dashboard</a>
  {sections}
</body>
</html>
"""


def _bar_html(label: str, value: float, max_value: float) -> str:
    pct = (value / max_value * 100) if max_value else 0
    return (
        f"<tr><td>{_esc(label)}</td>"
        f"<td style='width:200px'><div class='bar' style='width:{pct:.1f}%'></div></td>"
        f"<td>${value:.6f}</td></tr>"
    )


def _dashboard_html(runs: list[Any], summary: dict[str, Any], *, experiment: str = "") -> str:
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
  <title>orchestral: Dashboard</title>
  {STYLE}
</head>
<body>
  <h1>Orchestral: Dashboard</h1>
  <a href="index.html">Per-run drill-down</a> &middot; <a href="gallery.html">Gallery</a>

  <div class="summary">
    <div class="card"><div class="metric">{total}</div><small>Runs</small></div>
    <div class="card"><div class="metric">${total_cost:.4f}</div><small>Total cost</small></div>
    <div class="card"><div class="metric">{total_tokens}</div><small>Total tokens</small></div>
    <div class="card"><div class="metric">{pass_rate:.1f}%</div><small>Pass rate</small></div>
  </div>

  <div class="section">
    <h2>Cost by Planner</h2>
    <table>{rows(by_planner)}</table>
  </div>

  <div class="section">
    <h2>Cost by Orchestrator</h2>
    <table>{rows(by_orchestrator)}</table>
  </div>

  <div class="section">
    <h2>Cost by Worker</h2>
    <table>{rows(by_worker)}</table>
  </div>

  <div class="section">
    <h2>Cost vs Quality</h2>
    {scatter}
  </div>

  {history_sections}

  {experiment}

  <div class="section">
    <h2>Recent Runs</h2>
    <table>
      <tr><th>Run ID</th><th>Planner</th><th>Orchestrator</th><th>Worker</th><th>Cost</th><th>Tokens</th><th>Pass</th></tr>
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


def _unjudged_note(n: int) -> str:
    if not n:
        return ""
    verb = "run without a judge score is" if n == 1 else "runs without a judge score are"
    return f"{n} {verb} not plotted."


def _scatter_svg(runs: list[Any]) -> str:
    """Cost vs judge score. Only judged runs are plotted: the y axis is the judge
    score and never mixes in mechanical pass/fail. Unjudged runs are counted in
    text. Series use the Okabe-Ito order (max 7 hues); further pairings share a
    neutral mark and are labeled directly."""
    finished = [r for r in runs if r.status == "finished"]
    if not finished:
        return "<p>No finished runs yet.</p>"
    pts = [r for r in finished if r.judge_score is not None]
    note = _unjudged_note(len(finished) - len(pts))
    note_html = f"<p class='note'>{note}</p>" if note else ""
    if not pts:
        return f"<p>No judged runs to plot.</p>{note_html}"
    w, h, pad_l, pad_r, pad_t, pad_b = 720, 340, 70, 190, 20, 50
    x_max = max(r.total_cost_usd for r in pts) or 1.0

    def px(v: float) -> float:
        return pad_l + (v / x_max) * (w - pad_l - pad_r)

    def py(q: float) -> float:
        return pad_t + (1 - q) * (h - pad_t - pad_b)

    n_hues = len(design_tokens.CATEGORICAL)
    colors: dict[str, str] = {}
    best: dict[str, float] = {}
    circles = []
    for r in pts:
        pairing = f"{r.orchestrator} \u2192 {r.worker}"
        if pairing not in colors:
            i = len(colors)
            colors[pairing] = f"var(--cat-{i + 1})" if i < n_hues else "var(--ink-2)"
        color = colors[pairing]
        q = float(r.judge_score)
        best[pairing] = max(best.get(pairing, 0.0), q)
        label = f"{_esc(pairing)} \u00b7 {_esc(r.task_id)} \u00b7 ${r.total_cost_usd:.4f} \u00b7 judge {q:.2f}"
        extra = "" if color.startswith("var(--cat") else " stroke-dasharray='2 2'"
        circles.append(
            f"<circle cx='{px(r.total_cost_usd):.1f}' cy='{py(q):.1f}' r='5'"
            f" style='fill:{color};fill-opacity:.85;stroke:{color}'{extra}>"
            f"<title>{label}</title></circle>"
        )

    ticks = []
    for i in range(5):
        yv = i / 4
        y = py(yv)
        ticks.append(
            f"<line class='grid-line' x1='{pad_l}' y1='{y:.1f}' x2='{w - pad_r}' y2='{y:.1f}'/>"
            f"<text class='tick' x='{pad_l - 8}' y='{y + 4:.1f}' text-anchor='end'>{yv:.2f}</text>"
        )
    for i in range(6):
        xv = x_max * i / 5
        ticks.append(f"<text class='tick' x='{px(xv):.1f}' y='{h - pad_b + 18}' text-anchor='middle'>${xv:.3f}</text>")

    # direct labels in the right margin, ordered by height with a minimum gap
    labels = []
    last_y = -99.0
    for pairing, q in sorted(best.items(), key=lambda kv: -kv[1]):
        y = max(py(q) + 4, last_y + 14)
        last_y = y
        text = pairing if len(pairing) <= 28 else pairing[:27] + "\u2026"
        labels.append(
            f"<text class='series-label' x='{w - pad_r + 18}' y='{y:.1f}'>{_esc(text)}</text>"
            f"<rect x='{w - pad_r + 6}' y='{y - 8:.1f}' width='8' height='8' style='fill:{colors[pairing]}'/>"
        )

    desc = f"{len(pts)} judged runs across {len(colors)} pairings, cost per run against judge score."
    return (
        f"<svg class='chart' viewBox='0 0 {w} {h}' role='img' aria-labelledby='sc-t sc-d'>"
        "<title id='sc-t'>Cost vs judge score</title>"
        f"<desc id='sc-d'>{_esc(desc)}</desc>"
        + "".join(ticks)
        + f"<line class='axis' x1='{pad_l}' y1='{pad_t}' x2='{pad_l}' y2='{h - pad_b}'/>"
        + f"<line class='axis' x1='{pad_l}' y1='{h - pad_b}' x2='{w - pad_r}' y2='{h - pad_b}'/>"
        + "".join(circles)
        + "".join(labels)
        + f"<text class='axis-title' x='{(pad_l + w - pad_r) / 2:.0f}' y='{h - 8}' text-anchor='middle'>cost per run (USD)</text>"
        + "</svg>"
        + f"<p class='note'>y = judge score, judged runs only. {note}</p>"
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
        f"<table><tr><th>Model</th><th>Runs</th><th>Pass rate</th><th>Avg. score</th><th>Avg. cost</th><th>Total cost</th></tr>"
        f"{rows}</table></div>"
    )


def generate_dashboard(
    runs_dir: str | Path = "runs",
    reports_dir: str | Path = "reports",
    matrix_path: str | Path | None = None,
) -> Path:
    """Generate a stats dashboard from all stored runs.

    When ``matrix_path`` (or the default ``experiments/jev-ab.yaml`` next to
    the runs dir) exists, an experiment arm-comparison section is included;
    mechanical pass is the primary axis; judge deltas are self-referential.
    """
    reports = Path(reports_dir)
    reports.mkdir(parents=True, exist_ok=True)

    store = RunStore(runs_dir)
    runs = store.list_runs(limit=None)
    summary = store.summary()

    mp = Path(matrix_path) if matrix_path else Path(runs_dir).parent / "experiments" / "jev-ab.yaml"
    experiment = _experiment_html(store, mp) if mp.exists() else ""

    (reports / "dashboard.html").write_text(
        _dashboard_html(runs, summary, experiment=experiment), encoding="utf-8")
    return reports / "dashboard.html"


def _experiment_html(store: Any, matrix_path: Path) -> str:
    """Arm-comparison table for the dashboard, the same cell rows the
    observatory's Experiment section renders from /api/experiment."""
    from orchestral.coverage import coverage_rows
    from orchestral.experiment import load_matrix

    matrix = load_matrix(matrix_path)
    rows = coverage_rows(store, matrix)
    if not rows:
        return ""
    body = "".join(
        f"<tr><td>{_esc(r.task_id)}<br><small>{_esc(r.orchestrator)} → {_esc(r.worker)}</small></td>"
        f"<td>{f'{r.baseline_passes}/{r.baseline_n}' if r.baseline_n else NULL}</td>"
        f"<td>{f'{r.jev_passes}/{r.jev_n}' if r.jev_n else NULL}</td>"
        f"<td>{f'[{r.diff[0]:+.2f}, {r.diff[1]:+.2f}]' if r.diff else NULL}</td>"
        f"<td>{_esc(r.verdict)}</td><td>{_esc(r.state)}</td>"
        f"<td>{'posted' if r.posted else ''}</td></tr>"
        for r in rows
    )
    return f"""<div class="section">
    <h2>Experiment: {_esc(matrix.name)}</h2>
    <p><small>Baseline vs jev-assist, paired replicates. Primary axis: mechanical pass.
    Judge-score deltas are self-referential (the decisions engine assists the
    jev arm and scores both arms).</small></p>
    <table>
      <tr><th>Cell</th><th>Baseline</th><th>Jev</th><th>Diff CI</th><th>Verdict</th><th>State</th><th>Posted</th></tr>
      {body}
    </table>
  </div>"""
