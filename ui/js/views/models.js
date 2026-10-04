import * as F from "../format.js";
import { $view } from "../dom.js";
import { api } from "../api.js";
import { NIL, esc, fmtMoney, fmtWhen } from "../util.js";

export async function viewModels() {
  const d = await api("/api/models-catalog");
  const roleCell = (m, r) => {
    const u = (m.usage || {})[r] || {};
    if (!u.runs && !u.calls) return `<td class="t-num dim" title="${esc(m.slug)} has not run as ${r}">${NIL}</td>`;
    const err = u.errors ? ` · ${u.errors} error${u.errors === 1 ? "" : "s"}` : "";
    return `<td class="t-num${u.errors ? " warn" : ""}" title="${u.calls} call(s) as ${r} · ${fmtMoney(u.cost_usd)}${err}">${u.runs || u.calls}${u.errors ? ` <span class="dim sm">${u.errors} err</span>` : ""}</td>`;
  };
  const qualChips = m => (m.qualified || []).map(r =>
    `<span class="chip${r === m.declared_role ? "" : " chip-dim"}" title="${r === m.declared_role ? "Declared role" : "Qualified: inferred from capabilities or demonstrated in a run"}">${esc(r)}</span>`).join(" ");
  const capChips = m => [
    m.vision ? '<span class="chip chip-info" title="Image input: can judge image artifacts">vision</span>' : "",
    m.executor ? '<span class="chip chip-info" title="Executor agent, not a chat model">exec</span>' : "",
    m.free ? '<span class="chip chip-info" title="$0 pricing">free</span>' : "",
    m.structured ? '<span class="chip chip-dim" title="Declares tools/structured-output support">struct</span>' : "",
    m.expires ? `<span class="chip chip-dim" title="Listed expiry: preview/stealth entry">exp ${esc(m.expires)}</span>` : "",
  ].filter(Boolean).join(" ");
  const srcLabel = d.source_labels || {};
  const row = m => {
    const spend = Object.values(m.usage).reduce((s, u) => s + (u.cost_usd || 0), 0);
    const errs = Object.values(m.usage).reduce((s, u) => s + (u.errors || 0), 0);
    const sub = m.source === "configured" ? m.slug : `${m.slug} · ${srcLabel[m.source] || m.source}`;
    return `<tr class="cat-row" data-q="${esc((m.slug + " " + (m.name || "")).toLowerCase())}">
      <td>${esc(m.name || m.slug)}<div class="dim sm">${esc(sub)}</div></td>
      <td><span class="chip">${esc(m.declared_role || F.NULL_GLYPH)}</span>${m.default ? ' <span class="chip chip-dim">default</span>' : ""}</td>
      <td>${qualChips(m)}${capChips(m) ? " " + capChips(m) : ""}</td>
      <td class="dim sm">${(m.modalities || []).map(esc).join(", ") || F.NULL_GLYPH}</td>
      ${d.roles.map(r => roleCell(m, r)).join("")}
      <td class="t-num">${spend ? fmtMoney(spend) : NIL}${errs ? ` <span class="dim sm" title="calls that returned an error">${errs} err</span>` : ""}</td>
    </tr>`;
  };
  const body = (d.sources || []).map(src => {
    const ms = d.models.filter(m => m.source === src);
    if (!ms.length) return "";
    return `<tr class="cat-group"><th colspan="${5 + d.roles.length}">${esc(srcLabel[src] || src)} · ${ms.length}</th></tr>`
      + ms.map(row).join("");
  }).join("");
  const synced = d.provider_synced_at
    ? `provider list synced ${esc(fmtWhen(d.provider_synced_at))} · ${esc(d.provider_source || "")}`
    : `provider list not synced. Run <code>harness.py models sync</code>`;
  $view.innerHTML = `
    <h1>Model catalog</h1>
    <p class="page-sub">Configured roster, decisions engines, and the synced provider list: what qualifies for
    each role and what has actually run in it. Role cells count distinct runs (calls when no run row); "err" marks
    errored calls, and a provider-blocked model surfaces there. Dry runs don't count. ${synced}.</p>
    <div class="panel panel-pad"><input type="search" id="cat-q" placeholder="Filter models…" style="width:100%"></div>
    <div class="panel"><table class="data"><thead><tr>
      <th>Model</th><th>Declared</th><th>Qualified for</th><th>Out modalities</th>
      ${d.roles.map(r => `<th class="t-num">${esc(r)}</th>`).join("")}
      <th class="t-num">Spend</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;
  document.getElementById("cat-q").addEventListener("input", e => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll(".cat-row").forEach(tr => {
      tr.style.display = !q || tr.dataset.q.includes(q) ? "" : "none";
    });
  });
}
