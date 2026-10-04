import * as F from "../format.js";
import { $view } from "../dom.js";
import { data } from "../data.js";
import { NIL, esc, fmtMoney, fmtWhen } from "../util.js";

export async function viewModels(params = new URLSearchParams()) {
  const d = await data.modelsCatalog();
  const count = u => (u && (u.runs || u.calls)) || 0;
  // one bar scale per role, so a bar compares models inside that role
  const peak = Object.fromEntries(d.roles.map(r => [r, Math.max(1, ...d.models.map(m => count((m.usage || {})[r])))]));
  const roleCell = (m, r) => {
    const u = (m.usage || {})[r] || {};
    if (!u.runs && !u.calls) return `<td class="t-num dim" title="${esc(m.slug)} has not run as ${r}">${NIL}</td>`;
    const err = u.errors ? ` · ${u.errors} error${u.errors === 1 ? "" : "s"}` : "";
    const n = count(u);
    return `<td class="t-num${u.errors ? " warn" : ""}" title="${u.calls} call(s) as ${r} · ${fmtMoney(u.cost_usd)}${err}">
      <span class="usage-cell"><span class="usage-bar" role="img" aria-label="${n} runs as ${esc(r)}, against ${peak[r]} for the busiest model"><span class="usage-fill" style="--w:${(n / peak[r]).toFixed(3)}"></span></span>${n}${u.errors ? ` <span class="dim sm">${u.errors} err</span>` : ""}</span></td>`;
  };
  const qualChips = m => (m.qualified || []).map(r =>
    `<span class="chip${r === m.declared_role ? "" : " chip-dim"}" title="${r === m.declared_role ? "Declared role" : "Qualified: inferred from capabilities or demonstrated in a run"}">${esc(r)}</span>`).join(" ");
  const cap = (icon, word, tip) =>
    `<span class="cap-icon" title="${esc(tip)}"><svg class="i" aria-hidden="true" focusable="false"><use href="#i-${icon}"/></svg><span>${esc(word)}</span></span>`;
  const capChips = m => [
    m.vision ? cap("artifact", "vision", "Image input: can judge image artifacts") : "",
    m.executor ? cap("live", "exec", "Executor agent, not a chat model") : "",
    m.free ? cap("cost", "free", "$0 pricing") : "",
    m.structured ? cap("manifest", "struct", "Declares tools or structured-output support") : "",
    m.expires ? cap("stalled", `exp ${m.expires}`, "Listed expiry: preview or stealth entry") : "",
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
  const sync = d.provider_sync || { state: d.provider_synced_at ? "synced" : "never", synced_at: d.provider_synced_at, source: d.provider_source };
  const stamp = sync.state === "synced"
    ? `<p id="provider-sync" class="stamp" data-state="synced"><span class="stamp-key">Provider list</span> Synced ${esc(fmtWhen(sync.synced_at))}${sync.source ? ` · ${esc(sync.source)}` : ""}</p>`
    : `<p id="provider-sync" class="stamp" data-state="never"><span class="stamp-key">Provider list</span> Never synced. Run <code>harness.py models sync</code></p>`;
  $view.innerHTML = `
    <h1>Model catalog</h1>
    ${stamp}
    <p class="page-sub">Configured roster, decisions engines, and the synced provider list: what qualifies for
    each role and what has actually run in it. Role cells count distinct runs (calls when no run row); "err" marks
    errored calls, and a provider-blocked model surfaces there. Dry runs don't count.</p>
    <div class="panel panel-pad"><input type="search" id="cat-q" placeholder="Filter models…" style="width:100%"></div>
    <div class="panel"><table class="data"><thead><tr>
      <th>Model</th><th data-pri="3">Declared</th><th>Qualified for</th><th data-pri="3">Out modalities</th>
      ${d.roles.map(r => `<th class="t-num">${esc(r)}</th>`).join("")}
      <th class="t-num">Spend</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;
  const box = document.getElementById("cat-q");
  const apply = () => {
    const q = box.value.trim().toLowerCase();
    document.querySelectorAll(".cat-row").forEach(tr => {
      tr.style.display = !q || tr.dataset.q.includes(q) ? "" : "none";
    });
  };
  box.addEventListener("input", apply);
  box.value = params.get("q") || "";
  apply();
}
