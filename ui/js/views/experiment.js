import * as F from "../format.js";
import { $view } from "../dom.js";
import { data, optional } from "../data.js";
import { stateHtml } from "../components/states.js";
import { NIL, esc, fmtMoney, fmtPct, slug } from "../util.js";

const stateChip = s => ({
  done: "chip-pass", partial: "chip-warn", aborted: "chip-fail",
  pending: "chip-dim", skipped: "chip-dim",
}[s] || "chip-dim");
const armCell = a => a && a.n
  ? `${a.passes}/${a.n} <span class="dim sm">${fmtPct(a.rate)}${a.ci ? ` [${F.rangePct(a.ci[0], a.ci[1])}]` : ""}</span>`
  : `<span class="dim">·</span>`;

/* The A/B ledger. Overview still shows it until U10 moves the content off Now. */
export function experimentBlock(exp) {
  return `<h2>Experiment: ${esc(exp.matrix)}</h2>
    <p class="page-sub">Baseline vs jev-assist, paired replicates. Primary axis: ${esc(exp.primary_axis)}.
    ${esc((exp.caveats || [])[0] || "")}</p>
    <div class="m">${Object.entries((exp.summary || {}).states || {}).map(([k, n]) =>
      `<span class="chip ${stateChip(k)}">${esc(k)} ${n}</span>`).join("")}
      <span class="chip chip-dim">Posted ${(exp.summary || {}).posted || 0}</span>
      <span class="chip chip-dim">Spend ${fmtMoney((exp.summary || {}).spend)}</span>
    </div>
    <div class="panel"><table class="data"><tr>
      <th>Cell</th><th class="t-num">Baseline</th><th class="t-num">Jev</th>
      <th class="t-num">Diff CI</th><th>Verdict</th><th class="t-num" data-pri="3">Target</th><th>State</th><th data-pri="3">Posted</th>
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
    </tr>`).join("") + `</tbody></table></div>`;
}

export async function viewExperiment() {
  const exp = await optional(data.experiment());
  if (!exp || !(exp.cells || []).length) {
    $view.innerHTML = `<h1>Experiments</h1>` + stateHtml("empty", {
      title: "No experiment matrix yet",
      body: "The A/B ledger fills in once a baseline-versus-assist matrix has run.",
      action: { label: "See the runs", href: "#/runs" } });
    return;
  }
  $view.innerHTML = `<h1>Experiments</h1>${experimentBlock(exp)}`;
}
