import * as F from "../format.js";
import { $view } from "../dom.js";
import { api } from "../api.js";
import { RUN_HEAD, runRow } from "../chips.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { NIL, esc, fmtMoney, fmtPct, fmtScore, slug } from "../util.js";

export async function viewOverview() {
  const [ov, mx, exp] = await Promise.all([
    api("/api/overview"), api("/api/matrix"),
    api("/api/experiment").catch(() => null),
  ]);
  await loadFlags();
  const live = (ov.jobs || []).filter(j => j.status === "running");
  const groups = ov.groups || [];
  const tax = ov.taxonomy || {};
  const taxMax = Math.max(1, ...Object.values(tax));
  const stateChip = s => ({
    done: "chip-pass", partial: "chip-warn", aborted: "chip-fail",
    pending: "chip-dim", skipped: "chip-dim",
  }[s] || "chip-dim");
  const armCell = a => a && a.n
    ? `${a.passes}/${a.n} <span class="dim sm">${fmtPct(a.rate)}${a.ci ? ` [${F.rangePct(a.ci[0], a.ci[1])}]` : ""}</span>`
    : `<span class="dim">·</span>`;

  $view.innerHTML = `
    <h1>Overview</h1>
    <p class="page-sub">Live experiment observatory. Mechanical verdicts and judge scores are separate axes.</p>

    ${live.length ? `<div class="live-strip">${live.map(j => `
      <div class="live-card"><span class="dot dot-run pulse"></span>
        <span class="lc-label">${esc(j.label)}</span>
        <span class="dim">${esc(j.detail || "")}</span>
      </div>`).join("")}</div>` : ""}

    <h2>Groups</h2>
    <div class="group-grid">${groups.map(g => {
      const pass = g.pass_rate, fail = g.finished ? (1 - (pass ?? 0)) : 0;
      const rest = g.runs - (g.finished || 0);
      return `<a class="panel group-card" href="#/runs?group=${encodeURIComponent(g.group)}">
        <div class="split"><span class="gc-name">${esc(g.label || g.group)}</span>
          <span class="dim">${g.runs} runs ${flagWidget("group", g.group)}</span></div>
        ${g.label ? `<div class="dim sm">${esc(g.description || g.group)}</div>` : ""}
        <div class="gc-stats">
          <span>Pass <b>${fmtPct(pass)}</b></span>
          <span>Judge <b class="judge-axis">${fmtScore(g.judge_score_median)}</b></span>
          <span>Cost <b>${fmtMoney(g.cost_usd)}</b></span>
        </div>
        <div class="gc-bar">
          <i class="b-pass" style="width:${(pass ?? 0) * 100}%"></i>
          <i class="b-fail" style="width:${fail * 100 * (g.finished ? 1 : 0) / Math.max(g.finished, 1) * (g.finished / Math.max(g.runs, 1)) * 100 / 100}%"></i>
          <i class="b-rest" style="width:${(rest / Math.max(g.runs, 1)) * 100}%"></i>
        </div></a>`;
    }).join("") || `<div class="empty">No run groups yet</div>`}</div>

    ${exp && (exp.cells || []).length ? `<h2>Experiment: ${esc(exp.matrix)}</h2>
    <p class="page-sub">Baseline vs jev-assist, paired replicates. Primary axis: ${esc(exp.primary_axis)}.
    ${esc((exp.caveats || [])[0] || "")}</p>
    <div class="m">${Object.entries((exp.summary || {}).states || {}).map(([k, n]) =>
      `<span class="chip ${stateChip(k)}">${esc(k)} ${n}</span>`).join("")}
      <span class="chip chip-dim">Posted ${(exp.summary || {}).posted || 0}</span>
      <span class="chip chip-dim">Spend ${fmtMoney((exp.summary || {}).spend)}</span>
    </div>
    <div class="panel"><table class="data"><tr>
      <th>Cell</th><th class="t-num">Baseline</th><th class="t-num">Jev</th>
      <th class="t-num">Diff CI</th><th>Verdict</th><th class="t-num">Target</th><th>State</th><th>Posted</th>
    </tr><tbody>` +
    exp.cells.map(c => `<tr>
      <td>${esc(c.task)}<div class="dim sm">${esc(slug(c.orchestrator))} → ${esc(slug(c.worker))}${c.difficulty ? ` · ${esc(c.difficulty)}` : ""}${c.archetype ? ` ${esc(c.archetype)}` : ""}${c.jev && c.jev.interventions && (c.jev.interventions.replan + c.jev.interventions.rework) ? ` · jev intervened ${c.jev.interventions.replan + c.jev.interventions.rework}×` : ""}</div></td>
      <td class="t-num">${armCell(c.baseline)}</td>
      <td class="t-num">${armCell(c.jev)}</td>
      <td class="t-num">${c.diff_ci ? `[${c.diff_ci[0] >= 0 ? "+" : ""}${c.diff_ci[0].toFixed(2)}, ${c.diff_ci[1] >= 0 ? "+" : ""}${c.diff_ci[1].toFixed(2)}]` : NIL}</td>
      <td><span class="chip ${{ lift: "chip-pass", harm: "chip-fail", resolved: "chip-pass", inconclusive: "chip-warn" }[c.verdict] || "chip-dim"}">${esc(c.verdict)}</span></td>
      <td class="t-num">${c.target}</td>
      <td><span class="chip ${stateChip(c.state)}">${esc(c.state)}</span></td>
      <td>${c.posted ? `<span title="${esc(c.posted_note)}">posted</span>` : '<span class="dim">·</span>'}</td>
    </tr>`).join("") + `</tbody></table></div>` : ""}

    ${Object.keys(tax).length ? `<h2>Failure Taxonomy</h2>
    <div class="tax-list">${Object.entries(tax).map(([k, n]) => `
      <div class="tax-row"><span class="tx-name">${esc(k)}</span>
        <span class="tx-bar"><i style="width:${(n / taxMax) * 100}%"></i></span>
        <span class="tx-n">${n}</span></div>`).join("")}</div>` : ""}

    ${(mx.tasks || []).length ? `<h2>Tasks × Pairings</h2>
    <p class="page-sub">Mechanical pass rate per cell. Click a cell to drill into its runs. An empty cell means the pairing never attempted that task.</p>
    <div class="panel heat-wrap"><table class="data heat">
      <tr><th class="heat-task">Task</th>${mx.pairings.map(p =>
        `<th class="heat-col"><div>${esc(slug(p.split(" → ")[0]))}</div><div class="dim">→ ${esc(slug(p.split(" → ")[1] || ""))}</div></th>`).join("")}</tr>
      ${mx.tasks.map(t => `<tr>
        <th class="heat-task"><a href="#/runs?task=${encodeURIComponent(t.task_id)}">${esc(t.task_title || t.task_id)}</a>
          <div class="dim sm">${esc(t.task_id)}${t.task_type ? ` · ${esc(t.task_type)}` : ""}${t.difficulty ? ` · ${esc(t.difficulty)}` : ""}${t.archetype ? ` ${esc(t.archetype)}` : ""}</div></th>
        ${mx.pairings.map(p => {
          const c = t.cells[p];
          if (!c) return `<td class="heat-cell"><span class="dim">·</span></td>`;
          const v = c.pass_rate;
          const a = v == null ? 0.06 : 0.08 + 0.72 * v;
          const jm = c.judge_mean != null ? ` · judge ${fmtScore(c.judge_mean)}` : "";
          return `<td class="heat-cell${c.n < 3 ? " thin" : ""}" data-go="#/runs?task=${encodeURIComponent(t.task_id)}"
            title="${esc(t.task_id)} · ${esc(p)}: pass ${v == null ? F.NULL_GLYPH : fmtPct(v)} over ${c.n} run${c.n === 1 ? "" : "s"}${jm}${c.n < 3 ? " · Low n" : ""}">
            <span class="heat-fill" style="opacity:${a.toFixed(2)}">${v == null ? NIL : fmtPct(v)}</span>
          </td>`;
        }).join("")}</tr>`).join("")}
    </table></div>` : ""}

    <h2>Recent Runs</h2>
    <div class="panel"><table class="data">${RUN_HEAD}
      <tbody>${(ov.recent || []).map(runRow).join("") || `<tr><td colspan="9" class="empty">No runs</td></tr>`}</tbody>
    </table></div>`;
  for (const td of $view.querySelectorAll("td.heat-cell[data-go]")) {
    td.style.cursor = "pointer";
    td.addEventListener("click", () => { location.hash = td.dataset.go; });
  }
  bindFlags($view);
}
