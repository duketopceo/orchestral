import { isAbort } from "./api.js";
import { can, data } from "./data.js";
import { route } from "./router.js";
import { esc } from "./util.js";

export let FLAGS = {};

export function setFlags(rows) {
  FLAGS = {};
  for (const a of rows) FLAGS[`${a.kind}:${a.target}`] = a;
}

export async function loadFlags() {
  try {
    setFlags(await data.flags());
  } catch (e) {
    if (isAbort(e)) throw e;
    FLAGS = {};
  }
}

export function flagOf(kind, target) { return FLAGS[`${kind}:${target}`]?.flag || ""; }

export function flagWidget(kind, target) {
  const cur = flagOf(kind, target);
  // Flag writes are local only; the hosted mirror shows the flag, never the buttons.
  if (!can("flag_write")) return cur ? `<span class="chip chip-dim flag-static">${esc(cur)}</span>` : "";
  return `<span class="flag-pair" data-kind="${esc(kind)}" data-target="${esc(target)}">
    <button class="flag-btn ${cur === "interesting" ? "f-interesting" : ""}" data-f="interesting" title="Flag as interesting">+</button>
    <button class="flag-btn ${cur === "not" ? "f-not" : ""}" data-f="not" title="Flag as not interesting">∅</button>
  </span>`;
}

export function bindFlags(root) {
  for (const pair of root.querySelectorAll(".flag-pair")) {
    for (const btn of pair.querySelectorAll(".flag-btn")) {
      btn.addEventListener("click", async e => {
        e.preventDefault(); e.stopPropagation();
        const kind = pair.dataset.kind, target = pair.dataset.target;
        const cur = flagOf(kind, target);
        const flag = btn.dataset.f === cur ? "" : btn.dataset.f;
        await data.setFlag({ body: new URLSearchParams({ kind, target, flag }) });
        await loadFlags();
        route(); // re-render current view with the new flag
      });
    }
  }
}
