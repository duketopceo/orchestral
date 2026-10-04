import { $view } from "../dom.js";
import { can, data, isHosted } from "../data.js";
import { isAbort } from "../api.js";
import { judgeChip, statusChip } from "../chips.js";
import { HOSTED_NOTE, UNOWNED_NOTE } from "../live.js";
import { confirmAction } from "../components/action-dialog.js";
import { artifactHtml, bindArtifact } from "../components/artifact-viewer.js";
import { bindJsonViewer, copyText, jsonViewerHtml } from "../components/json-viewer.js";
import { icon, liveGlyph, stateHtml } from "../components/states.js";
import { bindTabs, tabsHtml } from "../components/tabs.js";
import { timelineHtml } from "../components/timeline.js";
import { fillBars, enterRows, patch, tick } from "../motion.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { duration } from "../format.js";
import { STOP, start, stop } from "../poller.js";
import { basisNote, billedOf, crumb, esc, failureText, fmtMoney, fmtMs, fmtScore, fmtTok, fmtWhen, nilOr, slug } from "../util.js";

const TAB_LABELS = { artifact: "Artifact", events: "Events", calls: "Calls",
  report: "Report", review: "Review", plan: "Plan", manifest: "Manifest" };
const TABS = Object.keys(TAB_LABELS);

let liveCursor = 0;
let evCount = 0;
let refocusTab = null; // arrow keys move between tabs; the re-render must keep focus on the new one

const kv = (label, val) =>
  `<div class="stat"><span class="s-label">${label}</span><span class="s-val">${val}</span></div>`;

/* ---------- evidence states: one place says why a tab has nothing ---------- */

function evidenceState(tab, sec) {
  const label = TAB_LABELS[tab];
  const reason = esc(sec.reason || "");
  const attrs = `data-evidence="${esc(sec.state)}"`;
  if (sec.state === "not_yet") return stateHtml("starting", { title: `${label}: not yet`, body: reason, attrs });
  if (sec.state === "empty_by_design") return stateHtml("empty", { title: `${label}: empty by design`, body: reason, attrs });
  if (sec.state === "withheld") return stateHtml("empty", { title: `${label}: withheld on the hosted copy`, body: reason, attrs });
  return stateHtml("missing", { title: `${label}: missing`, body: reason, attrs });
}

/* ---------- header ---------- */

function liveLine(d, hosted) {
  const lv = d.liveness || {};
  if (d.meta.status !== "running") return "";
  if (hosted) return `<span class="rd-live" id="rd-live" data-state="${esc(lv.state)}">${liveGlyph({ still: true })} ${HOSTED_NOTE}</span>`;
  if (lv.state === "abandoned") {
    return `<span class="rd-live" id="rd-live" data-state="abandoned">${icon("cancelled")} Marked abandoned. The index still lists it as running.</span>`;
  }
  const stalled = lv.state === "stalled";
  const idle = lv.idle_s == null ? "no events recorded" : `last event ${duration(lv.idle_s * 1000)} ago`;
  const note = lv.owned ? "" : ` <span class="rj-note">${UNOWNED_NOTE}</span>`;
  return `<span class="rd-live" id="rd-live" data-state="${esc(lv.state)}">${liveGlyph({ stalled })} ${stalled ? "Stalled" : "Live"}, ${idle}.${note}</span>`;
}

function actionButtons(d, hosted) {
  const lv = d.liveness || {};
  if (hosted || d.meta.status !== "running") return "";
  const cancel = lv.cancellable && can("cancel")
    ? `<button type="button" class="danger" id="cancel-btn">Cancel run</button>` : "";
  const abandon = lv.abandonable && can("flag_write")
    ? `<button type="button" class="danger" id="abandon-btn">Mark abandoned</button>` : "";
  return `${cancel}${abandon}<span class="rd-act-err" id="rd-act-err" role="alert" hidden></span>`;
}

