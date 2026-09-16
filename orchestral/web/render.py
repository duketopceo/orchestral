"""HTML builders for the web observatory — pure string functions.

Every value derived from run data goes through ``esc`` before it touches
markup; run dirs are model output, not trusted content. Style follows the
static dashboard's: one inline <style>, tables and cards, no assets.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from html import escape as _html_escape
from typing import Any

from orchestral.tui.state import fmt_cost, fmt_ms, fmt_tokens, pass_label, status_label

STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  nav { display: flex; gap: 1.25rem; margin-bottom: 1.5rem; font-weight: 500; }
  nav a.active { color: #111827; border-bottom: 2px solid #2563eb; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border: 1px solid #d1d5db; padding: 0.5rem; text-align: left; font-size: 0.9rem; }
  th { background: #f3f4f6; position: sticky; top: 0; }
  tr:hover { background: #f9fafb; }
  .tag { display: inline-block; background: #e5e7eb; border-radius: 999px; padding: 0.1rem 0.5rem; font-size: 0.75rem; }
  .ok { color: #15803d; background: #dcfce7; }
  .err { color: #b91c1c; background: #fee2e2; }
  .warn { color: #a16207; background: #fef9c3; }
  .muted { color: #6b7280; }
  .metric { font-size: 1.25rem; font-weight: 600; }
  pre, code { font-family: ui-monospace, monospace; font-size: 0.85rem; }
  pre { background: #1f2937; color: #f3f4f6; padding: 1rem; border-radius: 0.5rem; overflow-x: auto; }
  .section { margin-top: 2rem; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem; }
  .card { border: 1px solid #e5e7eb; border-radius: 0.5rem; padding: 1rem; background: #fafafa; }
  .banner { border: 1px solid #d1d5db; border-radius: 0.5rem; padding: 1rem; margin-bottom: 1rem; }
  .banner.live { border-color: #2563eb; background: #eff6ff; }
  .chips { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
  .tabs { display: flex; gap: 0.5rem; margin: 1rem 0; }
  .tabs a { padding: 0.25rem 0.75rem; border: 1px solid #d1d5db; border-radius: 0.375rem; }
  .tabs a.active { background: #2563eb; color: #fff; border-color: #2563eb; }
  form.inline { display: inline; }
  form.launch label { display: block; margin-top: 0.75rem; font-size: 0.85rem; color: #374151; }
  form.launch select, form.launch input, form.filter input { padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 0.375rem; min-width: 16rem; }
  button, .btn { padding: 0.4rem 0.9rem; border: 1px solid #d1d5db; border-radius: 0.375rem; background: #fff; cursor: pointer; font-size: 0.9rem; }
  button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
  button.danger { color: #b91c1c; border-color: #fca5a5; }
  #event-detail { min-height: 6rem; }
  .scroll { max-height: 32rem; overflow-y: auto; border: 1px solid #e5e7eb; }
  .scroll table { margin-top: 0; }
</style>
"""

NAV = (("/", "overview"), ("/runs", "runs"), ("/leaderboard", "leaderboard"), ("/new", "new run"))

DETAIL_TABS = ("events", "calls", "metrics", "plan", "manifest", "report")

LB_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cost_per_pass", "$/pass"),
    ("pass_rate", "pass rate"),
    ("score_median", "score"),
    ("cost_median", "cost"),
    ("duration_median_ms", "duration"),
)


def esc(s: Any) -> str:
    return _html_escape(str(s) if s is not None else "-")


def _json_for_script(data: Any) -> str:
    """JSON safe to embed inside a <script> block."""
    return json.dumps(data, default=str).replace("</", "<\\/")


