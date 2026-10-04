import { $view } from "../dom.js";
import { api } from "../api.js";
import { judgeChip, statusChip } from "../chips.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { poll, stopPolling } from "../poller.js";
import { basisNote, billedOf, crumb, esc, failureText, fmtMoney, fmtMs, fmtScore, fmtTok, slug } from "../util.js";

let liveCursor = 0;

function timelineHtml(tl, livePhase) {
  const nodes = tl || [];
  const totalMs = nodes.reduce((s, n) => s + (n.latency_ms || 0), 0) || 1;
  return `<div class="phases">${nodes.map(n => {
    const live = livePhase === n.phase;
    const cls = n.errors ? "ph-err" : live ? "ph-live" : "ph-done";
    const share = Math.min(100, Math.round(100 * (n.latency_ms || 0) / totalMs));
    return `<div class="ph-seg ${cls}" title="${esc(n.phase)}: ${n.events} events, ${fmtMoney(n.cost_usd)}, ${fmtMs(n.latency_ms)}${n.errors ? `, ${n.errors} errors` : ""}">
      <div class="ph-name">${esc(n.phase)}${live ? ' <span class="dot dot-run pulse"></span>' : ""}${n.errors ? ` <span class="e">${n.errors} err</span>` : ""}</div>
      <div class="ph-meta">${n.events} events · ${fmtMoney(n.cost_usd)} · ${fmtMs(n.latency_ms)}</div>
      <div class="ph-share"><i style="width:${share}%"></i></div>
    </div>`;
  }).join("")}</div>`;
}

function kv(label, val, cls) {
  return `<div class="stat"><span class="s-label">${label}</span><span class="s-val ${cls || ""}">${val}</span></div>`;
}

export async function viewRun(runId, params) {
  const tab = params.get("tab") || "artifact";
  const d = await api(`/api/run/${runId}`);
  await loadFlags();
  const m = d.meta, rep = d.report || {};
  const running = m.status === "running";

  const tabs = ["artifact", "events", "calls", "report", "review", "plan", "manifest"];
  const TAB_LABELS = { artifact: "Artifact", events: "Events", calls: "Calls",
    report: "Report", review: "Review", plan: "Plan", manifest: "Manifest" };
  $view.innerHTML = `
    <div class="run-head">
      <div class="rh-title">
        ${crumb("#/runs", "Runs")}
        <h1>${esc(d.task_title || m.task_id)}</h1>
        ${d.task_title ? `<div class="rh-pair dim">${esc(m.task_id)}${d.task_blurb ? `: ${esc(d.task_blurb)}` : ""}</div>` : ""}
        <div class="rh-pair">${esc(m.orchestrator)} <span class="arrow">→</span> ${esc(m.worker)}</div>
        <div class="rh-pair dim">${esc(d.group_label || m.run_group || "")}${d.group_label ? ` <span class="dim">(${esc(m.run_group)})</span>` : ""} ${m.replicate ? `· replicate ${m.replicate}` : ""} · run ${esc(runId.slice(0, 12))}</div>
      </div>
      <div class="run-stats">
        ${statusChip(m)} ${judgeChip({ ...m, judge_state: d.judge_state, judge_reason: d.judge_reason })}
        ${(() => {
          const js = (d.report && d.report.judges) || {};
          const extra = Object.entries(js).filter(([s]) => s !== (d.report.judge || {}).model);
          return extra.length ? `<span class="chip chip-dim" title="Secondary judge verdicts. The primary axis is ${esc((d.report.judge || {}).model || "unknown")}">${extra.map(([s, j]) => `${esc(slug(s))} ${fmtScore(j && j.score)}`).join(" · ")}</span>` : "";
        })()}
        ${kv("Cost", `${fmtMoney(billedOf(m))}${m.cost_basis ? ` <span class="dim sm">${esc(basisNote(m))}</span>` : ""}`)}
        ${kv("Tokens", fmtTok((m.total_input_tokens || 0) + (m.total_output_tokens || 0)))}
        ${kv("Duration", fmtMs(m.latency_ms))}
        ${m.failure_reason ? `<div class="stat" title="${esc(m.failure_reason)}"><span class="s-label">Failure</span><span class="s-val">${esc(failureText(m.failure_reason))}</span></div>` : ""}
        ${flagWidget("run", runId)}
        <a class="btn" href="#/card?kind=run&target=${esc(runId)}">View card</a>
        ${d.cancellable ? `<button class="danger" id="cancel-btn">Cancel</button>` : ""}
      </div>
    </div>
    <div class="panel ph-strip" id="tl">${timelineHtml(d.timeline, running ? "running" : null)}</div>
    <div class="tabs">${tabs.map(t =>
      `<button data-tab="${t}" class="${t === tab ? "active" : ""}">${TAB_LABELS[t]}</button>`).join("")}</div>
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
    if (!a) { el.innerHTML = `<div class="empty">No artifact stored${running ? " yet" : ""}</div>`; return; }
    let inner = `<div class="artifact-meta"><span>${esc(a.name)}</span><span>${a.bytes} B</span></div>`;
    if (a.ext === "zip") {
      const members = a.members || [];
      inner += `<div class="member-list">${members.map(mm =>
        `<button data-m="${esc(mm.name)}">${esc(mm.name)} <span class="dim">${mm.bytes}B</span></button>`).join("")}</div>
        <div id="member-view"><div class="empty">Select a file to preview</div></div>`;
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
      `</div>` + (running ? `<div class="dim" style="padding:8px;font-size:11px">Streaming…</div>` : "");
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
      <th>Phase</th><th>Model</th><th class="t-num">Input tokens</th><th class="t-num">Output tokens</th>
      <th class="t-num">Cost</th><th class="t-num">Latency</th></tr><tbody>` +
      calls.map(c => `<tr>
        <td class="mono">${esc(c.phase || "")}</td>
        <td class="mono">${esc(c.model || "")}</td>
        <td class="t-num">${fmtTok(c.input_tokens)}</td>
        <td class="t-num">${fmtTok(c.output_tokens)}</td>
        <td class="t-num">${fmtMoney(c.cost_usd)}</td>
        <td class="t-num">${fmtMs(c.latency_ms)}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="empty">No calls recorded</td></tr>` +
      `</tbody></table></div>`;
    return;
  }

  const key = { report: "report", review: "review", manifest: "manifest", plan: "plan" }[tab];
  const val = d[key];
  if (val == null) { el.innerHTML = `<div class="empty">No ${key} recorded</div>`; return; }
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
    ${findings.length ? `<table class="data"><tr><th>Severity</th><th>Finding</th><th>Evidence</th></tr><tbody>` +
      findings.map(f => `<tr>
        <td><span class="chip ${/high|crit/i.test(f.severity || "") ? "chip-fail" : /med/i.test(f.severity || "") ? "chip-warn" : "chip-dim"}">${esc(f.severity || "")}</span></td>
        <td>${esc(f.title || f.finding || "")}</td>
        <td class="dim">${esc(String(f.evidence || "").slice(0, 200))}</td></tr>`).join("") +
      `</tbody></table>` : `<pre class="block">${esc(JSON.stringify(v, null, 2))}</pre>`}
  </div>`;
}
