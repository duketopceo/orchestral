/* Combobox (DESIGN.md 6.10): a text input that filters a listbox, grouped,
   operable by keyboard alone. The input carries role=combobox and
   aria-activedescendant; focus never leaves it. The chosen value lives in a
   hidden input named `name`, so a plain FormData read sees it.

   mountCombobox(host, { id, name, label, items, value, required, placeholder,
                         describedBy, onChange }) -> { input, hidden, setItems, setValue, value, invalid }
   items: [{ value, label, sub?, meta?, group? }] in display order. */
import { esc } from "../util.js";

const MAX_OPTIONS = 200;
const norm = s => String(s ?? "").toLowerCase();

export function mountCombobox(host, opts) {
  const { id, name, label, placeholder = "", required = false, describedBy = "", onChange } = opts;
  let items = opts.items || [];
  let current = null;       // the chosen item
  let shown = [];           // the options on screen, in order
  let active = -1;
  let typed = false;        // filter only after the person types; opening shows everything

  host.classList.add("combo");
  host.innerHTML = `
    ${label ? `<label class="combo-label" for="${esc(id)}">${esc(label)}</label>` : ""}
    <input id="${esc(id)}" class="combo-input" type="text" role="combobox" autocomplete="off" spellcheck="false"
      aria-autocomplete="list" aria-expanded="false" aria-controls="${esc(id)}-list" aria-haspopup="listbox"
      placeholder="${esc(placeholder)}"${required ? ' aria-required="true"' : ""}${describedBy ? ` aria-describedby="${esc(describedBy)}"` : ""}>
    <input type="hidden" name="${esc(name)}">
    <ul id="${esc(id)}-list" class="combo-list" role="listbox" aria-label="${esc(label || name)}" hidden></ul>`;
  const input = host.querySelector(".combo-input");
  const hidden = host.querySelector("input[type=hidden]");
  const list = host.querySelector(".combo-list");

  const open = () => { list.hidden = false; input.setAttribute("aria-expanded", "true"); };
  const close = () => {
    list.hidden = true; input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant"); active = -1;
  };
  const isOpen = () => !list.hidden;

  function setActive(i, scroll = true) {
    const opts = [...list.querySelectorAll("[role=option]")];
    active = opts.length ? (i + opts.length) % opts.length : -1;
    opts.forEach((o, k) => o.setAttribute("aria-selected", String(k === active)));
    if (active >= 0) {
      input.setAttribute("aria-activedescendant", opts[active].id);
      if (scroll) opts[active].scrollIntoView({ block: "nearest" });
    } else input.removeAttribute("aria-activedescendant");
  }

  function render() {
    const q = typed ? norm(input.value).trim().split(/\s+/).filter(Boolean) : [];
    shown = items.filter(it => {
      const hay = norm(`${it.label} ${it.sub || ""} ${it.group || ""} ${it.value}`);
      return q.every(t => hay.includes(t));
    }).slice(0, MAX_OPTIONS);
    let html = "", group = null, n = 0;
    for (const it of shown) {
      if (it.group && it.group !== group) {
        group = it.group;
        html += `<li role="presentation" class="combo-group">${esc(group)}</li>`;
      }
      html += `<li role="option" id="${esc(id)}-o${n}" data-i="${n}" aria-selected="false">
        <span class="combo-main">${esc(it.label)}${it.sub ? `<span class="combo-sub">${esc(it.sub)}</span>` : ""}</span>
        ${it.meta ? `<span class="combo-meta">${esc(it.meta)}</span>` : ""}</li>`;
      n++;
    }
    list.innerHTML = html || `<li role="presentation" class="combo-none">No match. Clear the text to see every choice.</li>`;
    const at = current ? shown.findIndex(it => it.value === current.value) : -1;
    setActive(typed ? 0 : Math.max(at, 0), !typed);
    if (!shown.length) input.removeAttribute("aria-activedescendant");
  }

  function choose(it, { notify = true } = {}) {
    current = it || null;
    hidden.value = it ? it.value : "";
    input.value = it ? it.label : (notify ? "" : input.value);
    input.removeAttribute("aria-invalid");
    typed = false;
    close();
    if (notify) hidden.dispatchEvent(new Event("change", { bubbles: true }));
    onChange?.(current);
  }

  input.addEventListener("input", () => { typed = true; current = null; hidden.value = ""; render(); open(); });
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!isOpen()) { typed = false; render(); open(); }
      else setActive(active + (e.key === "ArrowDown" ? 1 : -1));
    } else if (e.key === "Enter") {
      if (isOpen() && active >= 0 && shown[active]) { e.preventDefault(); choose(shown[active]); }
      else if (isOpen()) e.preventDefault();
    } else if (e.key === "Escape" && isOpen()) {
      e.preventDefault(); e.stopPropagation(); close();
    }
  });
  input.addEventListener("focusout", () => {
    // an exact typed value is accepted; anything else leaves the field empty and invalid
    if (!current && input.value.trim()) {
      const exact = items.find(it => norm(it.label) === norm(input.value.trim()) || norm(it.value) === norm(input.value.trim()));
      if (exact) choose(exact, { notify: true }); else close();
    } else close();
  });
  list.addEventListener("mousedown", e => {
    e.preventDefault(); // keep focus in the input
    const li = e.target.closest("[role=option]");
    if (li) choose(shown[Number(li.dataset.i)]);
  });

  if (opts.value) {
    const it = items.find(x => x.value === opts.value);
    if (it) choose(it, { notify: false });
  }
  return {
    input, hidden,
    get value() { return hidden.value; },
    setItems(next) { items = next; if (current && !items.some(x => x.value === current.value)) choose(null, { notify: false }); },
    setValue(v) { const it = items.find(x => x.value === v); if (it) choose(it); },
    invalid(message) {
      if (message) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
    },
  };
}