def page(title: str, body: str, active: str = "") -> str:
    nav = " ".join(
        f'<a href="{href}" class="{"active" if label == active else ""}">{esc(label)}</a>'
        for href, label in NAV
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>orchestral — {esc(title)}</title>
  {STYLE}
</head>
<body>
  <nav>{nav}</nav>
  {body}
</body>
</html>
"""


def _pass_tag(passes: bool | None) -> str:
    text, cls = pass_label(passes)
    return f"<span class='tag {cls}'>{esc(text)}</span>"


def _status_tag(status: str) -> str:
    text, cls = status_label(status)
    return f"<span class='tag {cls}'>{esc(text)}</span>"


def _run_link(run_id: str, status: str) -> str:
    href = f"/run/{esc(run_id)}/live" if status == "running" else f"/run/{esc(run_id)}"
    return f"<a href='{href}'>{esc(run_id)}</a>"


def _runs_table(runs: Iterable[dict[str, Any]]) -> str:
    """Runs table over RunMeta.to_dict() rows — works for page and API data."""
    rows = "".join(
        "<tr>"
        f"<td>{_run_link(r['run_id'], r['status'])}</td>"
        f"<td>{esc(r['task_id'])}</td>"
        f"<td>{esc(r['orchestrator'])}</td>"
        f"<td>{esc(r['worker'])}</td>"
        f"<td>{fmt_cost(r.get('total_cost_usd'))}</td>"
        f"<td>{_fmt_score(r.get('score'))}</td>"
        f"<td>{_pass_tag(r.get('passes'))}</td>"
        f"<td>{_status_tag(r['status'])}</td>"
        f"<td>{esc((r.get('started_at') or '')[:19])}</td>"
        "</tr>"
        for r in runs
    )
    return (
        "<table><tr><th>run</th><th>task</th><th>orchestrator</th><th>worker</th>"
        "<th>cost</th><th>score</th><th>pass</th><th>status</th><th>started</th></tr>"
        f"{rows}</table>"
    )


def _jobs_banner(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return ""
    items = []
    for j in jobs:
        run_links = " ".join(
            f"<a href='/run/{esc(rid)}/live'>{esc(rid)}</a>" for rid in j["run_ids"]
        ) or '<span class="muted">starting…</span>'
        cancel = (
            f"<form class='inline' method='post' action='/run/{esc(j['run_ids'][-1])}/cancel'>"
            "<button class='danger'>cancel</button></form>"
            if j["cancellable"] and j["run_ids"] else ""
        )
        items.append(
            f"<div class='banner live'><strong>{esc(j['label'])}</strong> "
            f"{_status_tag(str(j['status']))} <span class='muted'>{esc(j['detail'])}</span>"
            f"<div class='chips'>{run_links} {cancel}</div></div>"
        )
    return f"<div class='section'><h2>live</h2>{''.join(items)}</div>"


def _fmt_score(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def _fmt_usd(v: float | None) -> str:
    return "-" if v is None else f"${v:.4f}"


def render_overview(data: dict[str, Any]) -> str:
    lb_rows = "".join(
        f"<tr><td>{esc(r['orchestrator'])}</td><td>{esc(r['worker'])}</td>"
        f"<td>{r['runs']}</td><td>{_pct(r['pass_rate'])}</td>"
        f"<td>{_fmt_score(r['score_median'])}</td>"
        f"<td>{fmt_cost(r['cost_median'])}</td>"
        f"<td>{_fmt_usd(r['cost_per_pass'])}</td>"
        f"<td>{'low-n' if r['low_sample'] else ''}</td></tr>"
        for r in data["leaderboard"]
    )
    body = f"""
  <h1>orchestral</h1>
  {_jobs_banner(data["jobs"])}
  <div class="section">
    <h2>leaderboard <a href="/leaderboard" class="muted" style="font-size:0.8rem">all →</a></h2>
    <table><tr><th>orchestrator</th><th>worker</th><th>runs</th><th>pass rate</th>
    <th>score</th><th>cost</th><th>$/pass</th><th></th></tr>{lb_rows}</table>
  </div>
  <div class="section">
    <h2>recent runs <a href="/runs" class="muted" style="font-size:0.8rem">all →</a></h2>
    {_runs_table(data["recent"])}
  </div>
"""
    return page("overview", body, "overview")


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


def render_history(runs: list[dict[str, Any]], query: str = "") -> str:
    body = f"""
  <h1>runs</h1>
  <form class="filter" method="get" action="/runs">
    <input name="q" value="{esc(query)}" placeholder="filter: id, task, model, group, status…" />
    <button>filter</button>
  </form>
  {_runs_table(runs)}
"""
    return page("runs", body, "runs")


def render_leaderboard(rows: list[dict[str, Any]], sort: str = "cost_per_pass") -> str:
    head = "".join(
        f"<th><a href='/leaderboard?sort={k}'>{esc(label)}{' ↓' if k == sort else ''}</a></th>"
        for k, label in LB_COLUMNS
    )
    trs = "".join(
        f"<tr><td>{esc(r['orchestrator'])}</td><td>{esc(r['worker'])}</td>"
        f"<td>{r['runs']}</td><td>{r['tasks_covered']}</td>"
        f"<td>{_pct(r['pass_rate'])}</td>"
        f"<td>{_fmt_score(r['score_median'])}</td>"
        f"<td>{fmt_cost(r['cost_median'])}</td>"
        f"<td>{fmt_ms(r['duration_median_ms'])}</td>"
        f"<td>{_pct(r['failure_rate'])}</td>"
        f"<td>{_fmt_usd(r['cost_per_pass'])}</td>"
        f"<td class='muted'>{'low-n' if r['low_sample'] else ''}</td></tr>"
        for r in rows
    )
    body = f"""
  <h1>leaderboard</h1>
  <p class="muted">cost-per-pass is the primary rank; "low-n" flags pairings with
  fewer than 10 runs — anecdote, not evidence.</p>
  <table><tr><th>orchestrator</th><th>worker</th><th>runs</th><th>tasks</th>
  {head}<th></th></tr>{trs}</table>
"""
    return page("leaderboard", body, "leaderboard")


def render_detail(run_id: str, meta: Any, calls: list[dict[str, Any]],
                  sections: dict[str, Any], tab: str = "events") -> str:
    if tab not in DETAIL_TABS:
        tab = "events"
    tabs = " ".join(
        f"<a href='/run/{esc(run_id)}?tab={t}' class='{'active' if t == tab else ''}'>{t}</a>"
        for t in DETAIL_TABS
    )
    header = ""
    if meta is not None:
        group_note = f"· group {meta.run_group}" if meta.run_group else ""
        fail_note = f"· failure: {meta.failure_reason}" if meta.failure_reason else ""
        header = f"""
  <div class="cards">
    <div class="card"><div class="metric">{_status_tag(meta.status)}</div><small>status</small></div>
    <div class="card"><div class="metric">{_pass_tag(meta.passes)}</div><small>verdict</small></div>
    <div class="card"><div class="metric">{_fmt_score(meta.score)}</div><small>score</small></div>
    <div class="card"><div class="metric">{fmt_cost(meta.total_cost_usd)}</div><small>cost</small></div>
    <div class="card"><div class="metric">{fmt_tokens(meta.total_input_tokens + meta.total_output_tokens)}</div><small>tokens</small></div>
    <div class="card"><div class="metric">{fmt_ms(meta.latency_ms)}</div><small>latency</small></div>
  </div>
  <p class="muted">{esc(meta.orchestrator)} → {esc(meta.worker)} · task {esc(meta.task_id)}
  {esc(group_note)} {esc(fail_note)}</p>
"""
    body = f"<h1>run {esc(run_id)}</h1>{header}<div class='tabs'>{tabs}</div>{_detail_tab(tab, sections, calls)}"
    return page(f"run {run_id}", body, "runs")


def _detail_tab(tab: str, sections: dict[str, Any], calls: list[dict[str, Any]]) -> str:
    if tab == "events":
        rows = "".join(
            f"<tr><td>{esc(ts)}</td><td>{esc(etype)}</td><td>{esc(wid)}</td><td>{esc(detail)}</td></tr>"
            for ts, etype, wid, detail in sections.get("events", [])
        )
        return f"<div class='scroll'><table><tr><th>time</th><th>type</th><th>worker</th><th>detail</th></tr>{rows}</table></div>"
    if tab == "calls":
        rows = "".join(
            f"<tr><td>{esc(c.get('phase'))}</td><td>{esc(c.get('model'))}</td>"
            f"<td>{fmt_tokens((c.get('input_tokens') or 0) + (c.get('output_tokens') or 0))}</td>"
            f"<td>{fmt_cost(c.get('cost_usd'))}</td>"
            f"<td>{fmt_cost(c.get('api_cost_usd'))}</td>"
            f"<td>{fmt_ms(c.get('latency_ms'))}</td>"
            f"<td>{esc(c.get('error_category') or '')}</td></tr>"
            for c in calls
        )
        return f"<div class='scroll'><table><tr><th>phase</th><th>model</th><th>tokens</th><th>est $</th><th>api $</th><th>latency</th><th>error</th></tr>{rows}</table></div>"
    if tab == "plan":
        plan = sections.get("plan")
        return f"<pre>{esc(plan) if plan else '(no plan.md)'}</pre>"
    data = sections.get(tab)
    return f"<pre>{esc(json.dumps(data, indent=2, default=str)) if data is not None else f'(no {tab}.json)'}</pre>"


def render_live(run_id: str, payload: dict[str, Any], cancellable: bool) -> str:
    chips = "".join(
        f"<span class='tag'>{esc(w)}: {esc(s)}</span>" for w, s in payload["workers"].items()
    )
    cancel = (
        f"<form class='inline' method='post' action='/run/{esc(run_id)}/cancel'>"
        "<button class='danger'>cancel run</button></form>"
        if cancellable else ""
    )
    initial_rows = "".join(
        f"<tr data-i='{i}'><td>{esc(ts)}</td><td>{esc(etype)}</td><td>{esc(wid)}</td><td>{esc(detail)}</td></tr>"
        for i, (ts, etype, wid, detail) in enumerate(payload["rows"])
    )
    script = f"""
<script>
const RUN_ID = {_json_for_script(run_id)};
let next = {int(payload["next"])};
const details = {_json_for_script(payload["details"])};
const tbody = document.getElementById('events');
const panel = document.getElementById('event-detail');
tbody.addEventListener('click', e => {{
  const tr = e.target.closest('tr');
  if (tr && tr.dataset.i !== undefined) panel.textContent = details[+tr.dataset.i] || '';
}});
async function poll() {{
  try {{
    const r = await fetch(`/api/run/${{RUN_ID}}/live?after=${{next}}`);
    if (!r.ok) return;
    const d = await r.json();
    next = d.next;
    document.getElementById('phase').textContent = d.phase;
    document.getElementById('cost').textContent = '$' + d.cost_usd.toFixed(4);
    document.getElementById('elapsed').textContent = d.elapsed;
    const chips = document.getElementById('chips');
    chips.textContent = '';
    for (const [w, s] of Object.entries(d.workers)) {{
      const c = document.createElement('span');
      c.className = 'tag'; c.textContent = w + ': ' + s; chips.appendChild(c);
    }}
    d.rows.forEach((row, k) => {{
      const tr = document.createElement('tr');
      tr.dataset.i = details.length;
      details.push(d.details[k]);
      row.forEach(cell => {{
        const td = document.createElement('td');
        td.textContent = cell; tr.appendChild(td);
      }});
      tbody.appendChild(tr);
    }});
    if (d.status === 'finished' || d.status === 'failed' || d.status === 'cancelled') {{
      document.getElementById('phase').textContent = d.status;
      return;  // terminal — stop polling
    }}
  }} finally {{ setTimeout(poll, 1000); }}
}}
setTimeout(poll, 1000);
</script>
"""
    body = f"""
  <h1>run {esc(run_id)} <span class='tag warn' id='phase'>{esc(payload["phase"])}</span></h1>
  <p>
    <span class='muted'>cost</span> <span id='cost'>${payload["cost_usd"]:.4f}</span> ·
    <span class='muted'>tokens</span> <span id='tokens'>{fmt_tokens(payload["tokens"])}</span> ·
    <span class='muted'>elapsed</span> <span id='elapsed'>{esc(payload["elapsed"])}</span>
    &nbsp; {cancel}
    &nbsp; <a href='/run/{esc(run_id)}'>detail →</a>
  </p>
  <div class='chips' id='chips'>{chips}</div>
  <div class='scroll section'><table>
    <tr><th>time</th><th>type</th><th>worker</th><th>detail</th></tr>
    <tbody id='events'>{initial_rows}</tbody>
  </table></div>
  <h3>event detail</h3>
  <pre id='event-detail'>click a row to inspect</pre>
  {script}
"""
    return page(f"live {run_id}", body, "runs")


def render_new(tasks: list[str], orchestrators: list[str], workers: list[str],
               judges: list[str], error: str = "") -> str:
    def opts(items: list[str]) -> str:
        return "".join(f"<option value='{esc(s)}'>{esc(s)}</option>" for s in items)

    err = f"<p class='err' style='padding:0.5rem'>{esc(error)}</p>" if error else ""
    body = f"""
  <h1>new run</h1>
  {err}
  <form class="launch" method="post" action="/run">
    <label>task <select name="task">{opts(tasks)}</select></label>
    <label>orchestrator <select name="orchestrator">{opts(orchestrators)}</select></label>
    <label>worker <select name="worker">{opts(workers)}</select></label>
    <label>judge (optional) <select name="judge"><option value="">none</option>{opts(judges)}</select></label>
    <label>replicates <input name="replicates" type="number" value="1" min="1" /></label>
    <label>seed (optional) <input name="seed" type="number" /></label>
    <label><input name="dry_run" type="checkbox" value="1" /> dry run (no API calls)</label>
    <p><button class="primary" type="submit">launch</button></p>
  </form>
"""
    return page("new run", body, "new run")


def render_not_found(what: str) -> str:
    return page("not found", f"<h1>not found</h1><p class='muted'>{esc(what)}</p>")


def render_bad_request(msg: str) -> str:
    return page("bad request", f"<h1>bad request</h1><p class='err' style='padding:0.5rem'>{esc(msg)}</p>")
