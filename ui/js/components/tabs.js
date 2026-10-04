/* Tabs: underline tablist with the roving-tabindex pattern. Tabs may carry a
   count ("Events 214"); an empty tab is dimmed but stays focusable. Selection
   is the caller's business (usually a hash change), so onSelect gets the id. */
import { esc } from "../util.js";

export function tabsHtml(tabs, current, { label = "Sections", idPrefix = "tab" } = {}) {
  return `<div class="tabs" role="tablist" aria-label="${esc(label)}">${tabs.map(t => {
    const on = t.id === current;
    return `<button type="button" role="tab" id="${idPrefix}-${esc(t.id)}" data-tab="${esc(t.id)}"
      aria-selected="${on}" aria-controls="tab-body" tabindex="${on ? 0 : -1}"
      class="${on ? "active" : ""}${t.empty ? " empty-tab" : ""}">${esc(t.label)}${
      t.count != null ? ` <span class="tab-count">${esc(t.count)}</span>` : ""}</button>`;
  }).join("")}</div>`;
}

export function bindTabs(root, onSelect) {
  const list = root.querySelector('[role="tablist"]');
  if (!list) return;
  const tabs = () => [...list.querySelectorAll('[role="tab"]')];
  list.addEventListener("click", e => {
    const b = e.target.closest('[role="tab"]');
    if (b) onSelect(b.dataset.tab);
  });
  list.addEventListener("keydown", e => {
    const all = tabs();
    const i = all.indexOf(document.activeElement);
    if (i < 0) return;
    const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: all.length - 1 }[e.key];
    if (to == null) return;
    e.preventDefault();
    all[(to + all.length) % all.length].focus();
  });
}
