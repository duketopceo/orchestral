import * as F from "../format.js";
import { $view } from "../dom.js";
import { data } from "../data.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { icon } from "../components/states.js";
import { NIL, esc, fmtMoney, fmtMs, fmtPct, fmtScore, slug } from "../util.js";

function lbScatter(rows, cardHref, selectedTarget) {
  const pts = rows.filter(r => r.cost_per_pass != null && r.pass_rate != null);
  if (pts.length < 2) return `<div class="empty">Need at least 2 metered pairings to plot cost vs outcome</div>`;
  const W = 720, H = 260, padL = 40, padR = 14, padT = 16, padB = 30;
  const xs = pts.map(r => r.cost_per_pass);
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const llo = Math.log10(lo), lhi = Math.log10(hi);
  const px = v => padL + ((Math.log10(v) - llo) / ((lhi - llo) || 1)) * (W - padL - padR);
  const py = v => padT + (1 - v) * (H - padT - padB);
  const rMax = Math.max(...pts.map(r => r.finished || 1));
  return `<svg class="scatter" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Cost per pass versus pass rate, one dot per pairing">
    <line x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}" class="sc-axis"/>
    <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${H - padB}" class="sc-axis"/>
    ${[0, 0.5, 1].map(v => `
      <line x1="${padL}" y1="${py(v)}" x2="${W - padR}" y2="${py(v)}" class="sc-grid"/>
      <text x="${padL - 6}" y="${py(v) + 3}" class="sc-lab" text-anchor="end">${v * 100}%</text>`).join("")}
    <text x="${(W + padL - padR) / 2}" y="${H - 6}" class="sc-lab" text-anchor="middle">Cost per pass (log scale) →</text>
    ${(() => {
      const placed = [];
      const LW = 5.7; // approx char width at 9px mono
      return pts.map((r, index) => {
        const rr = 4 + 8 * Math.sqrt((r.finished || 1) / rMax);
        const short = s => slug(s).replace(/-\d{2,4}$/, "").slice(0, 14);
        const label = `${short(r.orchestrator)}→${short(r.worker)}`;
        const cx = px(r.cost_per_pass), cy = py(r.pass_rate);
        const selected = r.target === selectedTarget;
        const showLabel = selected || index < 6;
        if (!showLabel) return `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${rr.toFixed(1)}"
          class="sc-pt${r.low_sample ? " thin" : ""}${selected ? " selected" : ""}"
          data-go="${cardHref("pairing", r.orchestrator + "|" + r.worker)}">
          <title>${esc(r.orchestrator)} → ${esc(r.worker)}: pass ${fmtPct(r.pass_rate)}, ${fmtMoney(r.cost_per_pass)}/pass, n=${r.finished}</title></circle>`;
        const lx = Math.min(Math.max(cx, padL + label.length * LW / 2), W - padR - label.length * LW / 2);
        let ty = cy - rr - 4;
        // nudge down until the label box clears everything placed so far
        for (let tries = 0; tries < 8; tries++) {
          const box = { x0: lx - label.length * LW / 2, x1: lx + label.length * LW / 2, y0: ty - 9, y1: ty + 2 };
          const hit = placed.some(p => box.x0 < p.x1 && box.x1 > p.x0 && box.y0 < p.y1 && box.y1 > p.y0);
          if (!hit) { placed.push(box); break; }
          ty += 12;
        }
        return `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${rr.toFixed(1)}"
          class="sc-pt${r.low_sample ? " thin" : ""}${selected ? " selected" : ""}"
          data-go="${cardHref("pairing", r.orchestrator + "|" + r.worker)}">
          <title>${esc(r.orchestrator)} → ${esc(r.worker)}: pass ${fmtPct(r.pass_rate)}, ${fmtMoney(r.cost_per_pass)}/pass, n=${r.finished}</title></circle>
        <text x="${lx.toFixed(1)}" y="${ty.toFixed(1)}" class="sc-pt-lab" text-anchor="middle">${esc(label)}</text>`;
      }).join("");
    })()}
  </svg>`;
}