function failureHtml(d) {
  const f = d.failure;
  if (!f) return "";
  const id = encodeURIComponent(d.meta.run_id);
  const rows = [`<dt>Reason</dt><dd>${f.reason ? `${esc(failureText(f.reason))}${failureText(f.reason) === f.reason ? "" : ` <code class="dim">${esc(f.reason)}</code>`}` : "No reason was recorded."}</dd>`];
  if (f.no_detail) {
    return `<section class="panel rd-fail" aria-labelledby="rd-fail-h" data-no-detail="1">
      <h2 id="rd-fail-h">Why it failed</h2>
      <dl>${rows.join("")}</dl>
      <p class="rd-fail-none">This run failed before it recorded any event, so there is no failing check or event to point at.
        ${f.report_available ? `<a href="#/run/${id}?tab=report">Open the raw report</a>.` : "It wrote no report either."}</p>
    </section>`;
  }
  rows.push(`<dt>Failing check</dt><dd>${f.failing_check ? `<code>${esc(f.failing_check)}</code>`
    : f.errors.length ? `None failed by name. ${esc(f.errors[0])}` : "No check is recorded as failed."}</dd>`);
  if (f.event) {
    const label = f.event.kind === "error" ? "First error event" : "Last event";
    const sum = [f.event.type, f.event.phase, f.event.summary].filter(Boolean).join(" · ");
    rows.push(`<dt>${label}</dt><dd><a class="rd-fail-ev" href="#/run/${id}?tab=events&event=${f.event.index}">${esc(sum)}</a>${
      f.event.kind === "last" ? ` <span class="dim">No event carried an error.</span>` : ""}</dd>`);
  }
  return `<section class="panel rd-fail" aria-labelledby="rd-fail-h">
    <h2 id="rd-fail-h">Why it failed</h2><dl>${rows.join("")}</dl></section>`;
}

const tokensText = m => fmtTok((m.total_input_tokens || 0) + (m.total_output_tokens || 0));

function headHtml(d, runId, tab, hosted) {
  const m = d.meta;
  const js = (d.report && d.report.judges) || {};
  const primary = ((d.report && d.report.judge) || {}).model;
  const extra = Object.entries(js).filter(([s]) => s !== primary);
  const rate = m.total_cost_usd;
  const billed = billedOf(m);
  const differs = rate != null && billed != null && Math.abs(rate - billed) > 1e-9;
  const tabs = TABS.map(t => {
    const s = (d.sections || {})[t] || { state: "ok" };
    return { id: t, label: TAB_LABELS[t], count: s.count, empty: s.state !== "ok" };
  });
  return `
    <header class="run-head rd-head">
      ${crumb("#/runs", "Runs")}
      <h1>${esc(d.task_title || m.task_id)}</h1>
      ${d.task_title ? `<div class="rh-pair dim">${esc(m.task_id)}${d.task_blurb ? `: ${esc(d.task_blurb)}` : ""}</div>` : ""}
      <div class="rh-pair">${esc(m.orchestrator)} <span class="arrow">→</span> ${esc(m.worker)}</div>
      <div class="rh-pair dim">${[`${esc(d.group_label || m.run_group || "")}${d.group_label ? ` <span class="dim">(${esc(m.run_group)})</span>` : ""}`,
        m.replicate ? `replicate ${m.replicate}` : "", `run ${esc(runId.slice(0, 12))}`].filter(Boolean).join(" · ")}</div>
      <div class="run-stats rd-stats">
        <div class="stat"><span class="s-label">Verdict</span><span class="s-val">${statusChip(m)}${m.dry_run ? ' <span class="chip chip-dim">Dry run</span>' : ""}${d.holdout ? ' <span class="chip chip-dim">Holdout</span>' : ""}</span></div>
        <div class="stat"><span class="s-label">Judge</span><span class="s-val">${judgeChip({ ...m, judge_state: d.judge_state, judge_reason: d.judge_reason })}${
          extra.length ? ` <span class="chip chip-dim" title="Secondary judge verdicts. The primary axis is ${esc(primary || "unknown")}">${extra.map(([s, j]) => `${esc(slug(s))} ${fmtScore(j && j.score)}`).join(" · ")}</span>` : ""}</span></div>
        ${kv("Cost", `<span data-tick="cost">${nilOr(fmtMoney(billed))}</span>${m.cost_basis ? ` <span class="dim sm" title="${esc(basisNote(m))}">${esc(basisNote(m))}</span>` : ""}${
          differs ? ` <span class="dim sm" data-rate-card="1">rate card ${fmtMoney(rate)}</span>` : ""}`)}
        ${kv("Tokens", `<span data-tick="tokens">${tokensText(m)}</span>`)}
        ${kv("Duration", fmtMs(m.latency_ms))}
        ${kv("Started", fmtWhen(m.started_at))}
      </div>
      <div class="rd-actions">
        ${liveLine(d, hosted)}
        ${flagWidget("run", runId)}
        <a class="btn" href="#/card?kind=run&target=${esc(runId)}">View card</a>
        ${actionButtons(d, hosted)}
      </div>
    </header>
    ${failureHtml(d)}
    <div class="panel ph-strip" id="tl">${timelineHtml(runId, d.lanes, { running: m.status === "running" })}</div>
    ${tabsHtml(tabs, tab, { label: "Run sections" })}
    <div id="tab-body" role="tabpanel" tabindex="0" aria-labelledby="tab-${tab}"></div>`;
}

