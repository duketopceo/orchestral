import * as F from "../format.js";
import { $view } from "../dom.js";
import { can, data } from "../data.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { icon } from "../components/states.js";
import { NIL, esc, fmtMoney, fmtMs, fmtPct, fmtScore, slug } from "../util.js";
import { heatmap, laneStrip } from "../charts/index.js";

let strip = null; // ResizeObserver of the strip plot currently on screen

function fmtMinutes(m) {
  if (m < 90) return `${Math.round(m)}min`;
  const h = m / 60;
  return `${h >= 10 ? Math.round(h) : h.toFixed(1)}h`;
}

function costWord(r) {
  if (r.cost_per_pass != null) return fmtMoney(r.cost_per_pass);
  if (!((r.cost_total || 0) > 0)) return `<span class="dim">unmetered</span>`;
  return `<span class="dim">no passes</span>`;
}

export async function viewLeaderboard(params) {
  if (strip) { strip.disconnect(); strip = null; }
  const groups = await data.groups();
  const requestedGroup = params.get("group");
  const requestedLens = params.get("lens") || "overall";
  // Default scope: the most recent group with at least three pairings, else all groups.
  let group = requestedGroup !== null ? requestedGroup : "";
  let d = await data.pairings(group);
  if (requestedGroup === null && d.default_group) {
    group = d.default_group;
    d = await data.pairings(group);
  }
  const lens = d.lenses.find(item => item.id === requestedLens) || d.lenses[0] || {
    id: "overall", label: "Best overall", description: "No eligible pairing yet.",
    selected_target: "", ranking: [], reason: "", empty_reason: "No pairing has three finished runs yet", eligible: 0,
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
  const summary = d.summary || { pairings: rows.length, metered: 0, unmetered: 0, best_eligible: 0, no_pass: 0 };
  const cardHref = (kind, target) => {
    const query = new URLSearchParams({ kind, target, lens: lens.id });
    if (group) query.set("group", group);
    return `#/card?${query.toString()}`;
  };
  const lensHref = id => {
    const query = new URLSearchParams({ lens: id });
    query.set("group", group);
    return `#/leaderboard?${query.toString()}`;
  };
  const leaderboardHash = lensHref(lens.id);
  const selectedHref = lens.selected_target ? cardHref("pairing", lens.selected_target) : "";
  const single = rows.length === 1;

  const notes = [];
  if (rows.length && summary.best_eligible === 0) {
    notes.push(`No pairing has n≥${F.LOW_N_BEST} yet. Rankings are provisional.`);
  }
  if (rows.length && summary.metered === 0) {
    notes.push("No pairing is metered, so the cost lenses have nothing to rank.");
  } else if (summary.unmetered > 0) {
    notes.push(`${summary.unmetered} unmetered ${summary.unmetered === 1 ? "pairing is" : "pairings are"} left out of the cost lenses and labeled.`);
  }
  if (summary.no_pass > 0) {
    notes.push(`${summary.no_pass} ${summary.no_pass === 1 ? "pairing has" : "pairings have"} no passes and sorts last.`);
  }

  const stripRows = rows.map(r => ({ ...r, key: r.target, href: cardHref("pairing", r.target) }));
  const heat = mx.orchestrators.length && mx.workers.length ? heatmap({
    id: "lb-matrix",
    caption: "Orchestrator rows by worker columns, mechanical pass rate. Scroll sideways to see every worker. Hatched cells have fewer than 3 evidence runs.",
    corner: "Orchestrator / worker",
    rowHeads: mx.orchestrators.map(o => ({ key: o, label: F.shortSlug(o, 20), title: o })),
    colHeads: mx.workers.map(w => ({ key: w, label: F.shortSlug(w, 18), title: w })),
    cells: mx.cells.filter(c => c.runs).map(c => ({
      row: c.orchestrator, col: c.worker, value: c.pass_rate, n: c.evidence ?? c.finished ?? c.runs,
      href: cardHref("pairing", `${c.orchestrator}|${c.worker}`),
      selected: `${c.orchestrator}|${c.worker}` === lens.selected_target,
      title: `${c.orchestrator} to ${c.worker}: pass ${fmtPct(c.pass_rate)} over ${c.evidence ?? c.finished ?? c.runs} evidence runs${c.low_sample ? ", low n" : ""}${c.score_mean != null ? `, score mean ${fmtScore(c.score_mean)}${c.score_mean_ci ? ` (bootstrap 95% CI ${fmtScore(c.score_mean_ci[0])} to ${fmtScore(c.score_mean_ci[1])})` : ""}` : ""}`,
    })),
  }) : `<div class="empty">No pairings in this scope yet.</div>`;

  $view.innerHTML = `
    <h1>Pairings</h1>
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
          ${can("png_capture") ? `<a class="btn" href="/api/shot.png?route=${encodeURIComponent(leaderboardHash.slice(1))}" download>Download view</a>` : ""}
          ${group ? `<a class="btn" href="${cardHref("group", group)}">Cohort card</a>` : ""}
        </div>
      </div>
      ${single ? `<p class="single-row-note">This group has one pairing, so every lens selects it. It is shown once below.</p>` : `
      <div class="lens-strip" role="tablist" aria-label="Story lenses">
        ${d.lenses.map(item => `<a class="lens-tab${item.id === lens.id ? " active" : ""}${item.selected_target ? "" : " empty"}"
          href="${lensHref(item.id)}" role="tab" aria-selected="${item.id === lens.id}">
          <span>${esc(item.label)}</span><small>${item.selected_target ? esc(item.selected_target.replace("|", " → ")) : "No eligible row"}</small>
        </a>`).join("")}
      </div>
      <div class="story-selection">
        <div>
          <div class="eyebrow">${esc(lens.label)}</div>
          <p>${esc(lens.selected_target ? lens.reason : lens.empty_reason)}${lens.eligible === 1 ? " Only one pairing is eligible for this lens." : ""}</p>
        </div>
        ${selectedHref ? `<a class="btn primary" href="${selectedHref}">Create card →</a>` : `<span class="chip chip-dim">No eligible card yet</span>`}
      </div>`}
    </div>
    ${notes.map(n => `<p class="chart-note">${esc(n)}</p>`).join("")}

    <h2>Lanes: ${esc(lens.label)}</h2>
    <p class="page-sub">One lane per pairing, in table order. Pass rate carries its 95% interval; cost per pass sits on its own log axis. Hatched intervals are low n. Unmetered pairings do not plot on cost.</p>
    <div class="panel panel-pad"><div id="lb-strip">${rows.length ? "" : `<div class="empty">No pairings in this scope yet.</div>`}</div></div>

    <h2>Orchestrator by worker</h2>
    <div class="panel panel-pad">${heat}</div>

    <h2>Ranking: ${esc(lens.label)}</h2>
    <div class="panel"><table class="data rank"><thead><tr>
      <th>#</th><th>Pairing</th><th class="t-num" title="Verdicted evidence runs / total real runs">Evidence</th>
      <th class="t-num">Pass</th><th class="t-num" data-pri="2" title="Macro-averaged pass rate: mean of per-task rates, every task equal weight">Macro</th><th class="t-num">95% CI</th><th class="t-num">Judge</th>
      ${rows.some(r => r.bt) ? '<th class="t-num" data-pri="2" title="Bradley-Terry rating from position-swapped judge battles on the same tasks; 1.0 is average strength">BT</th>' : ""}
      <th class="t-num">Fail</th><th class="t-num">Cost / pass</th><th class="t-num">Cost</th>
      <th class="t-num" data-pri="3">p50</th><th class="t-num" data-pri="3">p90</th>
      <th data-pri="3">Rationale</th><th></th>
    </tr></thead><tbody>` +
    rows.map(r => {
      const rowRank = !r.low_sample && order.has(r.target) ? ++rank : null;
      return `<tr data-target="${esc(r.target)}" class="${r.low_sample ? "row-thin" : ""}${r.target === lens.selected_target ? "story-selected" : ""}">
        <td class="dim">${rowRank == null ? NIL : `${rowRank}<span class="dim sm"> / ${ranked}</span>`}</td>
        <td class="mono">${esc(slug(r.orchestrator))} <span class="dim">→</span> ${esc(slug(r.worker))}
          ${r.low_n_best ? ` <span class="chip chip-dim">${icon("low-n")}Low n</span>` : ""}${r.on_frontier ? ` <span class="chip" title="Pareto frontier: no pairing with a credible sample is both better on macro pass rate and cheaper per pass">${icon("frontier")}Frontier</span>` : ""}${r.target === lens.selected_target ? ' <span class="chip chip-acc">Selected</span>' : ""}
          ${r.horizon ? `<div class="dim sm" title="Time-horizon fit: pass rate vs task length says this pairing passes half of ~${fmtMinutes(r.horizon.t50_minutes)} tasks (logistic fit, n=${r.horizon.n} runs over ${r.horizon.tasks} timed tasks)">t50 ~${fmtMinutes(r.horizon.t50_minutes)}</div>` : ""}</td>
        <td class="t-num">${r.evidence ?? r.finished ?? 0}/${r.runs ?? 0}</td>
        <td class="t-num mech-axis">${fmtPct(r.pass_rate)}</td>
        <td class="t-num" title="${r.macro_pass_rate_ci ? `bootstrap 95% CI ${fmtPct(r.macro_pass_rate_ci[0])} to ${fmtPct(r.macro_pass_rate_ci[1])}` : "mean of per-task pass rates"}">${r.macro_pass_rate != null ? fmtPct(r.macro_pass_rate) : NIL}</td>
        <td class="t-num dim">${r.pass_ci ? F.rangePct(r.pass_ci[0], r.pass_ci[1]) : NIL}</td>
        <td class="t-num judge-axis" title="${r.judged ? `${r.judged} judged run${r.judged === 1 ? "" : "s"}` : "No judged runs"}">${fmtScore(r.judge_score_median)}${r.judged ? `<span class="dim sm">·${r.judged}</span>` : ""}</td>
        ${rows.some(r => r.bt) ? `<td class="t-num" title="${r.bt ? `${r.bt.battles} battles, ${r.bt.wins} wins; theta ${r.bt.theta.toFixed(2)} ± ${r.bt.theta_se === Infinity ? "inf" : (1.96 * r.bt.theta_se).toFixed(2)}` : "no battles"}">${r.bt ? r.bt.rating.toFixed(2) : NIL}</td>` : ""}
        <td class="t-num${(r.failure_rate ?? 0) > 0.15 ? ' e' : ''}">${r.failure_rate != null ? fmtPct(r.failure_rate) : NIL}</td>
        <td class="t-num" title="${r.cost_per_pass_ci ? `bootstrap 95% CI ${fmtMoney(r.cost_per_pass_ci[0])} to ${fmtMoney(r.cost_per_pass_ci[1])}` : ""}">${costWord(r)}</td>
        <td class="t-num">${fmtMoney(r.cost_total)}</td>
        <td class="t-num">${fmtMs(r.duration_median_ms)}</td>
        <td class="t-num">${fmtMs(r.duration_p90_ms)}</td>
        <td class="dim why-cell">${esc(r.why || F.NULL_GLYPH)}</td>
        <td><a class="btn" href="${cardHref("pairing", r.target)}">Card</a>
          ${flagWidget("pairing", r.target)}</td>
      </tr>`;
    }).join("") + `</tbody></table></div>`;

  const host = document.getElementById("lb-strip");
  if (rows.length) {
    let drawn = 0;
    const draw = () => {
      const w = Math.floor(host.clientWidth);
      if (w < 1 || Math.abs(w - drawn) < 1) return;
      drawn = w;
      host.innerHTML = laneStrip({ id: "lb-lanes", rows: stripRows, width: w, lensLabel: lens.label, selected: lens.selected_target });
    };
    draw();
    if (typeof ResizeObserver !== "undefined") {
      strip = new ResizeObserver(draw);
      strip.observe(host);
    }
  }

  document.getElementById("lb-group").addEventListener("change", e => {
    const query = new URLSearchParams({ lens: lens.id, group: e.target.value });
    location.hash = `#/leaderboard?${query.toString()}`;
  });
  await loadFlags();
  bindFlags($view);
}