export async function viewLeaderboard(params) {
  const groups = await data.groups();
  const requestedGroup = params.get("group");
  // A run group is the default story boundary; all-runs remains an explicit choice.
  const group = requestedGroup !== null ? requestedGroup : (groups[0] && groups[0].group) || "";
  const requestedLens = params.get("lens") || "overall";
  const d = await data.pairings(group);
  const lens = d.lenses.find(item => item.id === requestedLens) || d.lenses[0] || {
    id: "overall", label: "Best overall", description: "No eligible pairing yet.",
    selected_target: "", ranking: [], reason: "", empty_reason: "No pairing has three finished runs yet",
  };
  const order = new Map(lens.ranking.map((target, index) => [target, index]));
  const rows = [...d.rows].sort((a, b) => {
    const ai = order.has(a.target) ? order.get(a.target) : Number.MAX_SAFE_INTEGER;
    const bi = order.has(b.target) ? order.get(b.target) : Number.MAX_SAFE_INTEGER;
    return ai - bi || Number(a.low_sample) - Number(b.low_sample) ||
      (b.pass_rate ?? -1) - (a.pass_rate ?? -1) ||
      (a.cost_per_pass ?? 1e9) - (b.cost_per_pass ?? 1e9);
  });
  let rank = 0;
  const ranked = lens.ranking.length;
  const mx = d.matrix;
  const maxPass = Math.max(0.01, ...mx.cells.map(c => c.pass_rate ?? 0));
  const cellOf = (o, w) => mx.cells.find(c => c.orchestrator === o && c.worker === w);
  const cardHref = (kind, target) => {
    const query = new URLSearchParams({ kind, target, lens: lens.id });
    if (group) query.set("group", group);
    return `#/card?${query.toString()}`;
  };
  const leaderboardHash = () => {
    const query = new URLSearchParams({ lens: lens.id });
    if (group) query.set("group", group);
    return `#/leaderboard?${query.toString()}`;
  };
  const lensHref = id => {
    const query = new URLSearchParams({ lens: id });
    if (group) query.set("group", group);
    return `#/leaderboard?${query.toString()}`;
  };
  const selectedHref = lens.selected_target ? cardHref("pairing", lens.selected_target) : "";

  $view.innerHTML = `
    <h1>Leaderboard</h1>
    <p class="page-sub">Choose a run group, then choose the story lens that makes the evidence useful.
    Mechanical pass and judge interpretation stay separate; thin samples stay below the line.</p>

    <div class="story-controls panel">
      <div class="story-control-head">
        <div>
          <div class="eyebrow">Story scope</div>
          <label class="f">Run group
            <select id="lb-group"><option value="">All groups</option>${groups.map(g =>
              `<option value="${esc(g.group)}" ${g.group === group ? "selected" : ""}>${esc(g.label || g.group)} · ${g.runs} runs</option>`).join("")}</select>
          </label>
        </div>
        <div class="story-control-actions">
          <a class="btn" href="/api/shot.png?route=${encodeURIComponent(leaderboardHash().slice(1))}" download>Download view</a>
          ${group ? `<a class="btn" href="${cardHref("group", group)}">Cohort card</a>` : ""}
        </div>
      </div>
      <div class="lens-strip" role="tablist" aria-label="Story lenses">
        ${d.lenses.map(item => `<a class="lens-tab${item.id === lens.id ? " active" : ""}${item.selected_target ? "" : " empty"}"
          href="${lensHref(item.id)}" role="tab" aria-selected="${item.id === lens.id}">
          <span>${esc(item.label)}</span><small>${item.selected_target ? esc(item.selected_target.replace("|", " → ")) : "No eligible row"}</small>
        </a>`).join("")}
      </div>
      <div class="story-selection">
        <div>
          <div class="eyebrow">${esc(lens.label)}</div>
          <p>${esc(lens.selected_target ? lens.reason : lens.empty_reason)}</p>
        </div>
        ${selectedHref ? `<a class="btn primary" href="${selectedHref}">Create card →</a>` : `<span class="chip chip-dim">No eligible card yet</span>`}
      </div>
    </div>

    <h2>Pairing Matrix</h2>
    <div class="panel panel-pad"><table class="data mx">
      <tr><th class="dim">Orchestrator ↓ Worker →</th>${mx.workers.map(w =>
        `<th class="mx-h">${esc(slug(w))}</th>`).join("")}</tr>
      ${mx.orchestrators.map(o => `<tr>
        <th class="mx-h">${esc(slug(o))}</th>
        ${mx.workers.map(w => {
          const c = cellOf(o, w);
          const v = c && c.runs ? c.pass_rate : null;
          const a = v == null ? 0 : 0.12 + 0.7 * (v / maxPass);
          const target = `${o}|${w}`;
          return `<td class="mx-cell${c && c.low_sample ? " thin" : ""}${target === lens.selected_target ? " selected" : ""}"
            title="${esc(o)} → ${esc(w)}${v != null ? ` · pass ${fmtPct(v)} · n=${c.runs}${c.low_sample ? " · Low n" : ""}` : ""}"
            ${v != null ? `data-go="${cardHref("pairing", target)}"` : ""}>
            ${v != null ? `<span class="mx-fill" style="opacity:${a.toFixed(2)}">${fmtPct(v)}</span>` : `<span class="dim">·</span>`}
          </td>`;
        }).join("")}</tr>`).join("")}
    </table></div>

    <h2>Cost vs Outcome</h2>
    <p class="page-sub">One dot per pairing. Upper-left is cheap and reliable. Dot size is finished runs; faded dots are low-n. Unmetered pairings do not plot.</p>
    <div class="panel panel-pad">${lbScatter(rows, cardHref, lens.selected_target)}</div>

    <h2>Pairings: ${esc(lens.label)}</h2>
    <div class="panel"><table class="data"><tr>
      <th>#</th><th>Pairing</th><th class="t-num">Runs</th>
      <th class="t-num">Pass</th><th class="t-num">95% CI</th><th class="t-num">Judge</th>
      <th class="t-num">Fail</th><th class="t-num">Cost / pass</th><th class="t-num">Cost</th>
      <th class="t-num" data-pri="3">p50</th><th class="t-num" data-pri="3">p90</th>
      <th data-pri="3">Rationale</th><th></th>
    </tr><tbody>` +
    rows.map(r => {
      const rowRank = !r.low_sample && order.has(r.target) ? ++rank : null;
      return `<tr class="${r.low_sample ? "row-thin" : ""}${r.target === lens.selected_target ? "story-selected" : ""}">
        <td class="dim">${rowRank == null ? NIL : `${rowRank}<span class="dim sm"> / ${ranked}</span>`}</td>
        <td class="mono">${esc(slug(r.orchestrator))} <span class="dim">→</span> ${esc(slug(r.worker))}
          ${r.low_sample ? ` <span class="chip chip-dim">${icon("low-n")}Low n</span>` : ""}${r.target === lens.selected_target ? ' <span class="chip chip-acc">Selected</span>' : ""}</td>
        <td class="t-num">${r.finished ?? 0}/${r.runs ?? 0}</td>
        <td class="t-num mech-axis">${fmtPct(r.pass_rate)}</td>
        <td class="t-num dim">${r.pass_ci ? F.rangePct(r.pass_ci[0], r.pass_ci[1]) : NIL}</td>
        <td class="t-num judge-axis" title="${r.judged ? `${r.judged} judged run${r.judged === 1 ? "" : "s"}` : "No judged runs"}">${fmtScore(r.judge_score_median)}${r.judged ? `<span class="dim sm">·${r.judged}</span>` : ""}</td>
        <td class="t-num${(r.failure_rate ?? 0) > 0.15 ? ' e' : ''}">${r.failure_rate != null ? fmtPct(r.failure_rate) : NIL}</td>
        <td class="t-num">${r.cost_per_pass != null ? fmtMoney(r.cost_per_pass) : NIL}</td>
        <td class="t-num">${fmtMoney(r.cost_total)}</td>
        <td class="t-num">${fmtMs(r.duration_median_ms)}</td>
        <td class="t-num">${fmtMs(r.duration_p90_ms)}</td>
        <td class="dim why-cell">${esc(r.why || F.NULL_GLYPH)}</td>
        <td><a class="btn" href="${cardHref("pairing", r.target)}">Card</a>
          ${flagWidget("pairing", r.target)}</td>
      </tr>`;
    }).join("") + `</tbody></table></div>`;

  document.getElementById("lb-group").addEventListener("change", e => {
    const query = new URLSearchParams({ lens: lens.id });
    if (e.target.value) query.set("group", e.target.value);
    location.hash = `#/leaderboard?${query.toString()}`;
  });
  for (const el of $view.querySelectorAll("[data-go]")) {
    el.style.cursor = "pointer";
    el.addEventListener("click", () => { location.hash = el.dataset.go; });
  }
  await loadFlags();
  bindFlags($view);
}
