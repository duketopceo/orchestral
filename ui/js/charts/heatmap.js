/* Heatmap as a real table: one sequential single-hue ramp for mechanical pass
   rate (DESIGN.md 6.9 #4), the percentage printed in each cell, cells are
   <a> elements. Low n hatches over the fill, never-attempted cells are empty
   with a diagonal rule. It scrolls inside its own container with a sticky
   row-head column and a caption that names the scroll. A head may carry `href`
   (the label links) and `sub` (a second, dimmer line). A cell with `unrated`
   has runs but none finished: a linked dash, not "never attempted". */
import * as F from "../format.js";
import { esc } from "../util.js";
import { rampStep } from "./scale.js";

const head = (h, scope) => {
  const label = h.href ? `<a href="${esc(h.href)}">${esc(h.label)}</a>` : esc(h.label);
  return `<th scope="${scope}" class="hm-h" title="${esc(h.title || h.label)}">${label}${h.sub ? `<span class="hm-sub">${esc(h.sub)}</span>` : ""}</th>`;
};

export function heatmap({ id, caption, corner = "", rowHeads, colHeads, cells, max = 1 }) {
  const byKey = new Map(cells.map(c => [`${c.row}\u0000${c.col}`, c]));
  const heads = colHeads.map(c => head(c, "col")).join("");
  const rows = rowHeads.map(r => `<tr>${head(r, "row")}${colHeads.map(c => {
    const cell = byKey.get(`${r.key}\u0000${c.key}`);
    if (cell && cell.value == null && cell.unrated) {
      const label = `${cell.title || ""}`.trim();
      return `<td><a class="hm-cell ramp-0" href="${esc(cell.href)}" title="${esc(label)}" aria-label="${esc(label)}">${F.NULL_GLYPH}</a></td>`;
    }
    if (!cell || cell.value == null) {
      return `<td class="hm-none"><span class="hm-empty" role="img" aria-label="never attempted"></span></td>`;
    }
    const step = rampStep(cell.value, max);
    const thin = F.lowNCell(cell.n) ? " hm-thin" : "";
    const label = `${cell.title || ""}`.trim();
    return `<td><a class="hm-cell ramp-${step}${thin}${cell.selected ? " hm-selected" : ""}" href="${esc(cell.href)}" title="${esc(label)}" aria-label="${esc(label)}">${esc(F.percent(cell.value))}${thin ? '<small class="hm-ln">low n</small>' : ""}</a></td>`;
  }).join("")}</tr>`).join("");
  return `<div class="chart-heatmap" id="${id}">
    <p class="chart-caption" id="${id}-cap">${esc(caption)}</p>
    <div class="chart-scroll" role="region" tabindex="0" aria-labelledby="${id}-cap">
      <table class="hm"><thead><tr><th scope="col" class="hm-corner">${esc(corner)}</th>${heads}</tr></thead><tbody>${rows}</tbody></table>
    </div></div>`;
}
