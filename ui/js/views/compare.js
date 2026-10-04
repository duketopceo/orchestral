import * as F from "../format.js";
import { $view } from "../dom.js";
import { api } from "../api.js";
import { NIL, esc, fmtMoney, fmtPct, slug } from "../util.js";

export async function viewCompare(params) {
  const groups = await api("/api/groups");
  const a = params.get("a") || (groups[0] && groups[0].group) || "";
  const b = params.get("b") || (groups[1] && groups[1].group) || "";

  $view.innerHTML = `
    <h1>Compare</h1>
    <p class="page-sub">Cell-by-cell delta between two run groups, by task, orchestrator and worker.</p>
    <div class="compare-controls">
      <label class="f">Baseline<select id="cmp-a">${groups.map(g =>
        `<option ${g.group === a ? "selected" : ""}>${esc(g.group)}</option>`).join("")}</select></label>
      <span class="dim" style="padding-bottom:8px">→</span>
      <label class="f">Candidate<select id="cmp-b">${groups.map(g =>
        `<option ${g.group === b ? "selected" : ""}>${esc(g.group)}</option>`).join("")}</select></label>
      <button class="primary" id="cmp-go" style="margin-bottom:1px">Compare</button>
    </div>
    <div id="cmp-out"></div>`;

  document.getElementById("cmp-go").addEventListener("click", () => {
    const av = document.getElementById("cmp-a").value, bv = document.getElementById("cmp-b").value;
    location.hash = `#/compare?a=${encodeURIComponent(av)}&b=${encodeURIComponent(bv)}`;
  });
  if (a && b) await renderCompare(a, b);
}

async function renderCompare(a, b) {
  const out = document.getElementById("cmp-out");
  if (!out) return;
  out.innerHTML = `<div class="loading">Comparing…</div>`;
  const d = await api(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
  const v = d.verdicts || {};
  const chipFor = x => ({ improved: "chip-pass", regressed: "chip-fail", stable: "chip-dim", "one-sided": "chip-warn" }[x]);
  const verdictLabel = x => ({ improved: "Improved", regressed: "Regressed", stable: "Stable", "one-sided": "One-sided" }[x] || x);

  out.innerHTML = `
    <div class="m">
      ${["improved", "regressed", "stable", "one-sided"].map(k =>
        `<span class="chip ${chipFor(k)}">${verdictLabel(k)} ${v[k] || 0}</span>`).join("")}
      <span class="chip chip-dim">${esc(a)} ${fmtMoney(d.cost_a)}</span>
      <span class="chip chip-dim">${esc(b)} ${fmtMoney(d.cost_b)}</span>
    </div>
    <div class="panel"><table class="data"><tr>
      <th>Task</th><th>Orchestrator → Worker</th><th class="t-num">Runs</th>
      <th class="t-num">${esc(a)}</th><th class="t-num">${esc(b)}</th><th class="t-num">Δ</th><th>Verdict</th>
    </tr><tbody>` +
    (d.cells || []).map(c => {
      const delta = c.verdict === "one-sided" ? NIL : F.delta(c.pass_b - c.pass_a, "pp");
      return `<tr>
        <td><a href="#/runs?task=${encodeURIComponent(c.task_id)}">${esc(c.task_id)}</a></td>
        <td class="mono">${esc(slug(c.orchestrator))} <span class="dim">→</span> ${esc(slug(c.worker))}</td>
        <td class="t-num">${c.n_a}/${c.n_b}</td>
        <td class="t-num">${fmtPct(c.pass_a)}</td>
        <td class="t-num">${fmtPct(c.pass_b)}</td>
        <td class="t-num cell-delta">${delta}</td>
        <td><span class="chip ${chipFor(c.verdict)}">${verdictLabel(c.verdict)}</span></td>
      </tr>`;
    }).join("") + `</tbody></table></div>`;
}
