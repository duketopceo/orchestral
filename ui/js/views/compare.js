import * as F from "../format.js";
import { $view } from "../dom.js";
import { data } from "../data.js";
import { esc, slug } from "../util.js";
import { icon } from "../components/states.js";
import { chartFigure, dataTable, dumbbell } from "../charts/index.js";

const MAX_ROWS = 300;
const CHIP = { improved: "chip-pass", regressed: "chip-fail", "no-clear-difference": "chip-dim", "one-sided": "chip-warn" };
const LABEL = { improved: "Improved", regressed: "Regressed", "no-clear-difference": "No clear difference", "one-sided": "One-sided" };
// Shapes, not hues, carry the verdict (grayscale-safe): check, box with slash, open circle, half-filled box. The open circle (not-judged glyph) marks "no clear difference": a hollow shape reads as "nothing established" next to the filled check and slashed box.
const GLYPH = { improved: "pass", regressed: "fail", "no-clear-difference": "not-judged", "one-sided": "inconclusive" };
const VERDICTS = ["regressed", "improved", "no-clear-difference", "one-sided"];

export async function viewCompare(params) {
  const groups = await data.groups();
  const a = params.get("a") || (groups[0] && groups[0].group) || "";
  const b = params.get("b") || (groups[1] && groups[1].group) || "";
  const options = sel => groups.map(g =>
    `<option ${g.group === sel ? "selected" : ""}>${esc(g.group)}</option>`).join("");

  $view.innerHTML = `
    <h1>Compare</h1>
    <p class="page-sub">Baseline first: what changed for each task and pairing between two run groups. Each row shows both pass-rate intervals.</p>
    <div class="compare-controls">
      <label class="f">Baseline<select id="cmp-a">${options(a)}</select></label>
      <span class="dim" style="padding-bottom:8px">→</span>
      <label class="f">Candidate<select id="cmp-b">${options(b)}</select></label>
      <button class="primary" id="cmp-go" style="margin-bottom:1px">Compare</button>
    </div>
    <div id="cmp-out"></div>`;

  document.getElementById("cmp-go").addEventListener("click", () => {
    const av = document.getElementById("cmp-a").value, bv = document.getElementById("cmp-b").value;
    location.hash = `#/compare?a=${encodeURIComponent(av)}&b=${encodeURIComponent(bv)}`;
  });
  if (a && b) await renderCompare(a, b);
}

function blocked(out, message) {
  out.innerHTML = `<p class="form-error" role="alert">${esc(message)}</p>`;
}

async function renderCompare(a, b) {
  const out = document.getElementById("cmp-out");
  if (!out) return;
  if (a === b) return blocked(out, "Pick two different run groups to compare.");
  out.innerHTML = `<div class="loading">Comparing…</div>`;
  const d = await data.compare(a, b);
  if (d.blocked) return blocked(out, d.blocked);
  const v = d.verdicts || {};
  const cells = d.cells || [];
  const shown = cells.slice(0, MAX_ROWS);
  const side = c => c.side === "baseline" ? "Baseline only" : c.side === "candidate" ? "Candidate only" : "No finished runs";
  const costLine = d.cost_delta == null ? "" :
    `Spend: baseline ${F.money(d.cost_a)}, candidate ${F.money(d.cost_b)}, change ${F.delta(d.cost_delta, "money")}.`;

  const rowHtml = (c, i) => {
    const two = c.verdict !== "one-sided";
    const low = c.low_n && two;
    const noise = c.verdict === "no-clear-difference";
    return `<li class="cmp-row" data-verdict="${c.verdict}" data-delta="${c.delta ?? ""}">
      <div class="cmp-id"><a href="#/runs?task=${encodeURIComponent(c.task_id)}">${esc(c.task_id)}</a>
        <span class="mono dim">${esc(slug(c.orchestrator))} → ${esc(slug(c.worker))}</span></div>
      <div class="cmp-db">${dumbbell({ id: `cmp-${i}`,
        a: { passed: c.passed_a, finished: c.finished_a, ci: c.ci_a },
        b: { passed: c.passed_b, finished: c.finished_b, ci: c.ci_b } })}</div>
      <div class="cmp-nums">
        <span class="cell-delta" title="${c.delta_ci ? `bootstrap 95% CI ${F.delta(c.delta_ci[0], "pp")} to ${F.delta(c.delta_ci[1], "pp")}` : ""}">${two ? F.delta(c.delta, "pp") : esc(side(c))}</span>
        <span class="dim sm">${noise ? "within noise · " : ""}${two ? `cost ${F.delta(c.cost_delta, "money")}` : ""}${low ? " · low n" : ""}</span>
      </div>
      <span class="chip ${CHIP[c.verdict]}">${icon(GLYPH[c.verdict])}${LABEL[c.verdict]}</span>
    </li>`;
  };

  const table = dataTable({
    caption: `Pass rate by task and pairing, ${a} against ${b}`,
    head: ["Task", "Pairing", "Baseline", "Candidate", "Change", "Cost change", "Verdict"],
    rows: cells.map(c => [
      c.task_id, `${slug(c.orchestrator)} to ${slug(c.worker)}`,
      c.finished_a ? `${c.passed_a} of ${c.finished_a} (${F.percent(c.pass_a)})` : F.NULL_GLYPH,
      c.finished_b ? `${c.passed_b} of ${c.finished_b} (${F.percent(c.pass_b)})` : F.NULL_GLYPH,
      c.delta == null ? F.NULL_GLYPH : F.delta(c.delta, "pp"),
      c.cost_delta == null ? F.NULL_GLYPH : F.delta(c.cost_delta, "money"),
      LABEL[c.verdict]]),
  });

  let body;
  if (!cells.length) {
    body = `<div class="empty">Neither group has any cells to compare.</div>`;
  } else {
    const list = `<div class="cmp-head" aria-hidden="true"><span>Task and pairing</span><span>Baseline and candidate, 95% interval</span><span>Change</span><span>Verdict</span></div>
      <ol class="cmp-rows">${shown.map(rowHtml).join("")}</ol>
      ${cells.length > shown.length ? `<p class="chart-note">Showing the ${shown.length} largest regressions first of ${cells.length} cells. The table lists all of them.</p>` : ""}`;
    body = chartFigure({ id: "cmp-chart", cls: "chart-compare", visual: list, table });
  }

  out.innerHTML = `
    <div class="cmp-summary m">
      ${VERDICTS.map(k => `<span class="chip ${CHIP[k]}">${icon(GLYPH[k])}${LABEL[k]} ${v[k] || 0}</span>`).join("")}
      <span class="dim">${d.shared} shared ${d.shared === 1 ? "cell" : "cells"}, ${d.one_sided} one-sided.</span>
      <span class="dim">${esc(costLine)}</span>
    </div>
    ${d.shared === 0 && cells.length ? `<p class="chart-note">0 shared cells: these groups have no task and pairing in common, so nothing can be compared. The cells below ran on one side only.</p>` : ""}
    <div class="panel panel-pad">${body}</div>`;
}
