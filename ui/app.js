/* orchestral observatory — hash-routed SPA over /api/*.
   Two verdict axes are kept visually distinct everywhere:
   mechanical pass = green/red, judge score = info blue. */

"use strict";

const $view = document.getElementById("view");
const $jobs = document.getElementById("rail-jobs");
let pollTimer = null;
let liveCursor = 0;

/* ---------- helpers ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `${r.status} ${path}`);
  return body;
}

function fmtMoney(v) { return v == null ? "—" : `$${Number(v).toFixed(4)}`; }
function fmtPct(v) { return v == null ? "—" : `${Math.round(v * 100)}%`; }
function fmtScore(v) { return v == null ? "—" : Number(v).toFixed(2); }
function fmtMs(v) {
  if (v == null) return "—";
  const s = v / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${(s / 60).toFixed(1)}m`;
}
function fmtTok(v) {
  if (v == null) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v);
}
function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
    " " + d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function slug(s) { return String(s || "").split("/").pop(); }
// Judge-axis caveat text: calibrated judges earn "calibrated" language,
// everything else stays advisory — provenance over thresholds.
function calAxis(d) {
  const cal = d.judge_calibration || {};
  const bits = Object.entries(cal).map(([m, s]) =>
    s.calibrated ? `${esc(slug(m))} κ=${Number(s.kappa).toFixed(2)}`
                 : `${esc(slug(m))} uncalibrated (${s.verdict_pairs} pairs)`);
  const calibrated = Object.values(cal).some(s => s.calibrated);
  const axis = calibrated ? "calibrated semantic axis" : "advisory semantic axis";
  return bits.length ? `${bits.join(" · ")} · ${axis}` : axis;
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
function poll(fn, ms) {
  stopPolling();
  pollTimer = setInterval(fn, ms);
}

function statusChip(r) {
  if (r.status === "running") return `<span class="chip chip-warn"><span class="dot dot-run pulse"></span>running</span>`;
  if (r.status === "failed") return `<span class="chip chip-fail">failed</span>`;
  if (r.status === "cancelled") return `<span class="chip chip-dim">cancelled</span>`;
  if (r.passes === true || r.passes === 1) return `<span class="chip chip-pass">pass</span>`;
  if (r.passes === false || r.passes === 0) return `<span class="chip chip-fail">fail</span>`;
  return `<span class="chip chip-dim">${esc(r.status)}</span>`;
}

function judgeChip(r) {
  // judge_score is the semantic axis — `score` is mechanical (don't mislabel)
  if (r.judge_score != null)
    return `<span class="chip chip-info" title="semantic quality score from the judge model">judge ${fmtScore(r.judge_score)}</span>`;
  const st = r.judge_state || "not_judged";
  const why = esc(r.judge_reason || "");
  if (st === "inconclusive")
    return `<span class="chip chip-warn" title="${why}">judge inconclusive</span>`;
  if (st === "not_judgeable")
    return `<span class="chip chip-dim" title="${why}">not judgeable</span>`;
  return `<span class="chip chip-dim" title="${why || "judge wasn't run for this run"}">not judged</span>`;
}

function runRow(r) {
  return `<tr>
    <td>${statusChip(r)} ${flagWidget("run", r.run_id)}</td>
    <td><a href="#/run/${esc(r.run_id)}">${esc(r.task_title || r.task_id)}</a>${r.task_title ? `<div class="dim sm">${esc(r.task_id)}</div>` : ""}</td>
    <td class="mono">${esc(slug(r.orchestrator))} <span class="dim">→</span> ${esc(slug(r.worker))}</td>
    <td>${judgeChip(r)}</td>
    <td class="t-num">${fmtMoney(r.total_cost_usd)}</td>
    <td class="t-num">${fmtTok((r.total_input_tokens || 0) + (r.total_output_tokens || 0))}</td>
    <td class="t-num">${fmtMs(r.latency_ms)}</td>
    <td class="dim">${esc(r.run_group || "—")}</td>
    <td class="dim">${fmtWhen(r.started_at)}</td>
  </tr>`;
}

const RUN_HEAD = `<tr>
  <th>verdict</th><th>task</th><th>orch → worker</th><th>judge</th>
  <th class="t-num">cost</th><th class="t-num">tok</th><th class="t-num">time</th>
  <th>group</th><th>started</th>
</tr>`;

/* ---------- rail activity ---------- */

async function refreshJobs() {
  try {
    const ov = await api("/api/overview");
    const jobs = (ov.jobs || []).filter(j => j.status === "running" || j.cancellable);
    $jobs.innerHTML = jobs.length
      ? jobs.map(j => `<div class="rail-job"><span class="dot dot-run pulse"></span><span class="jl">${esc(j.label)}</span></div>`).join("")
      : `<div class="rail-empty">no active jobs</div>`;
  } catch { /* rail is best-effort */ }
}
refreshJobs();
setInterval(refreshJobs, 5000);

/* ---------- views ---------- */

async function viewOverview() {
  const [ov, mx] = await Promise.all([api("/api/overview"), api("/api/matrix")]);
  await loadFlags();
  const live = (ov.jobs || []).filter(j => j.status === "running");
  const groups = ov.groups || [];
  const tax = ov.taxonomy || {};
  const taxMax = Math.max(1, ...Object.values(tax));

  $view.innerHTML = `
    <h1>Overview</h1>
    <p class="page-sub">Live experiment observatory — mechanical verdicts and judge scores are separate axes.</p>

    ${live.length ? `<div class="live-strip">${live.map(j => `
      <div class="live-card"><span class="dot dot-run pulse"></span>
        <span class="lc-label">${esc(j.label)}</span>
        <span class="dim">${esc(j.detail || "")}</span>
      </div>`).join("")}</div>` : ""}

    <h2>Groups</h2>
    <div class="group-grid">${groups.map(g => {
      const pass = g.pass_rate, fail = g.finished ? (1 - (pass ?? 0)) : 0;
      const rest = g.runs - (g.finished || 0);
      return `<a class="panel group-card" href="#/runs?group=${encodeURIComponent(g.group)}">
        <div class="split"><span class="gc-name">${esc(g.label || g.group)}</span>
          <span class="dim">${g.runs} runs ${flagWidget("group", g.group)}</span></div>
        ${g.label ? `<div class="dim sm">${esc(g.description || g.group)}</div>` : ""}
        <div class="gc-stats">
          <span>pass <b>${fmtPct(pass)}</b></span>
          <span>judge <b class="judge-axis">${fmtScore(g.judge_score_median)}</b></span>
          <span>cost <b>${fmtMoney(g.cost_usd)}</b></span>
        </div>
        <div class="gc-bar">
          <i class="b-pass" style="width:${(pass ?? 0) * 100}%"></i>
          <i class="b-fail" style="width:${fail * 100 * (g.finished ? 1 : 0) / Math.max(g.finished, 1) * (g.finished / Math.max(g.runs, 1)) * 100 / 100}%"></i>
          <i class="b-rest" style="width:${(rest / Math.max(g.runs, 1)) * 100}%"></i>
        </div></a>`;
    }).join("") || `<div class="empty">no run groups yet</div>`}</div>

    ${Object.keys(tax).length ? `<h2>Failure taxonomy</h2>
    <div class="tax-list">${Object.entries(tax).map(([k, n]) => `
      <div class="tax-row"><span class="tx-name">${esc(k)}</span>
        <span class="tx-bar"><i style="width:${(n / taxMax) * 100}%"></i></span>
        <span class="tx-n">${n}</span></div>`).join("")}</div>` : ""}

    ${(mx.tasks || []).length ? `<h2>Tasks × pairings</h2>
    <p class="page-sub">Mechanical pass rate per cell. Click a cell to drill into its runs — a dash means the pairing never attempted that task.</p>
    <div class="panel heat-wrap"><table class="data heat">
      <tr><th class="heat-task">task</th>${mx.pairings.map(p =>
        `<th class="heat-col"><div>${esc(slug(p.split(" → ")[0]))}</div><div class="dim">→ ${esc(slug(p.split(" → ")[1] || ""))}</div></th>`).join("")}</tr>
      ${mx.tasks.map(t => `<tr>
        <th class="heat-task"><a href="#/runs?task=${encodeURIComponent(t.task_id)}">${esc(t.task_title || t.task_id)}</a>
          <div class="dim sm">${esc(t.task_id)}${t.task_type ? ` · ${esc(t.task_type)}` : ""}</div></th>
        ${mx.pairings.map(p => {
          const c = t.cells[p];
          if (!c) return `<td class="heat-cell"><span class="dim">·</span></td>`;
          const v = c.pass_rate;
          const a = v == null ? 0.06 : 0.08 + 0.72 * v;
          const jm = c.judge_mean != null ? ` · judge ${fmtScore(c.judge_mean)}` : "";
          return `<td class="heat-cell${c.n < 3 ? " thin" : ""}" data-go="#/runs?task=${encodeURIComponent(t.task_id)}"
            title="${esc(t.task_id)} · ${esc(p)} — pass ${v == null ? "—" : fmtPct(v)} over ${c.n} run${c.n === 1 ? "" : "s"}${jm}${c.n < 3 ? " · low-n" : ""}">
            <span class="heat-fill" style="opacity:${a.toFixed(2)}">${v == null ? "—" : fmtPct(v)}</span>
          </td>`;
        }).join("")}</tr>`).join("")}
    </table></div>` : ""}

    <h2>Recent runs</h2>
    <div class="panel"><table class="data">${RUN_HEAD}
      <tbody>${(ov.recent || []).map(runRow).join("") || `<tr><td colspan="9" class="empty">no runs</td></tr>`}</tbody>
    </table></div>`;
  for (const td of $view.querySelectorAll("td.heat-cell[data-go]")) {
    td.style.cursor = "pointer";
    td.addEventListener("click", () => { location.hash = td.dataset.go; });
  }
  bindFlags($view);
}

