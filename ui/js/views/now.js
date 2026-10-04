import * as F from "../format.js";
import { $view } from "../dom.js";
import { can, data, isHosted, meta, optional } from "../data.js";
import { isAbort } from "../api.js";
import { start } from "../poller.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { HOSTED_NOTE, UNOWNED_NOTE } from "../live.js";
import { NIL, esc, failureText, fmtMoney, fmtPct, fmtScore, fmtWhen, nilOr, slug } from "../util.js";
import { icon, liveGlyph, stateHtml } from "../components/states.js";

/* Now: three bands (Live, Changed since you last looked, Needs a look), then the
   tasks x pairings heatmap and a compact groups table. The A/B ledger lives at
   #/experiment; one summary line here links to it. */

const DAY_MS = 24 * 3600 * 1000;
const LOOK_SHOWN = 5;
export const WATERMARK_KEY = "orchestral.now.seen";

/* ---------- the per-device watermark ---------- */

function readWatermark() {
  try {
    const t = Date.parse(localStorage.getItem(WATERMARK_KEY) || "");
    return Number.isNaN(t) ? null : t;
  } catch { return null; }
}

/* A visit is committed when the reader leaves Now (another route, or the page
   itself), not while looking, so re-rendering the band never empties it. */
let pendingVisit = null;
function commitVisit() {
  if (!pendingVisit) return;
  try { localStorage.setItem(WATERMARK_KEY, pendingVisit); } catch { /* storage may be blocked */ }
  pendingVisit = null;
}
window.addEventListener("hashchange", commitVisit);
window.addEventListener("pagehide", commitVisit);

/* ---------- bands ---------- */

const bandHtml = (id, title, body, sub = "") => `
  <section class="band" id="band-${id}" aria-labelledby="band-${id}-h">
    <h2 id="band-${id}-h">${esc(title)}</h2>${sub ? `<p class="band-sub">${sub}</p>` : ""}
    ${body}
  </section>`;

function liveLane(j, hosted) {
  const stalled = j.state === "stalled";
  const idle = j.idle_s == null || hosted ? "" : `${stalled ? "quiet for " : "last event "}${F.duration(j.idle_s * 1000)}`;
  const name = j.run_id
    ? `<a class="jl" href="#/run/${encodeURIComponent(j.run_id)}">${esc(j.label)}</a>`
    : `<span class="jl">${esc(j.label)}</span>`;
  const bits = [hosted ? HOSTED_NOTE : stalled ? "stalled" : (j.phase || "live"),
    j.elapsed && j.elapsed !== F.NULL_GLYPH ? j.elapsed : "", idle, j.spend_usd ? `${fmtMoney(j.spend_usd)} so far` : ""]
    .filter(Boolean);
  const note = !hosted && !j.owned ? `<span class="ll-note">${UNOWNED_NOTE}</span>` : "";
  const abandon = !hosted && j.abandonable && can("flag_write")
    ? `<button class="abandon" type="button" data-run="${esc(j.run_id)}">Mark abandoned</button>` : "";
  const cancel = !hosted && j.cancellable && j.run_id && can("cancel")
    ? `<button class="cancel" type="button" data-run="${esc(j.run_id)}">Cancel</button>` : "";
  return `<div class="live-lane${stalled ? " stalled" : ""}" data-run="${esc(j.run_id || "")}" data-state="${esc(j.state)}">
    ${liveGlyph({ stalled, still: hosted })} ${name}
    <span class="ll-meta">${esc(bits.join(" · "))}</span>${note}${abandon}${cancel}
    <span class="ll-err" role="alert" hidden></span></div>`;
}

function liveBand(jobs, hosted) {
  const body = jobs.length
    ? `<div class="lanes">${jobs.map(j => liveLane(j, hosted)).join("")}</div>`
    : `<p class="band-empty">Nothing is running.${can("launch") ? ` <a href="#/new">New run</a>` : ""}</p>`;
  return bandHtml("live", "Live", body);
}

