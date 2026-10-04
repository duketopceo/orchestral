/* Data tables. Views keep emitting plain `table.data` markup; enhanceTables()
   gives each one the shared behaviour after every render:
   - an overflow container, so a wide table scrolls inside itself and never
     widens the page (the 390px fix);
   - a sticky head row and sticky first column;
   - column priority: a `data-pri="2"` or `"3"` on a <th> is copied to its
     column's cells; priority 3 columns hide at 640px and below, and a row
     expand button reveals their values under the row;
   - density: 32px rows by default, 28px compact (`data-density="compact"`),
     40px on touch layouts. */
import { esc } from "../util.js";

const NARROW = "(max-width: 640px)";

function headRow(table) {
  const row = table.querySelector("tr");
  return row && [...row.children].every(c => c.tagName === "TH") && !row.classList.contains("cat-group") ? row : null;
}

function bodyRows(table) {
  return [...table.querySelectorAll("tr")].filter(r =>
    !r.classList.contains("thead-row") && !r.classList.contains("cat-group") &&
    !r.classList.contains("row-detail") && [...r.children].every(c => c.tagName === "TD" || c.tagName === "TH"));
}

function mark(table) {
  const head = headRow(table);
  if (!head) return null;
  head.classList.add("thead-row");
  const pri = [...head.children].map(th => th.dataset.pri || "");
  if (pri.some(Boolean)) {
    for (const r of bodyRows(table)) {
      if (r.children.length !== pri.length) continue;
      [...r.children].forEach((c, i) => { if (pri[i]) c.dataset.pri = pri[i]; });
    }
  }
  return { head, pri };
}

function addExpanders(table, head) {
  const heads = [...head.children].map(th => th.textContent.trim());
  const hasHidden = [...head.children].some(th => th.dataset.pri === "3");
  if (!hasHidden) return;
  for (const r of bodyRows(table)) {
    if (r.children.length !== heads.length || r.querySelector(":scope > td:first-child > .row-expand")) continue;
    const first = r.children[0];
    if (first.tagName !== "TD") continue;
    first.insertAdjacentHTML("afterbegin",
      `<button type="button" class="row-expand" aria-expanded="false" aria-label="Show more columns"><svg class="i" aria-hidden="true" focusable="false"><use href="#i-chevron"/></svg></button>`);
  }
  if (table.dataset.expandBound) return;
  table.dataset.expandBound = "1";
  table.addEventListener("click", e => {
    const btn = e.target.closest(".row-expand");
    if (!btn) return;
    const row = btn.closest("tr");
    const open = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!open));
    if (open) { row.nextElementSibling?.classList.contains("row-detail") && row.nextElementSibling.remove(); return; }
    const items = [...row.children].map((c, i) => [heads[i], c])
      .filter(([, c]) => c.dataset.pri === "3")
      .map(([h, c]) => `<div><dt>${esc(h || "Details")}</dt><dd>${c.innerHTML}</dd></div>`).join("");
    row.insertAdjacentHTML("afterend",
      `<tr class="row-detail"><td colspan="${heads.length}"><dl class="detail-list">${items}</dl></td></tr>`);
  });
}

export function enhanceTables(root = document) {
  for (const table of root.querySelectorAll("table.data")) {
    if (!table.parentElement?.classList.contains("tbl-wrap")) {
      const wrap = document.createElement("div");
      wrap.className = "tbl-wrap";
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    }
    const m = mark(table);
    if (m) addExpanders(table, m.head);
  }
}

/* Re-run after any change to #view, including rows a view streams in later. */
export function watchTables(view) {
  let queued = false;
  const run = () => { queued = false; enhanceTables(view); };
  new MutationObserver(() => { if (!queued) { queued = true; queueMicrotask(run); } })
    .observe(view, { childList: true, subtree: true });
  enhanceTables(view);
}

export const isNarrow = () => window.matchMedia(NARROW).matches;
