/* Shared chart frame: every SVG chart is role="img" with a <title>, a <desc>
   holding the key numbers, and a "View as table" toggle (DESIGN.md 6.9 #10).
   The toggle is a native <details>, so it works without script and is
   keyboard operable. */
import { esc } from "../util.js";

export function dataTable({ caption, head, rows, keys = [] }) {
  return `<table class="data chart-data"><caption class="sr-only">${esc(caption)}</caption>
    <thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((cells, i) =>
      `<tr${keys[i] != null ? ` data-key="${esc(keys[i])}"` : ""}>${cells.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody>
  </table>`;
}

export function svgFrame({ id, width, height, title, desc, layout = "", body, cls = "ch-svg" }) {
  const w = Math.round(width), h = Math.round(height);
  return `<svg class="${cls}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
    aria-labelledby="${id}-title ${id}-desc"${layout ? ` data-layout="${layout}"` : ""}>
    <title id="${id}-title">${esc(title)}</title>
    <desc id="${id}-desc">${esc(desc)}</desc>${body}</svg>`;
}

export function chartFigure({ id, cls = "", svg, visual = svg, table }) {
  return `<figure class="chart ${cls}" id="${id}">${visual}
    <details class="chart-table"><summary>View as table</summary>${table}</details></figure>`;
}

// Hatch (low n): the sprite's #hatch pattern at reduced opacity, never a hue.
export function hatchRect(x, y, w, h) {
  return `<rect class="ch-hatch" x="${x}" y="${y}" width="${w}" height="${h}" fill="url(#hatch)"/>`;
}
