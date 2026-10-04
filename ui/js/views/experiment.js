import * as F from "../format.js";
import { $view } from "../dom.js";
import { data, meta, optional } from "../data.js";
import { ApiError, isAbort } from "../api.js";
import { stateHtml } from "../components/states.js";
import { NIL, esc, fmtMoney, fmtPct, slug } from "../util.js";

/* Experiments: the A/B ledger on its own route. Cells are grouped by state with
   pending collapsed, each cell draws a per-arm interval dumbbell on a shared
   0 to 100% axis, and a spend gauge says "No budget recorded" when the driver
   did not record a budget. The ledger table carries the same numbers as text. */

const STATE_ORDER = ["done", "partial", "aborted", "pending"];
const STATE_LABEL = { done: "Done", partial: "Partial", aborted: "Aborted", pending: "Pending" };
const stateChip = s => ({
  done: "chip-pass", partial: "chip-warn", aborted: "chip-fail",
  pending: "chip-dim", skipped: "chip-dim",
}[s] || "chip-dim");

const armText = a => (a && a.n
  ? `${a.passes} of ${a.n} (${fmtPct(a.rate)})${a.ci ? `, interval ${F.rangePct(a.ci[0], a.ci[1])}` : ""}` : "no runs yet");
const armCell = a => a && a.n
  ? `${a.passes}/${a.n} <span class="dim sm">${fmtPct(a.rate)}${a.ci ? ` [${F.rangePct(a.ci[0], a.ci[1])}]` : ""}</span>`
  : `<span class="dim">·</span>`;

const W = 168, H = 28, PAD = 6;
const x = v => PAD + v * (W - 2 * PAD);

/* Two arms on one hairline axis: baseline is a ring, jev is a filled square, so
   the arms differ by shape and never by hue. A whisker is the Wilson interval;
   it is dashed (hatched) when the arm has fewer runs than the low-n threshold. */
export function dumbbell(cell, lowN) {
  const arms = [["baseline", cell.baseline], ["jev", cell.jev]].filter(([, a]) => a && a.n);
  const desc = `Baseline ${armText(cell.baseline)}. Jev ${armText(cell.jev)}.`;
  const title = `Pass rate by arm for ${cell.task}`;
  if (!arms.length) {
    return `<svg class="dumbbell" role="img" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><title>${esc(title)}</title><desc>${esc(desc)}</desc>
      <line class="db-axis" x1="${x(0)}" x2="${x(1)}" y1="${H / 2}" y2="${H / 2}"/></svg>`;
  }
  const ticks = [0, 0.5, 1].map(v => `<line class="db-tick" x1="${x(v)}" x2="${x(v)}" y1="${H / 2 - 9}" y2="${H / 2 + 9}"/>`).join("");
  const rates = arms.map(([, a]) => a.rate).filter(r => r != null);
  const link = rates.length === 2
    ? `<line class="db-link" x1="${x(rates[0])}" x2="${x(rates[1])}" y1="${H / 2}" y2="${H / 2}"/>` : "";
  const marks = arms.map(([name, a], i) => {
    const y = H / 2 + (i === 0 ? -5 : 5);
    const low = a.n < lowN;
    const whisker = a.ci ? `<line class="db-whisker${low ? " low-n" : ""}" x1="${x(a.ci[0])}" x2="${x(a.ci[1])}" y1="${y}" y2="${y}"/>` : "";
    const px = x(a.rate ?? 0);
    const point = name === "baseline"
      ? `<circle class="db-point db-baseline" cx="${px}" cy="${y}" r="3.5"/>`
      : `<rect class="db-point db-jev" x="${px - 3.5}" y="${y - 3.5}" width="7" height="7"/>`;
    return whisker + point;
  }).join("");
  return `<svg class="dumbbell" role="img" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><title>${esc(title)}</title><desc>${esc(desc)}</desc>
    <line class="db-axis" x1="${x(0)}" x2="${x(1)}" y1="${H / 2}" y2="${H / 2}"/>${ticks}${link}${marks}</svg>`;
}

const diffText = c => (c.diff_ci
  ? `[${c.diff_ci[0] >= 0 ? "+" : ""}${c.diff_ci[0].toFixed(2)}, ${c.diff_ci[1] >= 0 ? "+" : ""}${c.diff_ci[1].toFixed(2)}]` : NIL);

function cellRow(c, lowN) {
  const iv = c.jev && c.jev.interventions;
  const intervened = iv && (iv.replan + iv.rework) ? ` · jev intervened ${iv.replan + iv.rework} times` : "";
  return `<tr>
    <td>${esc(c.task)}<div class="dim sm">${esc(slug(c.orchestrator))} → ${esc(slug(c.worker))}${c.difficulty ? ` · ${esc(c.difficulty)}` : ""}${c.archetype ? ` ${esc(c.archetype)}` : ""}${intervened}</div></td>
    <td class="db-cell">${dumbbell(c, lowN)}</td>
    <td class="t-num">${armCell(c.baseline)}</td>
    <td class="t-num">${armCell(c.jev)}</td>
    <td class="t-num" data-pri="2">${diffText(c)}</td>
    <td><span class="chip ${{ lift: "chip-pass", harm: "chip-fail", resolved: "chip-pass", inconclusive: "chip-warn" }[c.verdict] || "chip-dim"}">${esc(c.verdict)}</span></td>
    <td class="t-num" data-pri="3">${c.target}</td>
    <td data-pri="3">${c.posted ? `<span title="${esc(c.posted_note)}">posted</span>` : '<span class="dim">·</span>'}</td>
  </tr>`;
}

