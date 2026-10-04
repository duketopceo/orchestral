import * as F from "../format.js";
import { $view } from "../dom.js";
import { data, optional } from "../data.js";
import { RUN_HEAD, runRow } from "../chips.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { NIL, esc, fmtMoney, fmtPct, fmtScore, nilOr, slug } from "../util.js";
import { liveGlyph, stateHtml } from "../components/states.js";
import { experimentBlock } from "./experiment.js";

export async function viewOverview() {
  const [ov, mx, exp] = await Promise.all([
    data.overview(), data.matrix(),
    optional(data.experiment()),
  ]);
  await loadFlags();
  const live = (ov.jobs || []).filter(j => j.status === "running");
  const groups = ov.groups || [];
  const tax = ov.taxonomy || {};
  const taxMax = Math.max(1, ...Object.values(tax));
  $view.innerHTML = `
    <h1>Overview</h1>
    <p class="page-sub">Live experiment observatory. Mechanical verdicts and judge scores are separate axes.</p>

    ${live.length ? `<div class="live-strip">${live.map(j => `
      <div class="live-card">${liveGlyph({ stalled: j.stalled })}
        <span class="lc-label">${esc(j.label)}</span>
        <span class="dim">${esc(j.stalled ? "stalled" : (j.detail || ""))}${j.owned ? "" : " · unowned"}</span>
      </div>`).join("")}</div>` : ""}

    <h2>Groups</h2>
    <div class="group-grid">${groups.map(g => {
      const pass = g.pass_rate, fail = g.finished ? (1 - (pass ?? 0)) : 0;
      const rest = g.runs - (g.finished || 0);
      return `<a class="panel group-card" href="#/runs?group=${encodeURIComponent(g.group)}">
        <div class="split"><span class="gc-name" title="${esc(g.label || g.group)}">${esc(g.label || slug(g.group))}</span>
          <span class="dim">${g.runs} runs ${flagWidget("group", g.group)}</span></div>
        ${g.label ? `<div class="dim sm">${esc(g.description || g.group)}</div>` : ""}
        <div class="gc-stats">
          <span>Pass <b>${nilOr(fmtPct(pass))}</b></span>
          <span>Judge <b class="judge-axis">${nilOr(fmtScore(g.judge_score_median))}</b></span>
          <span>Cost <b>${nilOr(fmtMoney(g.cost_usd))}</b></span>
        </div>
        <div class="gc-bar">
          <i class="b-pass" style="width:${(pass ?? 0) * 100}%"></i>
          <i class="b-fail" style="width:${fail * 100 * (g.finished ? 1 : 0) / Math.max(g.finished, 1) * (g.finished / Math.max(g.runs, 1)) * 100 / 100}%"></i>
          <i class="b-rest" style="width:${(rest / Math.max(g.runs, 1)) * 100}%"></i>
        </div></a>`;
    }).join("") || stateHtml("empty", { title: "No run groups yet", body: "Groups appear once runs share a group name.", action: { label: "Open Runs", href: "#/runs" } })}</div>

    ${exp && (exp.cells || []).length ? experimentBlock(exp) : ""}

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