/* ---------- the view ---------- */

export async function viewRun(runId, params) {
  const tab = TABS.includes(params.get("tab")) ? params.get("tab") : "artifact";
  const d = await data.run(runId);
  await loadFlags();
  const hosted = isHosted();
  const running = d.meta.status === "running" && (d.liveness || {}).state !== "abandoned";
  $view.innerHTML = headHtml(d, runId, tab, hosted);

  bindTabs($view, id => { location.hash = `#/run/${encodeURIComponent(runId)}?tab=${id}`; });
  // Arrow keys move focus (tabs.js); here they also select, so the URL follows the tab.
  $view.querySelector('[role="tablist"]')?.addEventListener("keydown", e => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const id = document.activeElement?.dataset?.tab;
    if (!id || id === tab) return;
    refocusTab = id;
    location.hash = `#/run/${encodeURIComponent(runId)}?tab=${id}`;
  });
  bindActions(runId, d, params);

  await renderTab(runId, tab, d, running, params);
  bindFlags($view);
  if (refocusTab) { document.getElementById(`tab-${refocusTab}`)?.focus(); refocusTab = null; }
  // A live run is watched by one task that owns the header and the finish: it
  // keeps going whichever tab is open, and a finished run re-renders the view.
  if (running && can("live_stream")) start("run-head", signal => refreshRun(runId, tab, params, signal), { ms: 3000 });
}

function bindActions(runId, d, params) {
  const err = document.getElementById("rd-act-err");
  const run = async (btn, call) => {
    btn.disabled = true;
    try {
      await call(runId);
      await viewRun(runId, params);
    } catch (e) {
      btn.disabled = false;
      if (err) { err.textContent = e.message || "That did not work. Try again."; err.hidden = false; }
    }
  };
  const cancel = document.getElementById("cancel-btn");
  cancel?.addEventListener("click", async () => {
    const ok = await confirmAction({
      title: "Cancel this run?",
      body: "The job running it in this server stops. Spend already incurred stays on the bill.",
      confirmLabel: "Cancel run", keepLabel: "Keep running", opener: cancel });
    if (ok) await run(cancel, id => data.cancelRun(id));
  });
  const abandon = document.getElementById("abandon-btn");
  abandon?.addEventListener("click", async () => {
    const idle = d.liveness?.idle_s;
    const ok = await confirmAction({
      title: "Mark this run abandoned?",
      body: `Nothing in this server is running it and it has been quiet${idle == null ? "" : ` for ${duration(idle * 1000)}`}. `
        + "Its recorded status and spend do not change; it only leaves the Activity list.",
      confirmLabel: "Mark abandoned", keepLabel: "Keep it", opener: abandon });
    if (ok) await run(abandon, id => data.abandonRun(id));
  });
}