const HEAD = `<thead><tr><th>Cell</th><th>Pass rate</th><th class="t-num">Baseline</th><th class="t-num">Jev</th>
  <th class="t-num" data-pri="2">Difference</th><th>Verdict</th><th class="t-num" data-pri="3">Target</th><th data-pri="3">Posted</th></tr></thead>`;

function ledger(exp, lowN) {
  const by = {};
  for (const c of exp.cells) (by[c.state] ||= []).push(c);
  const order = [...STATE_ORDER, ...Object.keys(by).filter(s => !STATE_ORDER.includes(s))];
  return order.filter(s => by[s]).map(s => `
    <details class="ledger-group" data-state="${esc(s)}"${s === "pending" ? "" : " open"}>
      <summary><span class="chip ${stateChip(s)}">${esc(STATE_LABEL[s] || s)}</span> <span class="dim">${by[s].length} cell${by[s].length === 1 ? "" : "s"}</span></summary>
      <div class="panel"><table class="data ledger">${HEAD}<tbody>${by[s].map(c => cellRow(c, lowN)).join("")}</tbody></table></div>
    </details>`).join("");
}

function spendGauge(exp) {
  const spend = (exp.summary || {}).spend || 0;
  const b = exp.budget || {};
  if (!b.recorded) {
    return `<div id="spend-gauge" class="gauge"><span class="gauge-label">Spend</span> <b>${fmtMoney(spend)}</b>
      <span class="dim">No budget recorded</span></div>`;
  }
  const share = Math.min(1, spend / b.usd);
  return `<div id="spend-gauge" class="gauge"><span class="gauge-label">Spend</span> <b>${fmtMoney(spend)}</b>
    <span class="dim">of ${fmtMoney(b.usd)} budget</span>
    <span class="gauge-track" role="img" aria-label="${esc(`${fmtMoney(spend)} spent of ${fmtMoney(b.usd)}`)}"><i class="gauge-fill" style="width:${(share * 100).toFixed(1)}%"></i></span></div>`;
}

const cap = t => (t ? t.charAt(0).toUpperCase() + t.slice(1) + (t.endsWith(".") ? "" : ".") : "");

const header = (extra = "") => `<h1>Experiments</h1>${extra}`;

function picker(list, current) {
  if (list.length < 2) return "";
  return `<nav class="exp-picker" aria-label="Experiment matrices">${list.map(e =>
    `<a href="#/experiment?matrix=${encodeURIComponent(e.name)}"${e.name === current ? ' aria-current="page"' : ""}>${esc(e.name)}</a>`).join("")}</nav>`;
}

function notFound(name, list) {
  return header() + stateHtml("missing", {
    title: `No experiment named ${name}`,
    body: list.length
      ? `This data set has ${list.map(e => esc(e.name)).join(", ")}.`
      : "No experiment matrices are in this data set.",
    action: list.length ? { label: "Open Experiments", href: "#/experiment" } : { label: "See the runs", href: "#/runs" } });
}

export async function viewExperiment(params = new URLSearchParams()) {
  const lowN = meta().low_n?.cell ?? 3;
  const asked = params.get("matrix") || "";
  const list = (await optional(data.experiments())) || [];
  if (!asked && !list.length) {
    $view.innerHTML = header() + stateHtml("empty", {
      title: "No experiment matrix yet",
      body: "The A/B ledger fills in once a baseline versus assist matrix has run.",
      action: { label: "See the runs", href: "#/runs" } });
    return;
  }
  const name = asked || list[0].name;
  if (list.length && !list.some(e => e.name === name)) { $view.innerHTML = notFound(name, list); return; }
  let exp;
  try {
    exp = await data.experiment(name);
  } catch (e) {
    if (isAbort(e)) throw e;
    if (e instanceof ApiError && e.status === 404) { $view.innerHTML = notFound(name, list); return; }
    throw e;
  }
  if (!exp || !(exp.cells || []).length) { $view.innerHTML = notFound(name, list); return; }
  const states = (exp.summary || {}).states || {};
  $view.innerHTML = header(`${picker(list, name)}
    <p class="page-sub">${esc(exp.matrix)}: baseline versus jev assist, paired replicates. Primary axis: ${esc(exp.primary_axis)}.
      ${esc(cap((exp.caveats || [])[0] || ""))}</p>
    <div class="ledger-chips">${Object.entries(states).map(([k, n]) => `<span class="chip ${stateChip(k)}">${esc(k)} ${n}</span>`).join("")}
      <span class="chip chip-dim">Posted ${(exp.summary || {}).posted || 0}</span></div>
    ${spendGauge(exp)}
    ${ledger(exp, lowN)}`);
}
