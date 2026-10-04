/* The shell: sprite, capability pruning, nav state, the More sheet, the
   palette mount and the polite live region. Everything here wraps markup that
   already exists in app.html, so the page is usable before any of it runs. */
import { can } from "./data.js";
import { closeDialog, initDialog, openDialog } from "./components/dialog.js";
import { initPalette, openPalette } from "./palette.js";

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

/* The palette lives in palette.js; the shell only opens it. */
export { openPalette };

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