function tickStat(name, text) {
  const el = document.querySelector(`#view [data-tick="${name}"]`);
  if (el) tick(el, text);
}

async function refreshRun(runId, tab, params, signal) {
  const d = await data.run(runId, { signal });
  const tl = document.getElementById("tl");
  // phase advance: only bars that appeared since the last poll fill in
  if (tl) patch(tl, timelineHtml(runId, d.lanes, { running: true }), ".ln-bar", n => n.getAttribute("href"), fillBars);
  tickStat("cost", nilOr(fmtMoney(billedOf(d.meta))));
  tickStat("tokens", tokensText(d.meta));
  const live = document.getElementById("rd-live");
  const next = (d.liveness || {}).state;
  if (d.meta.status === "running" && next !== "abandoned") {
    if (live && live.dataset.state !== next) {
      // live <-> stalled changes which actions apply: render the header again
      await viewRun(runId, params);
      return STOP;
    }
    if (live) live.outerHTML = liveLine(d, false);
    return;
  }
  stop("run-events");
  await viewRun(runId, new URLSearchParams(`tab=${tab}`));
  return STOP;
}

/* ---------- tabs ---------- */

async function renderTab(runId, tab, d, running, params) {
  const el = document.getElementById("tab-body");
  if (!el) return;
  const sec = (d.sections || {})[tab] || { state: "ok" };
  const title = d.task_title || d.meta.task_id;

  // a withheld archive still lists its members; every other non-ok state is a statement
  const listOnly = tab === "artifact" && sec.state === "withheld" && d.artifact;
  if (sec.state !== "ok" && !listOnly) {
    el.innerHTML = evidenceState(tab, sec);
    return;
  }

  if (tab === "artifact") {
    const a = d.artifact;
    el.innerHTML = (listOnly ? `<p class="dim sm" data-evidence="withheld">${esc(sec.reason)}</p>` : "")
      + artifactHtml(runId, a, { title, previewMembers: !listOnly });
    bindArtifact(el, runId, a, { title, previewMembers: !listOnly });
    return;
  }
  if (tab === "events") return renderEvents(el, runId, d, running, params);
  if (tab === "calls") { el.innerHTML = callsHtml(d); return; }
  if (tab === "report") { el.innerHTML = reportHtml(d.report); bindJsonViewer(el, d.report); return; }
  if (tab === "review") { el.innerHTML = reviewHtml(d.review); return; }
  if (tab === "plan") return renderPlan(el, d);
  if (tab === "manifest") { el.innerHTML = manifestHtml(d.manifest); bindJsonViewer(el, d.manifest); bindCopy(el); }
}

function bindCopy(el) {
  for (const b of el.querySelectorAll("button[data-copy]")) b.addEventListener("click", () => copyText(b.dataset.copy, b));
}

/* Events: one button per row (aria-expanded), a follow-latest toggle while live. */

const isErr = type => type.search(/fail|error/i) >= 0; // regex after `(`: the copy-lint scanner misreads `=> /re/`

function evRow(r, detail, i) {
  const [t, type, worker, dd] = r;
  return `<div class="ev-item" data-i="${i}">
    <button type="button" class="ev-row" aria-expanded="false" aria-controls="ev-d-${i}">
      <span class="ev-t">${esc(t)}</span><span class="ev-type${isErr(type) ? " ev-err" : ""}">${esc(type)}</span>
      <span class="ev-d">${esc(worker)} · ${esc(dd)}</span></button>
    <div class="ev-detail" id="ev-d-${i}" hidden>${esc(detail || "")}</div></div>`;
}

