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
  return r.score == null
    ? `<span class="chip chip-dim">judge —</span>`
    : `<span class="chip chip-info">judge ${fmtScore(r.score)}</span>`;
}

function runRow(r) {
  return `<tr>
    <td>${statusChip(r)}</td>
    <td><a href="#/run/${esc(r.run_id)}">${esc(r.task_id)}</a></td>
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
  const ov = await api("/api/overview");
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
        <div class="split"><span class="gc-name">${esc(g.group)}</span>
          <span class="dim">${g.runs} runs</span></div>
        <div class="gc-stats">
          <span>pass <b>${fmtPct(pass)}</b></span>
          <span>judge <b class="judge-axis">${fmtScore(g.score_median)}</b></span>
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

    <h2>Recent runs</h2>
    <div class="panel"><table class="data">${RUN_HEAD}
      <tbody>${(ov.recent || []).map(runRow).join("") || `<tr><td colspan="9" class="empty">no runs</td></tr>`}</tbody>
    </table></div>`;
}

async function viewRuns(params) {
  const groups = await api("/api/groups");
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
    if (body) body.innerHTML =
      rows.map(runRow).join("") || `<tr><td colspan="9" class="empty">no matching runs</td></tr>`;
  }
  for (const id of ["f-group", "f-status", "f-task", "f-q"]) {
    document.getElementById(id).addEventListener("input", () => load());
  }
  await load();
}

/* ----- run detail ----- */

function timelineHtml(tl, livePhase) {
  return `<div class="timeline">${(tl || []).map(n => {
    const live = livePhase === n.phase;
    const cls = n.errors ? "tl-err" : live ? "tl-live" : "tl-done";
    return `<div class="tl-node ${cls}">
      <div class="tl-name">${esc(n.phase)}${live ? ' <span class="dot dot-run pulse"></span>' : ""}</div>
      <div class="tl-meta">${n.events} ev · ${fmtMoney(n.cost_usd)} · ${fmtMs(n.latency_ms)}${n.errors ? ` · <span class="e">${n.errors} err</span>` : ""}</div>
    </div>`;
  }).join("")}</div>`;
}

function kv(label, val, cls) {
  return `<div class="stat"><span class="s-label">${label}</span><span class="s-val ${cls || ""}">${val}</span></div>`;
}

async function viewRun(runId, params) {
  const tab = params.get("tab") || "artifact";
  const d = await api(`/api/run/${runId}`);
  const m = d.meta, rep = d.report || {};
  const running = m.status === "running";

  const tabs = ["artifact", "events", "calls", "report", "review", "plan", "manifest"];
  $view.innerHTML = `
    <div class="run-head">
      <div class="rh-title">
        <h1>${esc(m.task_id)}</h1>
        <div class="rh-pair">${esc(m.orchestrator)} <span class="arrow">→</span> ${esc(m.worker)}</div>
        <div class="rh-pair dim">${esc(m.run_group || "")} ${m.replicate ? `· rep ${m.replicate}` : ""} · ${esc(runId)}</div>
      </div>
      <div class="run-stats">
        ${statusChip(m)} ${judgeChip(m)}
        ${kv("cost", fmtMoney(m.total_cost_usd))}
        ${kv("tokens", fmtTok((m.total_input_tokens || 0) + (m.total_output_tokens || 0)))}
        ${kv("time", fmtMs(m.latency_ms))}
        ${m.failure_reason ? kv("failure", esc(m.failure_reason), "") : ""}
        ${d.cancellable ? `<button class="danger" id="cancel-btn">cancel</button>` : ""}
      </div>
    </div>
    <div class="detail-grid">
      <div class="panel panel-pad" id="tl">${timelineHtml(d.timeline, running ? "running" : null)}</div>
      <div>
        <div class="tabs">${tabs.map(t =>
          `<button data-tab="${t}" class="${t === tab ? "active" : ""}">${t}</button>`).join("")}</div>
        <div id="tab-body"></div>
      </div>
    </div>`;

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
      for (const b of el.querySelectorAll(".member-list button")) {
        b.addEventListener("click", () => {
          for (const x of el.querySelectorAll(".member-list button")) x.classList.remove("active");
          b.classList.add("active");
          const name = b.dataset.m;
          const ext = name.rsplit(".", 1).length > 1 ? name.split(".").pop().toLowerCase() : "";
          const src = `/api/run/${runId}/artifact/${encodeURIComponent(name)}`;
          const mv = document.getElementById("member-view");
          mv.innerHTML = ext === "html"
            ? `<iframe class="artifact-frame" sandbox="allow-scripts" src="${src}"></iframe>`
            : `<iframe class="artifact-frame" style="background:var(--bg-inset)" sandbox="" src="${src}"></iframe>`;
        });
      }
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

async function viewLeaderboard(params) {
  const sort = params.get("sort") || "cost_per_pass";
  const rows = await api("/api/leaderboard?sort=" + encodeURIComponent(sort));
  const sorts = ["cost_per_pass", "pass_rate", "score_median", "cost_median", "duration_median_ms"];
  $view.innerHTML = `
    <h1>Leaderboard</h1>
    <p class="page-sub">Pairings ranked — pass rate is the mechanical axis, median score is the judge axis. Don't confuse them.</p>
    <div class="filters"><label class="f">sort by
      <select id="lb-sort">${sorts.map(s =>
        `<option value="${s}" ${s === sort ? "selected" : ""}>${s}</option>`).join("")}</select></label></div>
    <div class="panel"><table class="data"><tr>
      <th>#</th><th>orchestrator</th><th>worker</th><th class="t-num">runs</th>
      <th class="t-num">pass</th><th class="t-num">judge med</th><th class="t-num">cost</th><th class="t-num">$/pass</th>
    </tr><tbody>` +
    rows.map((r, i) => `<tr>
      <td class="dim">${i + 1}</td>
      <td class="mono">${esc(r.orchestrator)}</td>
      <td class="mono">${esc(r.worker)}</td>
      <td class="t-num">${r.runs ?? "—"}${r.low_sample ? ' <span class="chip chip-dim">low-n</span>' : ""}</td>
      <td class="t-num mech-axis">${fmtPct(r.pass_rate)}</td>
      <td class="t-num judge-axis">${fmtScore(r.score_median)}</td>
      <td class="t-num">${fmtMoney(r.cost_total)}</td>
      <td class="t-num">${r.cost_per_pass != null ? fmtMoney(r.cost_per_pass) : "—"}</td>
    </tr>`).join("") + `</tbody></table></div>`;
  document.getElementById("lb-sort").addEventListener("input", e => {
    location.hash = `#/leaderboard?sort=${e.target.value}`;
  });
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
      <label class="f">orchestrator<select name="orchestrator" required>${orchs.map(m => `<option>${esc(m)}</option>`).join("")}</select></label>
      <label class="f">worker<select name="worker" required>${workers.map(m => `<option>${esc(m)}</option>`).join("")}</select></label>
      <label class="f">judge (optional)<select name="judge"><option value="">none</option>${models.map(m => `<option>${esc(m)}</option>`).join("")}</select></label>
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

  try {
    if (path === "/") await viewOverview();
    else if (path === "/runs") await viewRuns(params);
    else if (path.startsWith("/run/")) await viewRun(path.split("/")[2], params);
    else if (path === "/compare") await viewCompare(params);
    else if (path === "/leaderboard") await viewLeaderboard(params);
    else if (path === "/new") await viewNew();
    else $view.innerHTML = `<div class="empty">unknown view ${esc(path)}</div>`;
  } catch (e) {
    $view.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

window.addEventListener("hashchange", route);
route();
