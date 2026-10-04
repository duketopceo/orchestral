/* Lane strip plot: the Pairings signature chart (DESIGN.md 6.9 #6).
   One staff lane per pairing, rows in the order given (the active lens).
   Left panel: pass-rate interval on a shared 0-100% axis. Right panel: cost
   per pass on a log axis ($0.001, $0.01, $0.1 ...). Mechanical pass is a hue,
   cost is ink only, and the two never share a channel (6.9 #1).
   Sized in CSS pixels from `width`; below STACK_AT the cost panel stacks
   under the pass panel and each row label sits above its row. */
import * as F from "../format.js";
import { esc } from "../util.js";
import { linear, logScale, logTicks, moneyTick, px } from "./scale.js";
import { chartFigure, dataTable, svgFrame } from "./frame.js";
import { intervalMark } from "./interval.js";
import { ofText, pairingName } from "./text.js";

export const STACK_AT = 640;
const LABEL_W = 188, GAP = 20, PAD_R = 12, PAD_L = 8;
const HEAD_H = 46, WIDE_ROW = 40, STACK_ROW = 56, FOOT = 8;

function costState(row) {
  if (row.cost_per_pass != null) return "";
  if (!((row.cost_total || 0) > 0)) return "unmetered";
  return "no passes";
}

function ariaFor(row) {
  const cost = row.cost_per_pass != null ? `${F.money(row.cost_per_pass)} per pass` : costState(row);
  const low = F.lowNBest(row.finished) ? ", low n" : "";
  return `${pairingName(row)}: ${ofText(row.passed, row.finished, row.pass_rate)}, ${cost}${low}. Opens the pairing.`;
}

function describe(rows, lensLabel) {
  const lead = rows[0];
  const low = rows.filter(r => F.lowNBest(r.finished)).length;
  const leadText = lead
    ? ` Leading pairing ${pairingName(lead)}: ${ofText(lead.passed, lead.finished, lead.pass_rate)}${lead.cost_per_pass != null ? `, ${F.money(lead.cost_per_pass)} per pass` : ""}.`
    : "";
  return `Lane strip plot, ${lensLabel}: ${rows.length} pairings, one lane each, pass rate with 95% interval and cost per pass.${leadText}${low ? ` ${low} of ${rows.length} pairings have low n.` : ""}`;
}

function tableFor(rows, lensLabel) {
  return dataTable({
    caption: `${lensLabel}: pass rate and cost per pass by pairing`,
    head: ["Pairing", "Runs", "Pass rate", "95% interval", "Cost per pass"],
    keys: rows.map(r => r.key),
    rows: rows.map(r => [
      `${F.shortSlug(r.orchestrator)} to ${F.shortSlug(r.worker)}`,
      ofText(r.passed, r.finished, r.pass_rate),
      F.percent(r.pass_rate),
      r.pass_ci ? F.rangePct(r.pass_ci[0], r.pass_ci[1]) : F.NULL_GLYPH,
      r.cost_per_pass != null ? F.money(r.cost_per_pass) : (costState(r) || F.NULL_GLYPH),
    ]),
  });
}

