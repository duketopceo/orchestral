import { api } from "./api.js";
import { route } from "./router.js";
import { esc } from "./util.js";

export let FLAGS = {};

export function setFlags(rows) {
  FLAGS = {};
  for (const a of rows) FLAGS[`${a.kind}:${a.target}`] = a;
}

export async function loadFlags() {
  try {
    const rows = await api("/api/flags");
    FLAGS = {};
    for (const a of rows) FLAGS[`${a.kind}:${a.target}`] = a;
  } catch { FLAGS = {}; }
}

export function flagOf(kind, target) { return FLAGS[`${kind}:${target}`]?.flag || ""; }

export function flagWidget(kind, target) {
  const cur = flagOf(kind, target);
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
        await api("/api/flag", {
          method: "POST",
          body: new URLSearchParams({ kind, target, flag }),
        });
        await loadFlags();
        route(); // re-render current view with the new flag
      });
    }
  }
}
