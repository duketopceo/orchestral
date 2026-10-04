/* Global keys, one table. The key map dialog is generated from the same table
   that binds them, so the two cannot drift. Keys are ignored while typing in a
   field and while a dialog is open (Escape and the dialog's own keys apply). */
import { initDialog, openDialog } from "./components/dialog.js";
import { openPalette } from "./shell.js";
import { esc } from "./util.js";

export const GO = {
  o: ["Now", "#/"], r: ["Runs", "#/runs"], p: ["Pairings", "#/leaderboard"],
  c: ["Compare", "#/compare"], e: ["Experiments", "#/experiment"],
};
const OTHER = [
  ["Ctrl K or Cmd K", "Open search"],
  ["/", "Focus the filter on this page"],
  ["?", "Show this key map"],
  ["Esc", "Close a dialog; focus returns where it was"],
];
const SEQ_MS = 1200;

const typing = t => !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
const modalOpen = () => !!document.querySelector("dialog[open]");

export function keymapHtml() {
  const rows = [
    ...Object.entries(GO).map(([k, [label]]) => [`g ${k}`, `Go to ${label}`]),
    ...OTHER,
  ];
  return `<div class="sheet-head"><h2 class="sheet-title">Keyboard shortcuts</h2>
    <button type="button" class="icon-btn" data-close aria-label="Close"><svg class="i" aria-hidden="true"><use href="#i-close"/></svg></button></div>
    <dl class="keymap">${rows.map(([k, d]) => `<div><dt><kbd>${esc(k)}</kbd></dt><dd>${esc(d)}</dd></div>`).join("")}</dl>`;
}

export function openKeymap(opener) {
  openDialog(document.getElementById("keymap"), opener);
}

function focusFilter() {
  const f = document.querySelector("#view input[type=search], #view input[type=text]");
  if (!f) return false;
  f.focus();
  return true;
}

export function initKeys() {
  const km = document.getElementById("keymap");
  km.innerHTML = keymapHtml();
  initDialog(km);
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-open-keymap]");
    if (b) openKeymap(b);
  });

  let pending = 0;
  document.addEventListener("keydown", e => {
    if (e.defaultPrevented || e.altKey) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (!document.getElementById("palette").open) openPalette(document.activeElement);
      return;
    }
    if (e.ctrlKey || e.metaKey || typing(e.target) || modalOpen()) return;
    if (pending && Date.now() - pending < SEQ_MS) {
      pending = 0;
      const to = GO[e.key.toLowerCase()];
      if (to) { e.preventDefault(); location.hash = to[1]; }
      return;
    }
    pending = 0;
    if (e.key === "g") { pending = Date.now(); return; }
    if (e.key === "?") { e.preventDefault(); openKeymap(document.activeElement); return; }
    if (e.key === "/" && focusFilter()) e.preventDefault();
  });
}
