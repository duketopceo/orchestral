/* JSON viewer: a collapsible tree with a copy-path button on every node, a search
   box, and a copy-whole-value button. It replaces raw <pre> dumps for report, plan
   and manifest. Built on <details> so keyboard and screen readers get expand and
   collapse for free. Long arrays and objects show their first 100 children. */
import { esc } from "../util.js";

const PAGE = 100;

export async function copyText(text, btn) {
  const was = btn?.textContent;
  try {
    await navigator.clipboard.writeText(text);
    if (btn) btn.textContent = "Copied";
  } catch {
    if (btn) btn.textContent = "Copy failed";
  }
  if (btn) {
    // restore the label without a timer: AbortSignal.timeout fires `abort` after the delay
    const t = AbortSignal.timeout(1500);
    t.addEventListener("abort", () => { if (btn.isConnected) btn.textContent = was; }, { once: true });
  }
}

const pathKey = (base, k) => (/^[A-Za-z_$][\w$]*$/.test(k) ? (base ? `${base}.${k}` : k) : `${base}[${JSON.stringify(k)}]`);
const idxKey = (base, i) => `${base}[${i}]`;

function leaf(v) {
  if (v === null) return `<span class="jv-val jv-null">null</span>`;
  if (typeof v === "string") return `<span class="jv-val jv-str">${esc(JSON.stringify(v))}</span>`;
  return `<span class="jv-val jv-${typeof v}">${esc(String(v))}</span>`;
}

const copyBtn = path => `<button type="button" class="jv-copy" data-path="${esc(path)}" aria-label="Copy path ${esc(path)}">Copy path</button>`;

function node(key, v, path, depth) {
  const k = key == null ? "" : `<span class="jv-key">${esc(key)}</span>`;
  if (v === null || typeof v !== "object") {
    return `<div class="jv-leaf" data-path="${esc(path)}">${k}${k ? '<span class="jv-colon">: </span>' : ""}${leaf(v)} ${copyBtn(path)}</div>`;
  }
  const isArr = Array.isArray(v);
  const entries = isArr ? v.map((x, i) => [i, x]) : Object.entries(v);
  const shown = entries.slice(0, PAGE);
  const kids = shown.map(([ck, cv]) => node(String(ck), cv, isArr ? idxKey(path, ck) : pathKey(path, String(ck)), depth + 1)).join("");
  const more = entries.length > PAGE ? `<div class="jv-more dim">${entries.length - PAGE} more not shown. Copy the value to read all of it.</div>` : "";
  return `<details class="jv-node" data-path="${esc(path)}"${depth < 1 ? " open" : ""}>
    <summary>${k}<span class="jv-meta">${isArr ? `[${entries.length}]` : `{${entries.length}}`}</span> ${copyBtn(path)}</summary>
    <div class="jv-kids">${kids}${more}</div></details>`;
}

export function jsonViewerHtml(value, { label = "value" } = {}) {
  return `<div class="jv" data-label="${esc(label)}">
    <div class="jv-bar">
      <input type="search" class="jv-search" placeholder="Search keys and values" aria-label="Search ${esc(label)}">
      <button type="button" class="jv-copy-all">Copy JSON</button>
    </div>
    <div class="jv-tree">${node(null, value, label, 0)}</div>
    <p class="jv-none dim" hidden>Nothing matches.</p>
  </div>`;
}

export function bindJsonViewer(root, value) {
  for (const jv of root.querySelectorAll(".jv")) {
    jv.addEventListener("click", e => {
      const b = e.target.closest(".jv-copy");
      if (b) { e.preventDefault(); e.stopPropagation(); copyText(b.dataset.path, b); }
    });
    jv.querySelector(".jv-copy-all")?.addEventListener("click", e => copyText(JSON.stringify(value, null, 2), e.currentTarget));
    const search = jv.querySelector(".jv-search");
    search?.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      let hits = 0;
      for (const el of jv.querySelectorAll(".jv-leaf")) {
        const match = !q || el.textContent.toLowerCase().includes(q) || el.dataset.path.toLowerCase().includes(q);
        el.hidden = !match;
        el.classList.toggle("jv-hit", !!q && match);
        if (match) hits += 1;
      }
      // open the branches that hold a hit; hide branches that hold none
      for (const d of [...jv.querySelectorAll(".jv-node")].reverse()) {
        const any = !q || !!d.querySelector(".jv-leaf:not([hidden])");
        d.hidden = !any;
        if (q && any) d.open = true;
      }
      jv.querySelector(".jv-none").hidden = !q || hits > 0;
    });
  }
}
