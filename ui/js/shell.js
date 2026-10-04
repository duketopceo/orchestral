/* The shell: sprite, capability pruning, nav state, the More sheet, the
   palette and the polite live region. Everything here wraps markup that
   already exists in app.html, so the page is usable before any of it runs. */
import { can } from "./data.js";
import { closeDialog, initDialog, openDialog } from "./components/dialog.js";
import { esc } from "./util.js";

/* The sprite is one same-origin file, fetched once and injected inline so every
   <use href="#id"> (and the #hatch pattern) resolves in the page's own document.
   It shares this module's ?v= token, so a new build is a new URL. */
async function loadSprite() {
  const url = new URL("/static/icons.svg", import.meta.url);
  url.search = new URL(import.meta.url).search;
  try {
    const r = await fetch(url);
    if (r.ok) document.body.insertAdjacentHTML("afterbegin", await r.text());
  } catch { /* icons are decoration; every glyph has text beside it */ }
}

/* Routes the viewer cannot use are removed from the rail, bar and sheet. */
export function pruneByCapability() {
  for (const el of document.querySelectorAll("[data-needs]")) {
    if (!can(el.dataset.needs)) el.remove();
  }
}

/* A detail route lights its parent section. Rail, bar and sheet share one rule. */
export function markNav(path) {
  const parent = path.startsWith("/run/") ? "/runs" : path === "/card" ? "/cards" : path;
  for (const a of document.querySelectorAll("a[data-route]")) {
    const on = a.dataset.route === "/" ? parent === "/" : parent.startsWith(a.dataset.route);
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

export function announce(text) {
  const el = document.getElementById("live-region");
  if (!el) return;
  el.textContent = "";
  requestAnimationFrame(() => { el.textContent = text; }); // clear first so a repeat is re-announced
}

/* ---------- palette ---------- */

/* U14 replaces `search` with the real index (runs, tasks, pairings, groups).
   Until then the palette jumps between views. */
const VIEWS = [
  ["Now", "#/", "o"], ["Runs", "#/runs", "r"], ["Pairings", "#/leaderboard", "p"],
  ["Compare", "#/compare", "c"], ["Experiments", "#/experiment", "e"],
  ["Publish", "#/cards"], ["Models", "#/models"], ["New run", "#/new", null, "launch"], ["Guide", "#/about"],
];
let search = q => VIEWS
  .filter(([label, , , need]) => (!need || can(need)) && label.toLowerCase().includes(q.toLowerCase()))
  .map(([label, href]) => ({ label, href }));

export function registerPaletteSearch(fn) { search = fn; }

function paletteBody() {
  return `<form method="dialog" class="palette-form" role="search">
    <label class="sr-only" for="palette-q">Jump to</label>
    <input id="palette-q" type="search" autocomplete="off" placeholder="Jump to a view, run, task or group" aria-controls="palette-list">
    <ul id="palette-list" class="palette-list" role="listbox" aria-label="Results"></ul>
  </form>`;
}

function renderResults(dlg) {
  const q = dlg.querySelector("#palette-q").value.trim();
  const rows = Promise.resolve(search(q)).then(rs => rs.slice(0, 50));
  rows.then(rs => {
    dlg.querySelector("#palette-list").innerHTML = rs.length
      ? rs.map((r, i) => `<li role="option" aria-selected="${i === 0}"><a href="${esc(r.href)}">${esc(r.label)}</a></li>`).join("")
      : `<li class="palette-none">Nothing matches. <a href="#/runs${q ? `?q=${encodeURIComponent(q)}` : ""}">Search Runs</a></li>`;
  });
}

function initPalette() {
  const dlg = document.getElementById("palette");
  dlg.innerHTML = paletteBody();
  initDialog(dlg);
  const input = dlg.querySelector("#palette-q");
  input.addEventListener("input", () => renderResults(dlg));
  input.addEventListener("keydown", e => {
    const opts = [...dlg.querySelectorAll("[role=option]")];
    const cur = opts.findIndex(o => o.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = opts.length ? (cur + (e.key === "ArrowDown" ? 1 : -1) + opts.length) % opts.length : 0;
      opts.forEach((o, i) => o.setAttribute("aria-selected", String(i === n)));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const a = (opts[cur] || opts[0])?.querySelector("a");
      if (a) { location.hash = a.getAttribute("href"); closeDialog(dlg); }
    }
  });
  dlg.addEventListener("close", () => { input.value = ""; });
}

export function openPalette(opener) {
  const dlg = document.getElementById("palette");
  closeDialog(document.getElementById("more-sheet"));
  renderResults(dlg);
  openDialog(dlg, opener);
  dlg.querySelector("#palette-q").focus();
}

/* ---------- boot ---------- */

export async function initShell() {
  await loadSprite();
  pruneByCapability();
  const more = document.getElementById("more-sheet");
  initDialog(more);
  initPalette();
  document.getElementById("more-open").addEventListener("click", e => openDialog(more, e.currentTarget));
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-open-palette]");
    if (b) openPalette(b.closest("#more-sheet") ? document.getElementById("more-open") : b);
  });
  // leaving the narrow layout while the sheet is open would strand a hidden modal
  matchMedia("(min-width: 641px)").addEventListener("change", e => { if (e.matches) closeDialog(more); });
}