async function viewRuns(params) {
  const groups = await api("/api/groups");
  await loadFlags();
  const group = params.get("group") || "";
  const status = params.get("status") || "";
  const task = params.get("task") || "";
  const q = params.get("q") || "";

  $view.innerHTML = `
    <h1>Runs</h1>
    <div class="filters">
      <select id="f-group"><option value="">all groups</option>
        ${groups.map(g => `<option ${g.group === group ? "selected" : ""}>${esc(g.group)}</option>`).join("")}</select>
      <input type="search" id="f-q" placeholder="task / model / reason…" value="${esc(q)}">
      <select id="f-status">
        ${["", "running", "finished", "passed", "failed", "cancelled"].map(s =>
          `<option value="${s}" ${s === status ? "selected" : ""}>${s || "any status"}</option>`).join("")}
      </select>
      <input type="search" id="f-task" placeholder="task id…" value="${esc(task)}" style="min-width:150px">
    </div>
    <div class="panel"><table class="data">${RUN_HEAD}<tbody id="runs-body">
      <tr><td colspan="9" class="empty">loading…</td></tr></tbody></table></div>`;

  async function load() {
    const qs = new URLSearchParams();
    const gv = document.getElementById("f-group").value;
    const sv = document.getElementById("f-status").value;
    const tv = document.getElementById("f-task").value;
    const qv = document.getElementById("f-q").value;
    if (gv) qs.set("group", gv);
    if (sv) qs.set("status", sv);
    if (tv) qs.set("task", tv);
    if (qv) qs.set("q", qv);
    const rows = await api("/api/runs?" + qs);
    const body = document.getElementById("runs-body");
    if (body) {
      body.innerHTML =
        rows.map(runRow).join("") || `<tr><td colspan="9" class="empty">no matching runs</td></tr>`;
      bindFlags(body);
    }
  }
  for (const id of ["f-group", "f-status", "f-task", "f-q"]) {
    document.getElementById(id).addEventListener("input", () => load());
  }
  await load();
}

/* ----- run detail ----- */

function timelineHtml(tl, livePhase) {
  const nodes = tl || [];
  const totalMs = nodes.reduce((s, n) => s + (n.latency_ms || 0), 0) || 1;
  return `<div class="phases">${nodes.map(n => {
    const live = livePhase === n.phase;
    const cls = n.errors ? "ph-err" : live ? "ph-live" : "ph-done";
    const share = Math.min(100, Math.round(100 * (n.latency_ms || 0) / totalMs));
    return `<div class="ph-seg ${cls}" title="${esc(n.phase)}: ${n.events} events, ${fmtMoney(n.cost_usd)}, ${fmtMs(n.latency_ms)}${n.errors ? `, ${n.errors} errors` : ""}">
      <div class="ph-name">${esc(n.phase)}${live ? ' <span class="dot dot-run pulse"></span>' : ""}${n.errors ? ` <span class="e">${n.errors}✕</span>` : ""}</div>
      <div class="ph-meta">${n.events} ev · ${fmtMoney(n.cost_usd)} · ${fmtMs(n.latency_ms)}</div>
      <div class="ph-share"><i style="width:${share}%"></i></div>
    </div>`;
  }).join("")}</div>`;
}

function kv(label, val, cls) {
  return `<div class="stat"><span class="s-label">${label}</span><span class="s-val ${cls || ""}">${val}</span></div>`;
}

async function viewRun(runId, params) {
  const tab = params.get("tab") || "artifact";
  const d = await api(`/api/run/${runId}`);
  await loadFlags();
  const m = d.meta, rep = d.report || {};
  const running = m.status === "running";

  const tabs = ["artifact", "events", "calls", "report", "review", "plan", "manifest"];
  $view.innerHTML = `
    <div class="run-head">
      <div class="rh-title">
        <h1>${esc(d.task_title || m.task_id)}</h1>
        ${d.task_title ? `<div class="rh-pair dim">${esc(m.task_id)}${d.task_blurb ? ` — ${esc(d.task_blurb)}` : ""}</div>` : ""}
        <div class="rh-pair">${esc(m.orchestrator)} <span class="arrow">→</span> ${esc(m.worker)}</div>
        <div class="rh-pair dim">${esc(d.group_label || m.run_group || "")}${d.group_label ? ` <span class="dim">(${esc(m.run_group)})</span>` : ""} ${m.replicate ? `· rep ${m.replicate}` : ""} · run ${esc(runId.slice(0, 12))}</div>
      </div>
      <div class="run-stats">
        ${statusChip(m)} ${judgeChip({ ...m, judge_state: d.judge_state, judge_reason: d.judge_reason })}
        ${(() => {
          const js = (d.report && d.report.judges) || {};
          const extra = Object.entries(js).filter(([s]) => s !== (d.report.judge || {}).model);
          return extra.length ? `<span class="chip chip-dim" title="secondary judge verdicts — the primary axis is ${esc((d.report.judge || {}).model || "unknown")}">${extra.map(([s, j]) => `${esc(slug(s))} ${j && j.score != null ? Number(j.score).toFixed(2) : "—"}`).join(" · ")}</span>` : "";
        })()}
        ${kv("cost", fmtMoney(m.total_cost_usd))}
        ${kv("tokens", fmtTok((m.total_input_tokens || 0) + (m.total_output_tokens || 0)))}
        ${kv("time", fmtMs(m.latency_ms))}
        ${m.failure_reason ? kv("failure", esc(m.failure_reason), "") : ""}
        ${flagWidget("run", runId)}
        <a class="btn" href="#/card?kind=run&target=${esc(runId)}">card →</a>
        ${d.cancellable ? `<button class="danger" id="cancel-btn">cancel</button>` : ""}
      </div>
    </div>
    <div class="panel ph-strip" id="tl">${timelineHtml(d.timeline, running ? "running" : null)}</div>
    <div class="tabs">${tabs.map(t =>
      `<button data-tab="${t}" class="${t === tab ? "active" : ""}">${t}</button>`).join("")}</div>
    <div id="tab-body"></div>`;

  for (const b of $view.querySelectorAll(".tabs button")) {
    b.addEventListener("click", () => {
      location.hash = `#/run/${runId}?tab=${b.dataset.tab}`;
    });
  }
  const cancelBtn = document.getElementById("cancel-btn");
  if (cancelBtn) cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    await api(`/api/run/${runId}/cancel`, { method: "POST" });
    viewRun(runId, params);
  });

  renderTab(runId, tab, d, running);
  bindFlags($view);
  if (running) poll(() => refreshRun(runId, tab), 3000);
}