function bindEvents(wrap) {
  wrap.addEventListener("click", e => {
    const b = e.target.closest(".ev-row");
    if (!b) return;
    const open = b.getAttribute("aria-expanded") !== "true";
    b.setAttribute("aria-expanded", String(open));
    document.getElementById(b.getAttribute("aria-controls")).hidden = !open;
  });
}

async function renderEvents(el, runId, d, running, params) {
  let live;
  try {
    live = await data.runLive(runId, 0);
  } catch (e) {
    if (isAbort(e)) throw e;
    el.innerHTML = stateHtml("error", { title: "Events could not be loaded", body: esc(e.message || "The request failed."),
      action: { label: "Retry", id: "ev-retry" }, attrs: 'data-evidence="error"', role: "alert" });
    document.getElementById("ev-retry")?.addEventListener("click", () => renderEvents(el, runId, d, running, params));
    return;
  }
  liveCursor = live.next;
  evCount = live.rows.length;
  const follow = running && can("live_stream");
  el.innerHTML = `${follow ? `<div class="ev-tools"><button type="button" id="ev-follow" aria-pressed="true">Follow latest</button>
      <span class="dim sm">Streaming</span></div>` : ""}
    <div class="ev-wrap" id="ev-wrap">${live.rows.map((r, i) => evRow(r, live.details[i], i)).join("")}</div>`;
  const wrap = document.getElementById("ev-wrap");
  bindEvents(wrap);

  if (params.has("event")) {
    const item = wrap.querySelector(`.ev-item[data-i="${Number(params.get("event"))}"]`);
    if (item) {
      item.classList.add("ev-target");
      item.querySelector(".ev-row").click();
      item.scrollIntoView({ block: "center" });
    }
  }

  const followBtn = document.getElementById("ev-follow");
  followBtn?.addEventListener("click", () => followBtn.setAttribute("aria-pressed", String(followBtn.getAttribute("aria-pressed") !== "true")));
  if (follow) start("run-events", async signal => {
    const inc = await data.runLive(runId, liveCursor, { signal });
    liveCursor = inc.next;
    const w = document.getElementById("ev-wrap");
    if (w && inc.rows.length) {
      const seen = w.children.length;
      w.insertAdjacentHTML("beforeend", inc.rows.map((r, i) => evRow(r, inc.details[i], evCount + i)).join(""));
      enterRows([...w.children].slice(seen));
      evCount += inc.rows.length;
      const tabCount = document.querySelector("#tab-events .tab-count");
      if (tabCount) tabCount.textContent = String(evCount);
      if (document.getElementById("ev-follow")?.getAttribute("aria-pressed") === "true") w.scrollTop = w.scrollHeight;
    }
    if (inc.status && inc.status !== "running") return STOP;
  }, { ms: 2000 });
}

/* Calls: billed against rate card, with where each price came from. */

function priceSource(c) {
  if (c.api_cost_usd != null) return "billed by provider";
  return !c.pricing_source || c.pricing_source === "none" ? "rate card" : String(c.pricing_source).replace(/_/g, " ");
}

function callsHtml(d) {
  const calls = d.calls || [];
  let billed = 0, rate = 0;
  const rows = calls.map(c => {
    billed += c.api_cost_usd != null ? c.api_cost_usd : (c.cost_usd || 0);
    rate += c.cost_usd || 0;
    return `<tr>
      <td class="mono">${esc(c.phase || "")}</td>
      <td class="mono">${esc(c.model || "")}</td>
      <td class="t-num" data-pri="3">${fmtTok(c.input_tokens)}</td>
      <td class="t-num" data-pri="3">${fmtTok(c.output_tokens)}</td>
      <td class="t-num" data-col="billed">${c.api_cost_usd != null ? fmtMoney(c.api_cost_usd) : `<span class="dim">${fmtMoney(c.cost_usd)}</span>`}</td>
      <td class="t-num" data-col="rate">${fmtMoney(c.cost_usd)}</td>
      <td data-col="source">${esc(priceSource(c))}</td>
      <td class="t-num">${fmtMs(c.latency_ms)}</td></tr>`;
  }).join("");
  return `${d.meta.dry_run ? `<p class="dim sm" data-evidence="dry-run">Dry run: no provider was called, so these calls are simulated and cost nothing.</p>` : ""}
    <div class="panel"><table class="data calls"><thead><tr>
      <th>Phase</th><th>Model</th><th class="t-num" data-pri="3">Input tokens</th><th class="t-num" data-pri="3">Output tokens</th>
      <th class="t-num">Billed</th><th class="t-num">Rate card</th><th>Price source</th><th class="t-num">Latency</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><th colspan="4" scope="row">Total</th><td class="t-num">${fmtMoney(billed)}</td><td class="t-num">${fmtMoney(rate)}</td><td colspan="2" class="dim">A call with no provider price shows its rate card price, dimmed, under Billed.</td></tr></tfoot>
    </table></div>`;
}