const isFail = c => c.status === "failed" || (c.status === "finished" && !c.passes);

function changedBand(ov, since, firstVisit) {
  const rows = (ov.changes || []).filter(c => Date.parse(c.finished_at) > since);
  const when = firstVisit
    ? "in the last 24 hours, on this device"
    : `since ${esc(fmtWhen(new Date(since).toISOString()))}, on this device`;
  if (!rows.length) {
    return bandHtml("changed", "Changed since you last looked",
      `<p class="band-empty">Nothing new ${when}.</p>`);
  }
  const pass = rows.filter(c => c.status === "finished" && c.passes).length;
  const fail = rows.filter(isFail).length;
  const other = rows.length - pass - fail;
  const spent = rows.reduce((t, c) => t + (c.cost_usd || 0), 0);
  const fails = rows.filter(isFail).slice(0, 3);
  const body = `<div class="tally">
      <span class="chip chip-pass" data-verdict="pass">${rows.length ? pass : 0} ${icon("pass")}passed</span>
      <span class="chip chip-fail" data-verdict="fail">${fail} ${icon("fail")}failed</span>
      ${other ? `<span class="chip chip-dim" data-verdict="other">${other} other</span>` : ""}
      <span class="dim">Spent ${fmtMoney(spent)}</span></div>
    ${fails.length ? `<ul class="new-fails">${fails.map(c => `<li>
      <a href="#/run/${encodeURIComponent(c.run_id)}">${esc(c.task_title || c.task_id)}</a>
      <span class="dim">${esc(c.failure_reason ? failureText(c.failure_reason) : "Did not pass the checks.")}</span></li>`).join("")}</ul>` : ""}`;
  return bandHtml("changed", "Changed since you last looked", body, `${rows.length} finished ${when}`);
}

const LOOK_KIND = {
  stalled: ["stalled", "Stalled"], infra_error: ["fail", "Infra error"],
  inconclusive_judge: ["inconclusive", "Judge inconclusive"], flagged: ["flag", "Flagged"],
  pricing_drift: ["cost", "Pricing drift"],
};

function lookBand(ov) {
  const items = ov.needs_look || [];
  const total = ov.needs_look_total ?? items.length;
  if (!items.length) return bandHtml("look", "Needs a look", `<p class="band-empty">Nothing needs a look right now.</p>`);
  const shown = items.slice(0, LOOK_SHOWN);
  const more = total - shown.length;
  const body = `<ul class="look-list">${shown.map(i => {
    const [glyph, label] = LOOK_KIND[i.kind] || ["inconclusive", i.kind];
    return `<li class="look-item" data-kind="${esc(i.kind)}">
      <span class="look-kind">${icon(glyph)} ${esc(label)}</span>
      <a href="${esc(i.href)}">${esc(i.title)}</a>
      <span class="dim sm look-why">${esc(i.detail)}</span></li>`;
  }).join("")}</ul>
    ${more > 0 ? `<p class="band-more dim">and ${more} more that need a look.</p>` : ""}`;
  return bandHtml("look", "Needs a look", body);
}

/* ---------- the one-line experiment summary ---------- */

function experimentLine(list) {
  if (!list || !list.length) return "";
  const e = list[0];
  const done = (e.states || {}).done || 0;
  return `<p class="exp-line"><a class="exp-summary" href="#/experiment?matrix=${encodeURIComponent(e.name)}">
    Experiment ${esc(e.name)}: ${e.cells} cells, ${done} done, spend ${fmtMoney(e.spend)}</a>
    <span class="dim">The A/B ledger has its own page.</span></p>`;
}

/* ---------- heatmap (DESIGN 6.9 #4) ---------- */

// Single-hue ramp, 6 steps from --sunken to --pass-fill; the dark steps carry on-fill text.
const rampStep = v => (v == null ? 0 : Math.min(5, Math.floor(v * 6)));
const sentence = t => (t && t === t.toUpperCase() && /[A-Z]/.test(t) ? t.charAt(0) + t.slice(1).toLowerCase() : t);