export function laneStrip({ id, rows, width, lensLabel = "Best overall", selected = "" }) {
  const stacked = width < STACK_AT;
  const W = Math.round(width);
  const metered = rows.filter(r => r.cost_per_pass != null);
  const rowH = stacked ? STACK_ROW : WIDE_ROW;
  const ticksPass = [0, 0.5, 1];
  const costTicks = metered.length
    ? logTicks(Math.min(...metered.map(r => r.cost_per_pass)), Math.max(...metered.map(r => r.cost_per_pass)))
    : [];

  // geometry: panels share a row band; stacked puts the cost panel below
  let passX0, passX1, costX0, costX1, labelX, passTop, costTop, H;
  if (stacked) {
    labelX = PAD_L; passX0 = PAD_L + 6; passX1 = W - PAD_R - 6; costX0 = passX0; costX1 = passX1;
    passTop = 0;
    costTop = HEAD_H + rows.length * rowH + FOOT;
    H = costTop + HEAD_H + rows.length * rowH + FOOT;
  } else {
    labelX = PAD_L;
    const area = W - LABEL_W - PAD_R - GAP;
    const pw = Math.max(80, Math.floor(area / 2) - 12);
    passX0 = LABEL_W + 6; passX1 = passX0 + pw;
    costX0 = passX1 + GAP + 12; costX1 = W - PAD_R - 6;
    passTop = costTop = 0;
    H = HEAD_H + rows.length * rowH + FOOT;
  }
  const xPass = linear(0, 1, passX0, passX1);
  const xCost = costTicks.length ? logScale(costTicks[0], costTicks[costTicks.length - 1], costX0, costX1) : null;

  const panelHead = (top, x0, x1, title, ticks, pos, label) => `
    <text class="ch-panel" x="${px(x0)}" y="${px(top + 14)}">${esc(title)}</text>
    ${ticks.map(t => `<text class="ch-tick" x="${px(pos(t))}" y="${px(top + 36)}" text-anchor="${t === ticks[0] ? "start" : t === ticks[ticks.length - 1] ? "end" : "middle"}">${esc(label(t))}</text>
    <line class="ch-grid" x1="${px(pos(t))}" y1="${px(top + HEAD_H - 6)}" x2="${px(pos(t))}" y2="${px(top + HEAD_H + rows.length * rowH)}"/>`).join("")}`;

  const valueText = row => {
    const low = F.lowNBest(row.finished) ? " | low n" : "";
    return `${ofText(row.passed, row.finished, row.pass_rate)}${low}`;
  };
  const passMark = (row, cy) => row.pass_rate == null
    ? `<text class="ch-note" x="${px(passX0)}" y="${px(cy + 4)}">${esc(F.NULL_GLYPH)}</text>`
    : intervalMark({ x: xPass, y: cy, rate: row.pass_rate, ci: row.pass_ci, lowN: F.lowNBest(row.finished) });
  const costMark = (row, cy, ty) => {
    if (xCost && row.cost_per_pass != null) {
      return `<circle class="ch-cost-pt" cx="${px(xCost(row.cost_per_pass))}" cy="${px(cy)}" r="4"/>
        <text class="ch-value" x="${px(costX0)}" y="${px(ty)}">${esc(F.money(row.cost_per_pass))} per pass</text>`;
    }
    return `<text class="ch-note" x="${px(costX0)}" y="${px(cy + 4)}">${esc(costState(row) || F.NULL_GLYPH)}</text>`;
  };

  let body = "";
  if (stacked) {
    body += panelHead(passTop, passX0, passX1, "Pass rate, 95% interval", ticksPass, xPass, t => F.percent(t));
    body += costTicks.length
      ? panelHead(costTop, costX0, costX1, "Cost per pass, log scale", costTicks, xCost, moneyTick)
      : `<text class="ch-panel" x="${px(costX0)}" y="${px(costTop + 14)}">Cost per pass, log scale</text>
         <text class="ch-note" x="${px(costX0)}" y="${px(costTop + 36)}">No pairing is metered, so no cost axis.</text>`;
  } else {
    body += panelHead(0, passX0, passX1, "Pass rate, 95% interval", ticksPass, xPass, t => F.percent(t));
    body += costTicks.length
      ? panelHead(0, costX0, costX1, "Cost per pass, log scale", costTicks, xCost, moneyTick)
      : `<text class="ch-panel" x="${px(costX0)}" y="${px(14)}">Cost per pass, log scale</text>
         <text class="ch-note" x="${px(costX0)}" y="${px(36)}">No pairing is metered, so no cost axis.</text>`;
  }

  rows.forEach((row, i) => {
    const sel = row.key === selected ? " ch-selected" : "";
    const attrs = (tab, first = false) =>
      `class="ch-mark" data-key="${esc(row.key)}" href="${esc(row.href)}" tabindex="${tab}" aria-label="${esc(ariaFor(row))}"${tab === "0" ? "" : ' aria-hidden="true"'}`;
    const l1 = esc(F.shortSlug(row.orchestrator, stacked ? 22 : 26));
    const l2 = esc(F.shortSlug(row.worker, stacked ? 22 : 26));
    if (!stacked) {
      const top = HEAD_H + i * rowH, cy = top + 14;
      body += `<a ${attrs("0")}><g class="ch-row${sel}">
        <rect class="ch-row-bg" x="0.5" y="${px(top)}" width="${W - 1}" height="${rowH}"/>
        <text class="ch-label" x="${labelX}" y="${px(top + 16)}">${l1}</text>
        <text class="ch-label ch-label-2" x="${labelX}" y="${px(top + 31)}">${esc("→")} ${l2}</text>
        ${passMark(row, cy)}
        <text class="ch-value" x="${px(passX0)}" y="${px(top + 33)}">${esc(valueText(row))}</text>
        ${costMark(row, cy, top + 33)}
      </g></a>`;
    } else {
      for (const [panel, tabv] of [["pass", "0"], ["cost", "-1"]]) {
        const top = (panel === "pass" ? passTop : costTop) + HEAD_H + i * rowH;
        const cy = top + 31;
        body += `<a ${attrs(tabv)}><g class="ch-row${sel}">
          <rect class="ch-row-bg" x="0.5" y="${px(top)}" width="${W - 1}" height="${rowH}"/>
          <text class="ch-label" x="${labelX}" y="${px(top + 14)}">${l1} ${esc("→")} ${l2}</text>
          ${panel === "pass"
            ? `${passMark(row, cy)}<text class="ch-value" x="${px(passX0)}" y="${px(top + 51)}">${esc(valueText(row))}</text>`
            : costMark(row, cy, top + 51)}
        </g></a>`;
      }
    }
  });

  const svg = svgFrame({
    id, width: W, height: H, layout: stacked ? "stacked" : "wide",
    title: `Lane strip plot: ${lensLabel}`,
    desc: describe(rows, lensLabel), body,
  });
  return chartFigure({ id, cls: "chart-strip", svg, table: tableFor(rows, lensLabel) });
}