/* Report: the checks as a list, then the whole document as a tree. */

function reportHtml(report) {
  const checks = report && typeof report.checks === "object" && report.checks ? Object.entries(report.checks) : [];
  const errors = Array.isArray(report?.errors) ? report.errors : [];
  const list = checks.length ? `<ul class="rd-checks" aria-label="Checks">${checks.map(([k, ok]) =>
    `<li data-check="${esc(k)}" data-ok="${ok === true}">${ok === true ? icon("pass") : icon("fail")} <code>${esc(k)}</code> <span class="dim">${ok === true ? "passed" : "failed"}</span></li>`).join("")}</ul>` : "";
  const errs = errors.length ? `<p class="rd-errs">${errors.map(e => esc(String(e))).join("<br>")}</p>` : "";
  const judge = report && typeof report.judge === "object" ? report.judge : null;
  const crit = judge && Array.isArray(judge.criteria) && judge.criteria.length ? criteriaHtml(judge) : "";
  return `${list}${errs}${crit}${jsonViewerHtml(report, { label: "report" })}`;
}

/* Per-criterion judge contract (v2): the verdict the judge claimed,
   whether quoted evidence verifies it, and the grading engine. Secret
   criteria withhold the rubric — render only what report.json carries. */

function criteriaHtml(judge) {
  const mark = (v) => v === true ? '<span class="chip chip-pass">yes</span>'
    : v === false ? '<span class="chip chip-fail">no</span>'
    : '<span class="chip chip-dim">-</span>';
  const r = judge.criteria_rollup || {};
  const head = `<div class="panel panel-pad"><h3>Criteria <span class="dim sm">${
    judge.judge_contract || "v1"}${r.total != null ? ` · ${r.satisfied}/${r.total} satisfied` : ""}${
    r.unsupported ? ` · ${r.unsupported} unsupported` : ""}${
    r.unassessed ? ` · ${r.unassessed} unassessed` : ""}</span></h3>
    <table class="data"><thead><tr>
      <th>Criterion</th><th>Satisfied</th><th>Evidence</th><th>Engine</th><th>Note</th>
    </tr></thead><tbody>`;
  const rows = judge.criteria.map(c => `<tr>
      <td>${esc(c.id || "")}${c.rubric ? `<div class="dim sm">${esc(c.rubric)}</div>` : ""}</td>
      <td>${mark(c.satisfied)}${c.supported === false ? ` <span class="chip chip-warn" title="satisfied claim without verified artifact evidence">unverified</span>` : ""}</td>
      <td class="dim sm">${(c.evidence || []).map(e => `<div class="mono">"${esc(e)}"</div>`).join("") || "-"}</td>
      <td class="dim">${esc(c.engine || "")}</td>
      <td class="dim sm">${esc(c.note || "")}</td>
    </tr>`).join("");
  return head + rows + `</tbody></table></div>`;
}