function heatCell(t, p, c, lowN) {
  const href = `#/runs?task=${encodeURIComponent(t.task_id)}&pairing=${encodeURIComponent(p)}`;
  if (!c) return `<td class="heat-cell"><span class="hm-none" role="img" aria-label="Not attempted"></span></td>`;
  const v = c.pass_rate, low = c.n < lowN;
  const jm = c.judge_mean != null ? `, judge ${fmtScore(c.judge_mean)}` : "";
  const label = `${t.task_title || t.task_id}, ${p}: ${v == null ? "no finished runs" : `${fmtPct(v)} pass`} over ${c.n} run${c.n === 1 ? "" : "s"}${jm}${low ? ", low n" : ""}`;
  return `<td class="heat-cell"><a class="hm hm-${rampStep(v)}${low ? " low-n" : ""}" href="${href}" aria-label="${esc(label)}" title="${esc(label)}">
    ${v == null ? NIL : esc(fmtPct(v))}${low ? '<small class="ln">low n</small>' : ""}</a></td>`;
}

function heatmapBand(mx, lowN) {
  const tasks = mx.tasks || [];
  if (!tasks.length) return "";
  const head = mx.pairings.map(p => `<th class="heat-col" scope="col"><div>${esc(slug(p.split(" → ")[0]))}</div><div class="dim">→ ${esc(slug(p.split(" → ")[1] || ""))}</div></th>`).join("");
  const rows = tasks.map(t => `<tr>
    <th class="heat-task" scope="row"><a href="#/runs?task=${encodeURIComponent(t.task_id)}">${esc(sentence(t.task_title) || t.task_id)}</a>
      <div class="dim sm">${esc(t.task_id)}${t.task_type ? ` · ${esc(t.task_type)}` : ""}${t.difficulty ? ` · ${esc(t.difficulty)}` : ""}</div></th>
    ${mx.pairings.map(p => heatCell(t, p, t.cells[p], lowN)).join("")}</tr>`).join("");
  return `<p class="band-sub">Mechanical pass rate per cell. A hatched cell has too few runs to trust. An empty cell was never attempted.</p>
    <div class="panel heat-wrap"><table class="data heat"><caption class="sr-only">Pass rate by task and pairing. Scrolls sideways.</caption>
      <thead><tr><th class="heat-task" scope="col">Task</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

const matrixError = () => `<div class="band-error" role="alert">
  <p>The task matrix could not load. The rest of this page is unaffected.</p>
  <button type="button" class="btn" id="retry-matrix">Try again</button></div>`;

/* ---------- groups ---------- */

/* A curated or auto label as is; a raw group key is shortened (the full key stays in the tooltip). */
const groupName = g => (g.display_label && g.display_label !== g.group ? g.display_label : slug(g.group));

function groupsBand(groups) {
  if (!groups.length) return "";
  return `<div class="panel"><table class="data groups"><thead><tr>
      <th>Group</th><th class="t-num">Runs</th><th class="t-num">Pass</th><th class="t-num" data-pri="3">Judge</th><th class="t-num" data-pri="3">Cost</th></tr></thead><tbody>
    ${groups.map(g => `<tr>
      <td><a href="#/runs?group=${encodeURIComponent(g.group)}" title="${esc(g.group)}">${esc(groupName(g))}</a>
        ${flagWidget("group", g.group)}${g.description ? `<div class="dim sm">${esc(g.description)}</div>` : ""}</td>
      <td class="t-num">${g.runs}</td>
      <td class="t-num">${nilOr(fmtPct(g.pass_rate))}</td>
      <td class="t-num judge-axis" data-pri="3">${nilOr(fmtScore(g.judge_score_median))}</td>
      <td class="t-num" data-pri="3">${nilOr(fmtMoney(g.cost_usd))}</td></tr>`).join("")}</tbody></table></div>`;
}

/* ---------- the view ---------- */

function emptyState(hosted) {
  return stateHtml("empty", {
    title: "No runs yet",
    body: "Runs appear here as they start and finish.",
    action: !hosted && can("launch")
      ? { label: "New dry run", href: "#/new" }
      : { label: "", command: "python harness.py run --dry-run --task <task> --orchestrator <model> --worker <model>" } });
}

async function settle(promise) {
  try { return [await promise, null]; }
  catch (e) { if (isAbort(e)) throw e; return [null, e]; }
}

function bindLive(root, refresh) {
  root.addEventListener("click", async ev => {
    const btn = ev.target.closest("button[data-run]");
    if (!btn) return;
    const lane = btn.closest(".live-lane");
    btn.disabled = true;
    try {
      await (btn.classList.contains("abandon") ? data.abandonRun(btn.dataset.run) : data.cancelRun(btn.dataset.run));
      await refresh();
    } catch (e) {
      const err = lane.querySelector(".ll-err");
      err.textContent = e.message || "That did not work. Try again.";
      err.hidden = false;
      btn.disabled = false;
    }
  });
}

export async function viewNow() {
  const hosted = isHosted();
  const lowN = meta().low_n?.cell ?? 3;
  const [ov, mx, exps] = await Promise.all([
    data.overview(), settle(data.matrix()), optional(data.experiments()),
  ]);
  await loadFlags();
  const [matrix, matrixErr] = mx;
  const seen = readWatermark();
  const generated = Date.parse(ov.generated_at) || Date.now();
  const since = seen ?? generated - DAY_MS;
  pendingVisit = ov.generated_at || new Date(generated).toISOString();

  const nothing = !(ov.jobs || []).length && !(ov.changes || []).length && !(ov.groups || []).length
    && !(matrix?.tasks || []).length && !matrixErr;
  if (nothing) {
    $view.innerHTML = `<h1>Now</h1>${emptyState(hosted)}`;
    return;
  }

  const taxonomy = ov.taxonomy || {};
  const taxMax = Math.max(1, ...Object.values(taxonomy));
  $view.innerHTML = `
    <h1>Now</h1>
    <p class="page-sub">What is running, what changed and what needs a look. Mechanical verdicts and judge scores are separate axes.</p>
    <div id="live-host">${liveBand(ov.jobs || [], hosted)}</div>
    ${changedBand(ov, since, seen == null)}
    ${lookBand(ov)}
    ${experimentLine(exps)}
    <section class="band" id="band-heatmap" aria-labelledby="band-heatmap-h"><h2 id="band-heatmap-h">Tasks by pairing</h2>
      <div id="heat-host">${matrixErr ? matrixError() : heatmapBand(matrix, lowN)}</div>
    </section>
    ${(ov.groups || []).length ? `<section class="band" id="band-groups" aria-labelledby="band-groups-h"><h2 id="band-groups-h">Groups</h2>${groupsBand(ov.groups)}</section>` : ""}
    ${Object.keys(taxonomy).length ? `<section class="band" id="band-taxonomy" aria-labelledby="band-tax-h"><h2 id="band-tax-h">Failure taxonomy</h2>
      <div class="tax-list">${Object.entries(taxonomy).map(([k, n]) => `
        <div class="tax-row"><span class="tx-name">${esc(k)}</span>
          <span class="tx-bar"><i style="width:${(n / taxMax) * 100}%"></i></span>
          <span class="tx-n">${n}</span></div>`).join("")}</div></section>` : ""}`;

  bindFlags($view);
  const heat = $view.querySelector("#heat-host");
  heat.addEventListener("click", async ev => {
    if (!ev.target.closest("#retry-matrix")) return;
    const [fresh, err] = await settle(data.matrix());
    heat.innerHTML = err ? matrixError() : heatmapBand(fresh, lowN);
  });

  const host = $view.querySelector("#live-host");
  const refreshLive = async signal => {
    const fresh = await data.overview({ signal });
    host.innerHTML = liveBand(fresh.jobs || [], hosted);
  };
  bindLive(host, refreshLive);
  if (can("live_stream")) start("now-live", refreshLive, { ms: 5000, scope: "route" });
}