async function refreshRun(runId, tab) {
  try {
    const d = await api(`/api/run/${runId}`);
    const tl = document.getElementById("tl");
    if (tl) tl.innerHTML = timelineHtml(d.timeline, "running");
    if (tab === "events") renderTab(runId, "events", d, true);
    if (d.meta.status !== "running") { stopPolling(); viewRun(runId, new URLSearchParams(`tab=${tab}`)); }
  } catch { /* transient */ }
}

async function renderTab(runId, tab, d, running) {
  const el = document.getElementById("tab-body");
  if (!el) return;

  if (tab === "artifact") {
    const a = d.artifact;
    if (!a) { el.innerHTML = `<div class="empty">no artifact stored${running ? " yet" : ""}</div>`; return; }
    let inner = `<div class="artifact-meta"><span>${esc(a.name)}</span><span>${a.bytes} B</span></div>`;
    if (a.ext === "zip") {
      const members = a.members || [];
      inner += `<div class="member-list">${members.map(mm =>
        `<button data-m="${esc(mm.name)}">${esc(mm.name)} <span class="dim">${mm.bytes}B</span></button>`).join("")}</div>
        <div id="member-view"><div class="empty">pick a member to preview</div></div>`;
      el.innerHTML = inner;
      const memberBtns = [...el.querySelectorAll(".member-list button")];
      for (const b of memberBtns) {
        b.addEventListener("click", () => {
          for (const x of el.querySelectorAll(".member-list button")) x.classList.remove("active");
          b.classList.add("active");
          const name = b.dataset.m;
          const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
          const src = `/api/run/${runId}/artifact/${encodeURIComponent(name)}`;
          const mv = document.getElementById("member-view");
          mv.innerHTML = ext === "html"
            ? `<iframe class="artifact-frame" sandbox="allow-scripts" src="${src}"></iframe>`
            : `<iframe class="artifact-frame" style="background:var(--bg-inset)" sandbox="" src="${src}"></iframe>`;
        });
      }
      if (memberBtns[0]) memberBtns[0].click();
      return;
    }
    const src = `/api/run/${runId}/artifact`;
    if (a.ext === "html") {
      inner += `<iframe class="artifact-frame" sandbox="allow-scripts" src="${src}"></iframe>`;
    } else if (["png", "jpg", "jpeg", "svg", "webp", "gif"].includes(a.ext)) {
      inner += `<img class="artifact-img" src="${src}">`;
    } else {
      inner += `<iframe class="artifact-frame" style="background:var(--bg-inset)" sandbox="" src="${src}"></iframe>`;
    }
    el.innerHTML = inner;
    return;
  }

  if (tab === "events") {
    const live = await api(`/api/run/${runId}/live?after=0`);
    liveCursor = live.next;
    const rows = live.rows, details = live.details;
    el.innerHTML = `<div class="ev-wrap" id="ev-wrap">` +
      rows.map((r, i) => evRow(r, details[i])).join("") +
      `</div>` + (running ? `<div class="dim" style="padding:8px;font-size:11px">streaming…</div>` : "");
    bindEvRows(el, rows, details);
    if (running) poll(async () => {
      try {
        const inc = await api(`/api/run/${runId}/live?after=${liveCursor}`);
        liveCursor = inc.next;
        const wrap = document.getElementById("ev-wrap");
        if (wrap && inc.rows.length) {
          const html = inc.rows.map((r, i) => evRow(r, inc.details[i])).join("");
          wrap.insertAdjacentHTML("beforeend", html);
          bindEvRows(wrap, inc.rows, inc.details, true);
          wrap.scrollTop = wrap.scrollHeight;
        }
      } catch { /* transient */ }
    }, 2000);
    return;
  }

  if (tab === "calls") {
    const calls = d.calls || [];
    el.innerHTML = `<div class="panel"><table class="data"><tr>
      <th>phase</th><th>model</th><th class="t-num">in tok</th><th class="t-num">out tok</th>
      <th class="t-num">cost</th><th class="t-num">ms</th></tr><tbody>` +
      calls.map(c => `<tr>
        <td class="mono">${esc(c.phase || "")}</td>
        <td class="mono">${esc(c.model || "")}</td>
        <td class="t-num">${fmtTok(c.input_tokens)}</td>
        <td class="t-num">${fmtTok(c.output_tokens)}</td>
        <td class="t-num">${fmtMoney(c.cost_usd)}</td>
        <td class="t-num">${fmtMs(c.latency_ms)}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="empty">no calls recorded</td></tr>` +
      `</tbody></table></div>`;
    return;
  }

  const key = { report: "report", review: "review", manifest: "manifest", plan: "plan" }[tab];
  const val = d[key];
  if (val == null) { el.innerHTML = `<div class="empty">no ${tab} recorded</div>`; return; }
  if (tab === "review" && val && typeof val === "object") {
    el.innerHTML = reviewHtml(val);
    return;
  }
  el.innerHTML = `<pre class="block">${esc(typeof val === "string" ? val : JSON.stringify(val, null, 2))}</pre>`;
}

function evRow(r, detail) {
  const [t, type, worker, d] = r;
  const err = /fail|error/i.test(type) ? " ev-err" : "";
  return `<div class="ev-row"><span class="ev-t">${esc(t)}</span>
    <span class="ev-type${err}">${esc(type)}</span>
    <span class="ev-d">${esc(worker)} · ${esc(d)}</span></div>`;
}

function bindEvRows(el, rows, details, append) {
  // click a row to reveal its detail inline under it
  const evRows = el.querySelectorAll(".ev-row");
  const start = append ? evRows.length - rows.length : 0;
  rows.forEach((r, i) => {
    const row = evRows[start + i];
    if (!row) return;
    row.style.cursor = "pointer";
    row.addEventListener("click", () => {
      const next = row.nextElementSibling;
      if (next && next.classList.contains("ev-detail")) { next.remove(); return; }
      row.insertAdjacentHTML("afterend", `<div class="ev-detail">${esc(details[i] || "")}</div>`);
    });
  });
}

function reviewHtml(v) {
  const verdict = v.verdict || v.summary || "";
  const findings = v.findings || [];
  return `<div class="panel panel-pad">
    ${verdict ? `<p style="margin-top:0">${esc(String(verdict))}</p>` : ""}
    ${findings.length ? `<table class="data"><tr><th>severity</th><th>finding</th><th>evidence</th></tr><tbody>` +
      findings.map(f => `<tr>
        <td><span class="chip ${/high|crit/i.test(f.severity || "") ? "chip-fail" : /med/i.test(f.severity || "") ? "chip-warn" : "chip-dim"}">${esc(f.severity || "")}</span></td>
        <td>${esc(f.title || f.finding || "")}</td>
        <td class="dim">${esc(String(f.evidence || "").slice(0, 200))}</td></tr>`).join("") +
      `</tbody></table>` : `<pre class="block">${esc(JSON.stringify(v, null, 2))}</pre>`}
  </div>`;
}

/* ----- compare ----- */

async function viewCompare(params) {
  const groups = await api("/api/groups");
  const a = params.get("a") || (groups[0] && groups[0].group) || "";
  const b = params.get("b") || (groups[1] && groups[1].group) || "";

  $view.innerHTML = `
    <h1>Compare</h1>
    <p class="page-sub">Cell-by-cell delta between two run groups — task × orchestrator × worker.</p>
    <div class="compare-controls">
      <label class="f">baseline<select id="cmp-a">${groups.map(g =>
        `<option ${g.group === a ? "selected" : ""}>${esc(g.group)}</option>`).join("")}</select></label>
      <span class="dim" style="padding-bottom:8px">→</span>
      <label class="f">candidate<select id="cmp-b">${groups.map(g =>
        `<option ${g.group === b ? "selected" : ""}>${esc(g.group)}</option>`).join("")}</select></label>
      <button class="primary" id="cmp-go" style="margin-bottom:1px">compare</button>
    </div>
    <div id="cmp-out"></div>`;

  document.getElementById("cmp-go").addEventListener("click", () => {
    const av = document.getElementById("cmp-a").value, bv = document.getElementById("cmp-b").value;
    location.hash = `#/compare?a=${encodeURIComponent(av)}&b=${encodeURIComponent(bv)}`;
  });
  if (a && b) await renderCompare(a, b);
}

async function renderCompare(a, b) {
  const out = document.getElementById("cmp-out");
  if (!out) return;
  out.innerHTML = `<div class="loading">comparing…</div>`;
  const d = await api(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
  const v = d.verdicts || {};
  const chipFor = x => ({ improved: "chip-pass", regressed: "chip-fail", stable: "chip-dim", "one-sided": "chip-warn" }[x]);

  out.innerHTML = `
    <div class="m">
      ${["improved", "regressed", "stable", "one-sided"].map(k =>
        `<span class="chip ${chipFor(k)}">${k} ${v[k] || 0}</span>`).join("")}
      <span class="chip chip-dim">${esc(a)} ${fmtMoney(d.cost_a)}</span>
      <span class="chip chip-dim">${esc(b)} ${fmtMoney(d.cost_b)}</span>
    </div>
    <div class="panel"><table class="data"><tr>
      <th>task</th><th>orch → worker</th><th class="t-num">n</th>
      <th class="t-num">${esc(a)}</th><th class="t-num">${esc(b)}</th><th class="t-num">Δ</th><th>verdict</th>
    </tr><tbody>` +
    (d.cells || []).map(c => {
      const delta = c.verdict === "one-sided" ? "—" : `${((c.pass_b - c.pass_a) * 100).toFixed(0)}pp`;
      const sign = c.verdict === "one-sided" ? "" : (c.pass_b - c.pass_a >= 0 ? "+" : "");
      return `<tr>
        <td><a href="#/runs?task=${encodeURIComponent(c.task_id)}">${esc(c.task_id)}</a></td>
        <td class="mono">${esc(slug(c.orchestrator))} <span class="dim">→</span> ${esc(slug(c.worker))}</td>
        <td class="t-num">${c.n_a}/${c.n_b}</td>
        <td class="t-num">${fmtPct(c.pass_a)}</td>
        <td class="t-num">${fmtPct(c.pass_b)}</td>
        <td class="t-num cell-delta">${sign}${delta}</td>
        <td><span class="chip ${chipFor(c.verdict)}">${c.verdict}</span></td>
      </tr>`;
    }).join("") + `</tbody></table></div>`;
}

/* ----- leaderboard ----- */

function lbScatter(rows, cardHref, selectedTarget) {
  const pts = rows.filter(r => r.cost_per_pass != null && r.pass_rate != null);
  if (pts.length < 2) return `<div class="empty">need ≥2 metered pairings to plot cost vs outcome</div>`;
  const W = 720, H = 260, padL = 40, padR = 14, padT = 16, padB = 30;
  const xs = pts.map(r => r.cost_per_pass);
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const llo = Math.log10(lo), lhi = Math.log10(hi);
  const px = v => padL + ((Math.log10(v) - llo) / ((lhi - llo) || 1)) * (W - padL - padR);
  const py = v => padT + (1 - v) * (H - padT - padB);
  const rMax = Math.max(...pts.map(r => r.finished || 1));
  return `<svg class="scatter" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="cost per pass versus pass rate, one dot per pairing">
    <line x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}" class="sc-axis"/>
    <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${H - padB}" class="sc-axis"/>
    ${[0, 0.5, 1].map(v => `
      <line x1="${padL}" y1="${py(v)}" x2="${W - padR}" y2="${py(v)}" class="sc-grid"/>
      <text x="${padL - 6}" y="${py(v) + 3}" class="sc-lab" text-anchor="end">${v * 100}%</text>`).join("")}
    <text x="${(W + padL - padR) / 2}" y="${H - 6}" class="sc-lab" text-anchor="middle">cost per pass (log) →</text>
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
          <title>${esc(r.orchestrator)} → ${esc(r.worker)} — pass ${fmtPct(r.pass_rate)}, ${fmtMoney(r.cost_per_pass)}/pass, n=${r.finished}</title></circle>`;
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
          <title>${esc(r.orchestrator)} → ${esc(r.worker)} — pass ${fmtPct(r.pass_rate)}, ${fmtMoney(r.cost_per_pass)}/pass, n=${r.finished}</title></circle>
        <text x="${lx.toFixed(1)}" y="${ty.toFixed(1)}" class="sc-pt-lab" text-anchor="middle">${esc(label)}</text>`;
      }).join("");
    })()}
  </svg>`;
}

async function viewLeaderboard(params) {
  const groups = await api("/api/groups");
  const requestedGroup = params.get("group");
  // A run group is the default story boundary; all-runs remains an explicit choice.
  const group = requestedGroup !== null ? requestedGroup : (groups[0] && groups[0].group) || "";
  const requestedLens = params.get("lens") || "overall";
  const d = await api("/api/pairings" + (group ? `?group=${encodeURIComponent(group)}` : ""));
  const lens = d.lenses.find(item => item.id === requestedLens) || d.lenses[0] || {
    id: "overall", label: "Best overall", description: "No eligible pairing yet.",
    selected_target: "", ranking: [], reason: "", empty_reason: "no pairing has three finished runs yet",
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
          <div class="eyebrow">story scope</div>
          <label class="f">run group
            <select id="lb-group"><option value="">all groups</option>${groups.map(g =>
              `<option value="${esc(g.group)}" ${g.group === group ? "selected" : ""}>${esc(g.label || g.group)} · ${g.runs} runs</option>`).join("")}</select>
          </label>
        </div>
        <div class="story-control-actions">
          <a class="btn" href="/api/shot.png?route=${encodeURIComponent(leaderboardHash().slice(1))}" download>download view</a>
          ${group ? `<a class="btn" href="${cardHref("group", group)}">cohort card</a>` : ""}
        </div>
      </div>
      <div class="lens-strip" role="tablist" aria-label="Story lenses">
        ${d.lenses.map(item => `<a class="lens-tab${item.id === lens.id ? " active" : ""}${item.selected_target ? "" : " empty"}"
          href="${lensHref(item.id)}" role="tab" aria-selected="${item.id === lens.id}">
          <span>${esc(item.label)}</span><small>${item.selected_target ? esc(item.selected_target.replace("|", " → ")) : "no eligible row"}</small>
        </a>`).join("")}
      </div>
      <div class="story-selection">
        <div>
          <div class="eyebrow">${esc(lens.label)}</div>
          <p>${esc(lens.selected_target ? lens.reason : lens.empty_reason)}</p>
        </div>
        ${selectedHref ? `<a class="btn primary" href="${selectedHref}">make selected card →</a>` : `<span class="chip chip-dim">no eligible card yet</span>`}
      </div>
    </div>

    <h2>pairing matrix</h2>
    <div class="panel panel-pad"><table class="data mx">
      <tr><th class="dim">orch ↓ worker →</th>${mx.workers.map(w =>
        `<th class="mx-h">${esc(slug(w))}</th>`).join("")}</tr>
      ${mx.orchestrators.map(o => `<tr>
        <th class="mx-h">${esc(slug(o))}</th>
        ${mx.workers.map(w => {
          const c = cellOf(o, w);
          const v = c && c.runs ? c.pass_rate : null;
          const a = v == null ? 0 : 0.12 + 0.7 * (v / maxPass);
          const target = `${o}|${w}`;
          return `<td class="mx-cell${c && c.low_sample ? " thin" : ""}${target === lens.selected_target ? " selected" : ""}"
            title="${esc(o)} → ${esc(w)}${v != null ? ` · pass ${fmtPct(v)} · n=${c.runs}${c.low_sample ? " · low-n" : ""}` : ""}"
            ${v != null ? `data-go="${cardHref("pairing", target)}"` : ""}>
            ${v != null ? `<span class="mx-fill" style="opacity:${a.toFixed(2)}">${fmtPct(v)}</span>` : `<span class="dim">·</span>`}
          </td>`;
        }).join("")}</tr>`).join("")}
    </table></div>

    <h2>cost vs outcome</h2>
    <p class="page-sub">One dot per pairing — upper-left is cheap and reliable. Dot size = finished runs; faded dots are low-n. Unmetered pairings do not plot.</p>
    <div class="panel panel-pad">${lbScatter(rows, cardHref, lens.selected_target)}</div>

    <h2>pairings · ${esc(lens.label)}</h2>
    <div class="panel"><table class="data"><tr>
      <th>#</th><th>pairing</th><th class="t-num">runs</th>
      <th class="t-num">pass</th><th class="t-num">95% CI</th><th class="t-num">judge</th>
      <th class="t-num">fail</th><th class="t-num">$/pass</th><th class="t-num">cost</th>
      <th class="t-num">p50</th><th class="t-num">p90</th>
      <th>why</th><th></th>
    </tr><tbody>` +
    rows.map(r => {
      const rowRank = !r.low_sample && order.has(r.target) ? ++rank : null;
      return `<tr class="${r.low_sample ? "row-thin" : ""}${r.target === lens.selected_target ? "story-selected" : ""}">
        <td class="dim">${rowRank == null ? "—" : `${rowRank}<span class="dim sm"> / ${ranked}</span>`}</td>
        <td class="mono">${esc(slug(r.orchestrator))} <span class="dim">→</span> ${esc(slug(r.worker))}
          ${r.low_sample ? ' <span class="chip chip-dim">low-n</span>' : ""}${r.target === lens.selected_target ? ' <span class="chip chip-acc">selected</span>' : ""}</td>
        <td class="t-num">${r.finished ?? 0}/${r.runs ?? 0}</td>
        <td class="t-num mech-axis">${fmtPct(r.pass_rate)}</td>
        <td class="t-num dim">${r.pass_ci ? `${Math.round(r.pass_ci[0] * 100)}–${Math.round(r.pass_ci[1] * 100)}%` : "—"}</td>
        <td class="t-num judge-axis" title="${r.judged ? `${r.judged} judged run${r.judged === 1 ? "" : "s"}` : "no judged runs"}">${fmtScore(r.judge_score_median)}${r.judged ? `<span class="dim sm">·${r.judged}</span>` : ""}</td>
        <td class="t-num${(r.failure_rate ?? 0) > 0.15 ? ' e' : ''}">${r.failure_rate != null ? fmtPct(r.failure_rate) : "—"}</td>
        <td class="t-num">${r.cost_per_pass != null ? fmtMoney(r.cost_per_pass) : "—"}</td>
        <td class="t-num">${fmtMoney(r.cost_total)}</td>
        <td class="t-num">${fmtMs(r.duration_median_ms)}</td>
        <td class="t-num">${fmtMs(r.duration_p90_ms)}</td>
        <td class="dim why-cell">${esc(r.why || "—")}</td>
        <td><a class="btn" href="${cardHref("pairing", r.target)}">card</a>
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

/* ----- launch ----- */

async function viewNew() {
  const [tasks, orchs, workers, models] = await Promise.all([
    api("/api/tasks"), api("/api/models?role=orchestrator"),
    api("/api/models?role=worker"), api("/api/models"),
  ]);
  $view.innerHTML = `
    <h1>New run</h1>
    <p class="page-sub">Launch an evaluation. Replicates &gt; 1 creates a run group.</p>
    <div class="panel panel-pad"><form id="launch" class="form-grid">
      <label class="f">task<select name="task" required>${tasks.map(t => `<option>${esc(t)}</option>`).join("")}</select></label>
      <label class="f">orchestrator<select name="orchestrator" required>${orchs.map(m => `<option value="${esc(m.slug)}">${esc(m.slug)}</option>`).join("")}</select></label>
      <label class="f">worker<select name="worker" required>${workers.map(m => `<option value="${esc(m.slug)}">${esc(m.slug)}${m.executor ? " · executor" : ""}</option>`).join("")}</select></label>
      <label class="f">judge (optional)<select name="judge"><option value="">none</option>${models.map(m => `<option value="${esc(m.slug)}"${m.default ? " selected" : ""}>${esc(m.slug)}</option>`).join("")}</select></label>
      <label class="f">replicates<input type="number" name="replicates" value="1" min="1" max="50"></label>
      <label class="f">seed (optional)<input type="number" name="seed" placeholder="auto"></label>
      <label class="f wide check-line"><input type="checkbox" name="dry_run" value="1"> dry run — stub models, no API spend</label>
      <div class="wide form-actions">
        <button type="submit" class="primary">launch</button>
        <span class="form-error" id="launch-err"></span>
      </div>
    </form></div>`;

  document.getElementById("launch").addEventListener("submit", async e => {
    e.preventDefault();
    const err = document.getElementById("launch-err");
    err.textContent = "";
    const f = new FormData(e.target);
    const body = new URLSearchParams();
    for (const [k, v] of f) body.set(k, v);
    try {
      const r = await api("/api/run", { method: "POST", body });
      location.hash = r.run_id ? `#/run/${r.run_id}` : "#/runs";
    } catch (ex) { err.textContent = ex.message; }
  });
}

/* ---------- flags (stateful annotations) ---------- */

let FLAGS = {};

async function loadFlags() {
  try {
    const rows = await api("/api/flags");
    FLAGS = {};
    for (const a of rows) FLAGS[`${a.kind}:${a.target}`] = a;
  } catch { FLAGS = {}; }
}

function flagOf(kind, target) { return FLAGS[`${kind}:${target}`]?.flag || ""; }

function flagWidget(kind, target) {
  const cur = flagOf(kind, target);
  return `<span class="flag-pair" data-kind="${esc(kind)}" data-target="${esc(target)}">
    <button class="flag-btn ${cur === "interesting" ? "f-interesting" : ""}" data-f="interesting" title="flag interesting">★</button>
    <button class="flag-btn ${cur === "not" ? "f-not" : ""}" data-f="not" title="flag not interesting">∅</button>
  </span>`;
}

function bindFlags(root) {
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

/* ---------- X showcase cards ---------- */

function galleryCardHref(card, lens, group) {
  const query = new URLSearchParams({ kind: card.kind, target: card.target, lens });
  if (group) query.set("group", group);
  return `#/card?${query.toString()}`;
}

function galleryCard(card, lens, group) {
  const story = card.story || {};
  const cohort = story.cohort || {};
  const href = galleryCardHref(card, lens, group);
  const proof = story.proof || {};
  const title = card.kind === "group"
    ? (card.group_label || card.target)
    : card.kind === "pairing"
      ? `${slug(card.orchestrator)} → ${slug(card.worker)}`
      : (card.task_title || card.task_id || card.target);
  const context = card.kind === "group"
    ? `${cohort.runs || card.runs || 0} runs · ${cohort.tasks || card.tasks || 0} tasks · ${cohort.pairings || 0} pairings`
    : card.kind === "pairing"
      ? `${card.finished || 0}/${card.runs || 0} finished · ${card.tasks || 0} tasks`
      : `${card.task_id || "run"} · ${card.run_group || "ungrouped"}`;
  const metrics = (story.metrics || []).slice(0, 3);
  const flags = card.flag
    ? `<span class="chip ${card.flag === "interesting" ? "chip-acc" : "chip-fail"}">${card.flag === "not" ? "not interesting" : "flagged"}</span>`
    : "";
  const proofLabel = proof.status === "available" ? "proof ready" : proof.status === "partial" ? "partial proof" : "no stored proof";
  return `<article class="gallery-card panel${card.flag === "interesting" ? " story-selected" : ""}">
    <div class="gallery-card-top"><span class="chip chip-dim">${esc(card.kind)}</span><span class="gallery-card-flags">${flags}${flagWidget(card.kind, card.target)}</span></div>
    <a class="gallery-card-title" href="${href}">${esc(title)}</a>
    <div class="gallery-card-context">${esc(context)}</div>
    <p class="gallery-card-claim">${esc(story.claim || card.verdict_line || "Evidence is still incomplete.")}</p>
    <div class="gallery-card-metrics">${metrics.map(metric => `<span><b>${esc(metric.value)}</b><small>${esc(metric.label)}</small></span>`).join("")}</div>
    <div class="gallery-card-foot"><span class="proof-status ${proof.status || "unavailable"}">${esc(proofLabel)}</span>
      <span class="gallery-card-actions"><a class="btn" href="${href}">open</a><a class="btn" href="/api/shot.png?route=${encodeURIComponent(href.slice(1))}" download>png</a></span>
    </div>
  </article>`;
}

async function viewCards(params) {
  const groups = await api("/api/groups");
  const requestedGroup = params.get("group");
  const group = requestedGroup !== null
    ? requestedGroup
    : (groups[0] && groups[0].group) || "";
  const scope = params.get("scope") || "group";
  const lens = params.get("lens") || "overall";
  const flagged = params.get("flagged") === "1";
  const query = new URLSearchParams({ scope, lens });
  if (group) query.set("group", group);
  if (flagged) query.set("flagged", "1");
  const [catalog, flags] = await Promise.all([
    api(`/api/cards?${query.toString()}`), api("/api/flags"),
  ]);
  FLAGS = {};
  for (const a of flags) FLAGS[`${a.kind}:${a.target}`] = a;
  const lenses = catalog.lenses || [];
  const activeLens = lenses.find(item => item.id === lens) || lenses[0] || { id: lens, label: lens };
  const cards = catalog.cards || [];
  const cardHash = () => {
    const next = new URLSearchParams({ scope, lens: activeLens.id });
    if (group) next.set("group", group);
    if (flagged) next.set("flagged", "1");
    return `#/cards?${next.toString()}`;
  };
  $view.innerHTML = `
    <h1>Cards</h1>
    <p class="page-sub">Choose a cohort, select a lens, and open a shareable card with its real proof.
    Cards are local exports; nothing is posted automatically.</p>
    <div class="gallery-controls panel">
      <div class="gallery-control-row">
        <label class="f">run group<select id="cards-group"><option value="">all groups</option>${groups.map(g =>
          `<option value="${esc(g.group)}" ${g.group === group ? "selected" : ""}>${esc(g.label || g.group)} · ${g.runs} runs</option>`).join("")}</select></label>
        <label class="f">scope<select id="cards-scope">${(catalog.scopes || []).map(item =>
          `<option value="${esc(item.id)}" ${item.id === scope ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label>
        <label class="f">lens<select id="cards-lens">${lenses.map(item =>
          `<option value="${esc(item.id)}" ${item.id === activeLens.id ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label>
        <label class="check-line gallery-flag-filter"><input id="cards-flagged" type="checkbox" ${flagged ? "checked" : ""}> flagged only</label>
      </div>
      <div class="gallery-current"><span class="eyebrow">${esc(activeLens.label)}</span>
        <span>${cards.length} card${cards.length === 1 ? "" : "s"}${group ? ` · ${esc(group)}` : " · all groups"}</span>
        <a class="btn" href="#/leaderboard?${new URLSearchParams({ group, lens: activeLens.id }).toString()}">open leaderboard →</a></div>
    </div>
    <div class="gallery-grid">${cards.map(card => galleryCard(card, activeLens.id, group)).join("") ||
      `<div class="empty gallery-empty">no cards match this view.<br><a href="${cardHash()}">clear filters</a></div>`}</div>`;
  for (const [id, key] of [["cards-group", "group"], ["cards-scope", "scope"], ["cards-lens", "lens"]]) {
    document.getElementById(id).addEventListener("change", e => {
      const next = new URLSearchParams({ scope, lens: activeLens.id });
      const value = e.target.value;
      if (key !== "group" && group) next.set("group", group);
      if (value) next.set(key, value);
      if (flagged) next.set("flagged", "1");
      location.hash = `#/cards?${next.toString()}`;
    });
  }
  document.getElementById("cards-flagged").addEventListener("change", e => {
    const next = new URLSearchParams({ scope, lens: activeLens.id });
    if (group) next.set("group", group);
    if (e.target.checked) next.set("flagged", "1");
    location.hash = `#/cards?${next.toString()}`;
  });
  bindFlags($view);
}

function cardMetric(metric) {
  return `<div class="xc-metric ${esc(metric.tone || "cost")}">
    <span class="xc-metric-label">${esc(metric.label)}</span>
    <strong>${esc(metric.value)}</strong>
    <span class="xc-metric-detail">${esc(metric.detail || "")}</span>
  </div>`;
}

function cardProof(proof, evidence) {
  const resolved = evidence || proof || {};
  const transcript = resolved.transcript || proof?.transcript;
  const artifact = resolved.artifact || proof?.artifact;
  const artifactBytes = artifact && artifact.bytes != null ? Number(artifact.bytes) : null;
  const hasArtifact = !!artifact && artifactBytes !== 0;
  const transcriptText = transcript && transcript.text ? transcript.text : "";
  const proofStatus = resolved.status || proof?.status || "unavailable";
  const runId = resolved.run_id || proof?.run_id;
  const left = transcriptText
    ? `<pre class="xc-proof-code">${esc(transcriptText)}</pre>`
    : `<div class="xc-proof-empty"><span class="proof-mark">—</span><p>No terminal or test transcript stored for this run.</p></div>`;
  let right;
  if (!hasArtifact) {
    right = `<div class="xc-proof-empty"><span class="proof-mark">∅</span><p>No artifact survives in this run. The transcript is the available proof.</p></div>`;
  } else if (artifact.media_type === "image" || artifact.kind === "image") {
    right = `<img class="xc-proof-media" src="${esc(artifact.url)}" alt="${esc(artifact.name)} stored artifact">`;
  } else if (artifact.media_type === "video" || artifact.kind === "video") {
    right = `<video class="xc-proof-media" src="${esc(artifact.url)}" controls muted></video>`;
  } else if (artifact.preview) {
    right = `<pre class="xc-proof-code artifact">${esc(artifact.preview)}</pre>`;
  } else {
    right = `<div class="xc-proof-empty"><span class="proof-mark">↗</span><p><a href="${esc(artifact.url)}">Open ${esc(artifact.name)}</a> in the run inspector.</p></div>`;
  }
  return `<div class="xc-proof-grid">
    <section class="xc-proof-panel">
      <div class="xc-proof-head"><span>terminal / tests</span><span class="proof-status ${proofStatus}">${transcriptText ? "stored" : "unavailable"}</span></div>
      ${left}
    </section>
    <section class="xc-proof-panel">
      <div class="xc-proof-head"><span>code / artifact</span><span class="proof-status ${hasArtifact ? "stored" : "unavailable"}">${hasArtifact ? esc(artifact.name) : "unavailable"}</span></div>
      ${right}
    </section>
  </div>${runId ? `<div class="xc-proof-foot">representative evidence · <a href="#/run/${esc(runId)}">inspect run ${esc(runId.slice(0, 12))}</a></div>` : ""}`;
}

function cardTitle(d, kind) {
  if (kind === "pairing") return `${esc(slug(d.orchestrator))} <span class="arrow">→</span> ${esc(slug(d.worker))}`;
  if (kind === "run") return esc(d.task_title || d.task_id || d.target);
  return esc(d.group_label || d.target);
}

function cardContext(d, kind, cohort) {
  cohort = cohort || {};
  if (kind === "group") {
    const repeats = cohort.repeats ? ` · ${cohort.repeats} repeats` : "";
    return `${cohort.runs ?? 0} runs · ${cohort.finished ?? 0} finished · ${cohort.tasks ?? 0} tasks · ${cohort.pairings ?? 0} pairings · ${cohort.orchestrators ?? 0} orchestrators / ${cohort.workers ?? 0} workers${repeats}`;
  }
  if (kind === "pairing") {
    return `${d.runs} runs · ${d.tasks} tasks · ${(d.groups || []).map(esc).join(", ") || "ungrouped"}`;
  }
  return `${esc(d.task_id || "")} · ${esc(d.group_label || d.run_group || "ungrouped")} · run ${esc((d.target || "").slice(0, 12))}`;
}

async function waitForCardAssets() {
  const images = [...document.querySelectorAll(".xcard img")];
  await Promise.all(images.map(image => image.complete
    ? Promise.resolve()
    : new Promise(resolve => { image.addEventListener("load", resolve, { once: true }); image.addEventListener("error", resolve, { once: true }); })));
  if (document.fonts && document.fonts.ready) await document.fonts.ready;
}

async function viewCard(params) {
  const kind = params.get("kind") || (params.get("run") ? "run" : "group");
  const target = params.get("target") || params.get("run") || params.get("group") || "";
  if (!target) { location.hash = "#/cards"; return; }
  const scopedGroup = params.get("group") || "";
  const lens = params.get("lens") || "overall";
  const query = new URLSearchParams({ kind, target, lens });
  if (scopedGroup) query.set("group", scopedGroup);
  const d = await api(`/api/card?${query.toString()}`);
  const proof = d.story?.proof;
  let evidence = proof;
  if (proof?.run_id) {
    try {
      evidence = await api(`/api/run/${encodeURIComponent(proof.run_id)}/evidence?max_bytes=6000&max_lines=80`);
    } catch {
      evidence = proof;
    }
  }
  await loadFlags();
  const story = d.story || {
    claim: d.verdict_line || "Evidence is still incomplete.",
    caption: d.description || "",
    lens: { id: lens, label: lens, reason: "" },
    cohort: { runs: d.runs ?? 1, finished: d.finished ?? 0, tasks: d.tasks ?? 1, pairings: d.pairings?.length ?? 1, orchestrators: 1, workers: 1 },
    metrics: [],
    signals: [],
    caveats: [],
  };
  const metrics = story.metrics?.length ? story.metrics : [
    { id: "mechanical", label: "mechanical", value: d.passes == null ? "—" : d.passes ? "PASS" : "FAIL", detail: "execution gate", tone: "mech" },
    { id: "judge", label: "judge axis", value: fmtScore(d.judge_score), detail: d.judge_state || "not judged", tone: "judge" },
    { id: "cost", label: "cost", value: fmtMoney(d.cost_usd), detail: "observed spend", tone: "cost" },
  ];
  const signals = story.signals || [];
  const flag = flagOf(kind, target);
  const signalHtml = signals.length
    ? `<div class="xc-signal-row">${signals.map(signal => `<span class="xc-signal ${esc(signal.tone || "info")}">${esc(signal.label)}</span>`).join("")}</div>`
    : `<div class="xc-signal-row"><span class="xc-signal neutral">no escalation signal</span></div>`;
  const caveats = (story.caveats || []).slice(0, 2).join(" · ");
  const inspectHref = kind === "group"
    ? `#/runs?group=${encodeURIComponent(target)}`
    : kind === "pairing"
      ? `#/leaderboard?${new URLSearchParams({ group: scopedGroup, lens }).toString()}`
      : `#/run/${encodeURIComponent(target)}`;

  $view.innerHTML = `<div class="card-stage">
    <div class="card-toolbar">
      ${flagWidget(kind, target)}
      <a class="btn" href="#/cards">all cards</a>
      <a class="btn" href="${inspectHref}">inspect →</a>
      <a class="btn" href="/api/shot.png?route=${encodeURIComponent(location.hash.slice(1))}" download>download png</a>
      <button class="btn" id="copy-context">copy context</button>
      <input id="thread-model" class="thread-model" placeholder="writer model (blank = template)" value="moonshotai/kimi-k2">
      <button class="btn primary" id="btn-thread">write follow-up</button>
      <span class="hint">1200×675 PNG · ${flag === "interesting" ? "flagged story" : "local export"}</span>
    </div>
    <div class="xcard" data-card-scope="${esc(kind)}" data-card-lens="${esc(story.lens?.id || lens)}">
      <div class="xc-top">
        <div class="xc-brand"><span class="mark">◆</span><span class="word">orchestral</span><span class="sub">observatory</span></div>
        <div class="xc-suite">${esc(kind)} card · suite ${esc(d.suite || "—")}</div>
      </div>
      <div class="xc-story-head">
        <div class="xc-story-title">
          <div class="xc-scope">${esc(kind === "group" ? "run group" : kind === "pairing" ? "orchestrator → worker" : "individual run")}</div>
          <div class="xc-title">${cardTitle(d, kind)}</div>
          <div class="xc-sub">${cardContext(d, kind, story.cohort)}</div>
        </div>
        <div class="xc-lens"><span>${esc(story.lens?.label || lens)}</span><small>${esc(story.lens?.reason || "")}</small></div>
      </div>
      <div class="xc-claim">${esc(story.claim)}</div>
      ${signalHtml}
      <div class="xc-metrics">${metrics.map(cardMetric).join("")}</div>
      ${cardProof(proof, evidence)}
      <div class="xc-footer">
        <span>${esc(caveats || "mechanical and judge axes remain separate")}</span>
        <span>${esc(d.suite || "suite —")} · ${esc((story.provenance?.source || "orchestral observatory"))}</span>
      </div>
    </div>
    <div id="thread-panel"></div>
  </div>`;

  document.getElementById("copy-context").addEventListener("click", async e => {
    const button = e.currentTarget;
    const text = story.caption || d.description || story.claim;
    try { await navigator.clipboard?.writeText(text); button.textContent = "copied"; }
    catch { button.textContent = "copy failed"; }
  });
  document.getElementById("btn-thread").addEventListener("click", async e => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "writing…";
    try {
      const body = new URLSearchParams({ kind, target, lens, model: document.getElementById("thread-model").value.trim() });
      if (scopedGroup) body.set("group", scopedGroup);
      const out = await api("/api/thread", { method: "POST", body });
      document.getElementById("thread-panel").innerHTML = `<div class="panel panel-pad thread">
        <h3>follow-up thread ${out.templated ? '<span class="chip chip-dim">template</span>' : `<span class="chip">by ${esc(slug(out.model))}</span>`}</h3>
        ${out.posts.map((p, i) => `<div class="tpost"><span class="tnum">${i + 2}/${out.posts.length + 1}</span><p>${esc(p)}</p><button class="btn copy" data-p="${esc(p)}">copy</button></div>`).join("")}
        ${out.error ? `<div class="dim">writer fell back to template: ${esc(out.error)}</div>` : ""}
      </div>`;
      for (const b of $view.querySelectorAll("button.copy")) {
        b.addEventListener("click", async () => {
          try { await navigator.clipboard?.writeText(b.dataset.p); b.textContent = "copied"; }
          catch { b.textContent = "copy failed"; }
        });
      }
    } catch (ex) {
      document.getElementById("thread-panel").innerHTML = `<div class="panel panel-pad dim">thread failed: ${esc(ex.message)}</div>`;
    }
    btn.disabled = false;
    btn.textContent = "write follow-up";
  });
  bindFlags($view);
  await waitForCardAssets();
}

/* ---------- about ---------- */

async function viewAbout() {
  $view.innerHTML = `
    <h1>What am I looking at?</h1>
    <p class="page-sub">orchestral runs the same task through two models — an
    <b>orchestrator</b> that plans and delegates, and a <b>worker</b> that
    executes — then grades the result twice, on two independent axes.</p>

    <div class="panel panel-pad">
      <h2>The two axes</h2>
      <table class="data">
        <tr><th>axis</th><th>what it means</th><th>how it's graded</th></tr>
        <tr><td><b>mechanical</b></td>
            <td>Did it work? Binary, deterministic, no opinions.</td>
            <td>Code runs its own tests · SQL output is diffed against a
            reference · pages are checked for required elements.</td></tr>
        <tr><td><b>judge</b></td>
            <td>Is it good? A separate model scores quality 0–1.</td>
            <td>A judge model reads the actual artifact (code, HTML, SQL)
            and scores it. <span class="dim">Score ≥ the configured bar
            counts as judge-approved.</span></td></tr>
      </table>
      <p class="dim">They can disagree — a run can pass every check and still
      be mediocre work. Divergence between the axes is the interesting part,
      not noise.</p>
    </div>

    <div class="panel panel-pad">
      <h2>Judge states</h2>
      <table class="data">
        <tr><th>state</th><th>meaning</th></tr>
        <tr><td><span class="chip chip-info">judge 0.83</span></td>
            <td>Scored — the number is the judge's verdict.</td></tr>
        <tr><td><span class="chip chip-dim">not judged</span></td>
            <td>No judge was run for this run (older batches predate the
            judge axis, or it wasn't configured).</td></tr>
        <tr><td><span class="chip chip-warn">judge inconclusive</span></td>
            <td>A verdict was attempted but couldn't be parsed — retryable,
            never counts as a rejection.</td></tr>
        <tr><td><span class="chip chip-dim">not judgeable</span></td>
            <td>No artifact survives to score — nothing to show the judge.</td></tr>
      </table>
    </div>

    <div class="panel panel-pad">
      <h2>Naming</h2>
      <table class="data">
        <tr><th>you see</th><th>it's</th></tr>
        <tr><td class="mono">code-expr-parser</td>
            <td>A task: <code>&lt;type&gt;-&lt;slug&gt;</code>. The type says
            what's being graded (code, sql, html…), the slug names the
            exercise. Titles like "Expression parser" are the same task.</td></tr>
        <tr><td class="mono">grok47-eval</td>
            <td>A run group — one experiment batch. Runs launched together
            share it so they can be compared as a set.</td></tr>
        <tr><td class="mono">0013278fbaa0</td>
            <td>A run id — hash prefix identifying one single attempt.</td></tr>
        <tr><td class="mono">deepseek-v4-pro → gemma-4-31b-it</td>
            <td>A pairing: orchestrator plans → worker executes. The
            leaderboard ranks pairings, not individual models.</td></tr>
      </table>
    </div>

    <div class="panel panel-pad">
      <h2>Reading the numbers</h2>
      <p><b>pass %</b> is the mechanical pass rate. <b>judge</b> is the mean
      judge score or the count approved. <b>CI</b> is the Wilson 95%
      interval — wide on small samples, by design. Rows under the minimum
      sample size are dimmed and sorted below full-evidence rows.
      <b>cost</b> is metered provider spend for that cell.</p>
    </div>`;
}

/* ---------- router ---------- */

async function route() {
  stopPolling();
  const hash = location.hash.slice(1) || "/";
  const [pathQ, query] = hash.split("?");
  const params = new URLSearchParams(query || "");
  const path = pathQ || "/";

  document.querySelectorAll("#nav a").forEach(a => {
    a.classList.toggle("active",
      a.dataset.route === "/" ? path === "/" : path.startsWith(a.dataset.route));
  });

  delete $view.dataset.ready;
  try {
    if (path === "/") await viewOverview();
    else if (path === "/runs") await viewRuns(params);
    else if (path.startsWith("/run/")) await viewRun(path.split("/")[2], params);
    else if (path === "/compare") await viewCompare(params);
    else if (path === "/leaderboard") await viewLeaderboard(params);
    else if (path === "/cards") await viewCards(params);
    else if (path === "/card") await viewCard(params);
    else if (path === "/new") await viewNew();
    else if (path === "/about") await viewAbout();
    else $view.innerHTML = `<div class="empty">unknown view ${esc(path)}</div>`;
  } catch (e) {
    $view.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
  // settled marker for headless captures (/api/shot.png)
  $view.dataset.ready = "1";
}

window.addEventListener("hashchange", route);
route();