function reviewHtml(v) {
  if (!v || typeof v !== "object") return `<pre class="block">${esc(String(v))}</pre>`;
  const verdict = v.verdict || v.summary || "";
  const findings = v.findings || [];
  return `<div class="panel panel-pad">
    ${verdict ? `<p style="margin-top:0">${esc(String(verdict))}</p>` : ""}
    ${findings.length ? `<table class="data"><tr><th>Severity</th><th>Finding</th><th>Evidence</th></tr><tbody>` +
      findings.map(f => `<tr>
        <td><span class="chip ${/high|crit/i.test(f.severity || "") ? "chip-fail" : /med/i.test(f.severity || "") ? "chip-warn" : "chip-dim"}">${esc(f.severity || "")}</span></td>
        <td>${esc(f.title || f.finding || "")}</td>
        <td class="dim">${esc(String(f.evidence || "").slice(0, 200))}</td></tr>`).join("") +
      `</tbody></table>` : `<pre class="block">${esc(JSON.stringify(v, null, 2))}</pre>`}
  </div>`;
}

/* Plan: the subtask list by default, with a JSON toggle. */

function subtaskHtml(plan) {
  const subs = Array.isArray(plan.subtasks) ? plan.subtasks : [];
  return `${plan.plan ? `<p class="rd-plan-sum">${esc(plan.plan)}</p>` : ""}
    <ol class="rd-subtasks">${subs.map(s => `<li class="rd-subtask"><strong>${esc(s.title || `Subtask ${s.id ?? ""}`)}</strong>
      ${s.description ? `<p>${esc(s.description)}</p>` : ""}
      ${s.acceptance_criteria ? `<p class="dim sm">Done when: ${esc(Array.isArray(s.acceptance_criteria) ? s.acceptance_criteria.join("; ") : s.acceptance_criteria)}</p>` : ""}</li>`).join("")}</ol>`;
}

function renderPlan(el, d) {
  const plan = d.plan_json;
  if (!plan) {
    el.innerHTML = `<div class="ev-tools"><button type="button" data-copy="${esc(d.plan || "")}">Copy plan</button></div><pre class="block">${esc(d.plan || "")}</pre>`;
    bindCopy(el);
    return;
  }
  el.innerHTML = `<div class="ev-tools" role="group" aria-label="Plan view">
      <button type="button" data-view="list" aria-pressed="true">Subtasks</button>
      <button type="button" data-view="json" aria-pressed="false">JSON</button></div>
    <div id="plan-body">${subtaskHtml(plan)}</div>`;
  const body = document.getElementById("plan-body");
  el.querySelector('[role="group"]').addEventListener("click", e => {
    const b = e.target.closest("button[data-view]");
    if (!b) return;
    for (const x of el.querySelectorAll("[data-view]")) x.setAttribute("aria-pressed", String(x === b));
    if (b.dataset.view === "json") { body.innerHTML = jsonViewerHtml(plan, { label: "plan" }); bindJsonViewer(body, plan); }
    else body.innerHTML = subtaskHtml(plan);
  });
}

/* Manifest: hashes first, each with copy, then the tree. */

function manifestHtml(m) {
  const hashes = [];
  const walk = (v, path) => {
    if (typeof v === "string" && /^[0-9a-f]{32,}$/i.test(v)) hashes.push([path, v]);
    else if (v && typeof v === "object") for (const [k, x] of Object.entries(v)) walk(x, path ? `${path}.${k}` : k);
  };
  walk(m, "");
  const table = hashes.length ? `<div class="panel"><table class="data"><thead><tr><th>Field</th><th>Hash</th><th></th></tr></thead><tbody>${hashes.map(([k, h]) =>
    `<tr><td class="mono">${esc(k)}</td><td class="mono rd-hash" title="${esc(h)}">${esc(h.slice(0, 16))}…</td>
      <td><button type="button" data-copy="${esc(h)}" aria-label="Copy hash for ${esc(k)}">Copy</button></td></tr>`).join("")}</tbody></table></div>` : "";
  return `${table}${jsonViewerHtml(m, { label: "manifest" })}`;
}
