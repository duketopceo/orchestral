import * as F from "../format.js";
import { $view } from "../dom.js";
import { can, data, optional } from "../data.js";
import { confirmSpend } from "../confirm.js";
import { bindFlags, flagOf, flagWidget, loadFlags } from "../flags.js";
import { NIL, esc, fmtEstimate, fmtMoney, fmtScore, fmtUsdRange, newIdempotencyKey, providerErrorText, slug, spendContextRows } from "../util.js";

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
    : `<div class="xc-proof-empty"><span class="proof-mark">${NIL}</span><p>No terminal or test transcript stored for this run.</p></div>`;
  let right;
  if (!hasArtifact) {
    right = `<div class="xc-proof-empty"><span class="proof-mark">∅</span><p>No artifact survives in this run. The transcript is the available proof.</p></div>`;
  } else if (artifact.media_type === "image" || artifact.kind === "image") {
    right = `<img class="xc-proof-media" src="${esc(artifact.url)}" alt="${esc(artifact.name)} stored artifact">`;
  } else if (artifact.media_type === "video" || artifact.kind === "video") {
    right = `<video class="xc-proof-media" src="${esc(artifact.url)}" controls muted></video>`;
  } else if (artifact.render_url) {
    right = `<iframe class="xc-proof-frame" sandbox="allow-scripts" src="${esc(artifact.render_url)}" title="rendered artifact ${esc(artifact.render_name || artifact.name)}" loading="lazy"></iframe>`;
  } else if (artifact.preview) {
    right = `<pre class="xc-proof-code artifact">${esc(artifact.preview)}</pre>`;
  } else {
    right = `<div class="xc-proof-empty"><span class="proof-mark">↗</span><p><a href="${esc(artifact.url)}">Open ${esc(artifact.name)}</a> in the run inspector.</p></div>`;
  }
  return `<div class="xc-proof-grid">
    <section class="xc-proof-panel">
      <div class="xc-proof-head"><span>Terminal / Tests</span><span class="proof-status ${proofStatus}">${transcriptText ? "Stored" : "Unavailable"}</span></div>
      ${left}
    </section>
    <section class="xc-proof-panel">
      <div class="xc-proof-head"><span>Code / Artifact</span><span class="proof-status ${hasArtifact ? "stored" : "unavailable"}">${hasArtifact ? esc(artifact.name) : "Unavailable"}</span></div>
      ${right}
    </section>
  </div>${runId ? `<div class="xc-proof-foot">Representative evidence: <a href="#/run/${esc(runId)}">inspect run ${esc(runId.slice(0, 12))}</a></div>` : ""}`;
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

export async function viewCard(params) {
  const kind = params.get("kind") || (params.get("run") ? "run" : "group");
  const target = params.get("target") || params.get("run") || params.get("group") || "";
  if (!target) { location.hash = "#/cards"; return; }
  const scopedGroup = params.get("group") || "";
  const lens = params.get("lens") || "overall";
  const d = await data.card({ kind, target, lens, group: scopedGroup });
  const proof = d.story?.proof;
  let evidence = proof;
  if (proof?.run_id) {
    evidence = (await optional(data.runEvidence(proof.run_id))) ?? proof;
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
    { id: "mechanical", label: "Mechanical", value: d.passes == null ? F.NULL_GLYPH : d.passes ? "PASS" : "FAIL", detail: "Execution gate", tone: "mech" },
    { id: "judge", label: "Judge", value: fmtScore(d.judge_score), detail: d.judge_state || "not judged", tone: "judge" },
    { id: "cost", label: "Cost", value: fmtMoney(d.cost_usd), detail: "Observed spend", tone: "cost" },
  ];
  const signals = story.signals || [];
  const flag = flagOf(kind, target);
  const signalHtml = signals.length
    ? `<div class="xc-signal-row">${signals.map(signal => `<span class="xc-signal ${esc(signal.tone || "info")}">${esc(signal.label)}</span>`).join("")}</div>`
    : `<div class="xc-signal-row"><span class="xc-signal neutral">No escalation signal</span></div>`;
  const caveats = (story.caveats || []).slice(0, 2).join(" · ");
  const inspectHref = kind === "group"
    ? `#/runs?group=${encodeURIComponent(target)}`
    : kind === "pairing"
      ? `#/leaderboard?${new URLSearchParams({ group: scopedGroup, lens }).toString()}`
      : `#/run/${encodeURIComponent(target)}`;

  $view.innerHTML = `<div class="card-stage">
    <div class="card-toolbar">
      ${flagWidget(kind, target)}
      <a class="btn" href="#/cards">All cards</a>
      <a class="btn" href="${inspectHref}">Inspect →</a>
      ${can("png_capture") ? `<a class="btn" href="/api/shot.png?route=${encodeURIComponent(location.hash.slice(1))}" download>Download PNG</a>` : ""}
      <button class="btn" id="copy-context">Copy context</button>
      ${can("thread") ? `<input id="thread-model" class="thread-model" placeholder="Writer model (optional, paid)" aria-label="Writer model slug. Leave blank to use free templates." value="">
      <select id="thread-n" class="thread-n" title="Follow-up posts (the card is post 1)">
        <option value="3" selected>4-post thread</option>
        <option value="4">5-post thread</option>
        <option value="2">3-post thread</option>
      </select>
      <button class="btn primary" id="btn-thread">Write thread</button>` : ""}
      ${can("png_capture") ? `<span class="hint">1200×675 PNG (X-ready) · named download · ${flag === "interesting" ? "Flagged story" : "Local export"}</span>` : ""}
    </div>
    <div class="xcard" data-card-scope="${esc(kind)}" data-card-lens="${esc(story.lens?.id || lens)}">
      <div class="xc-top">
        <div class="xc-brand"><span class="mark">◆</span><span class="word">orchestral</span><span class="sub">observatory</span></div>
        <div class="xc-suite">${esc(kind[0].toUpperCase() + kind.slice(1))} card · suite ${esc(d.suite || F.NULL_GLYPH)}</div>
      </div>
      <div class="xc-story-head">
        <div class="xc-story-title">
          <div class="xc-scope">${esc(kind === "group" ? "Run group" : kind === "pairing" ? "Orchestrator → Worker" : "Individual run")}</div>
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
        <span>${esc(caveats || "Mechanical and judge axes remain separate")}</span>
        <span>${esc(d.suite ? d.suite : "no suite")} · ${esc((story.provenance?.source || "orchestral observatory"))}</span>
      </div>
    </div>
    <div id="thread-panel"></div>
  </div>`;

  document.getElementById("copy-context").addEventListener("click", async e => {
    const button = e.currentTarget;
    const text = story.caption || d.description || story.claim;
    try { await navigator.clipboard?.writeText(text); button.textContent = "Copied"; }
    catch { button.textContent = "Copy failed"; }
  });
  document.getElementById("btn-thread")?.addEventListener("click", async e => {
    const btn = e.currentTarget;
    const panel = document.getElementById("thread-panel");
    const model = document.getElementById("thread-model").value.trim();
    const body = new URLSearchParams({ kind, target, lens, model,
      n: document.getElementById("thread-n").value });
    if (scopedGroup) body.set("group", scopedGroup);
    const confirmWriter = est => confirmSpend({
      title: "Write the thread with a paid model?",
      rows: [
        ["Writer model", est.model || model],
        ["Estimated cost", est.max_usd == null ? "Unknown (model not in models/)" : `up to ${fmtEstimate(est.high_usd ?? est.max_usd).replace("about ", "")}`],
        ["Range", est.max_usd == null ? "Unknown" : fmtUsdRange(0, est.high_usd ?? est.max_usd)],
        ["Estimate basis", est.basis_label || F.NULL_GLYPH],
        ...spendContextRows(est),
      ],
      note: est.caveat || "",
      confirmLabel: "Spend and write",
    });
    btn.disabled = true;
    try {
      if (model) {
        const est = await data.threadEstimate(model);
        if (est.will_spend) {
          if (!(await confirmWriter(est))) {
            panel.innerHTML = `<div class="panel panel-pad dim">Thread not written. Nothing was spent.</div>`;
            return;
          }
          body.set("confirm_spend", "1");
          if (!body.has("idempotency_key")) body.set("idempotency_key", newIdempotencyKey());
        }
      }
      btn.textContent = "Writing…";
      let out;
      try { out = await data.thread({ body }); }
      catch (ex) {
        if (ex.status === 409 && ex.body.needs_confirm && await confirmWriter(ex.body.estimate || {})) {
          body.set("confirm_spend", "1");
          if (!body.has("idempotency_key")) body.set("idempotency_key", newIdempotencyKey());
          out = await data.thread({ body });
        } else throw ex;
      }
      document.getElementById("thread-panel").innerHTML = `<div class="panel panel-pad thread">
        <h3>Follow-up thread ${out.templated ? '<span class="chip chip-dim">Template</span>' : `<span class="chip">By ${esc(slug(out.model))}</span>`}</h3>
        ${out.posts.map((p, i) => `<div class="tpost"><span class="tnum">${i + 2}/${out.posts.length + 1}</span><p>${esc(p)}</p><button class="btn copy" data-p="${esc(p)}">Copy</button></div>`).join("")}
        ${out.error ? `<div class="dim" title="${esc(out.error)}">The writer model failed (${esc(providerErrorText(out.error))}), so these posts come from templates.</div>` : ""}
      </div>`;
      for (const b of $view.querySelectorAll("button.copy")) {
        b.addEventListener("click", async () => {
          try { await navigator.clipboard?.writeText(b.dataset.p); b.textContent = "Copied"; }
          catch { b.textContent = "Copy failed"; }
        });
      }
    } catch (ex) {
      panel.innerHTML = ex.status === 409 && ex.body.needs_confirm
        ? `<div class="panel panel-pad dim">Thread not written. Nothing was spent.</div>`
        : `<div class="panel panel-pad form-error" role="alert">Could not write the thread: ${esc(ex.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Write thread";
    }
  });
  bindFlags($view);
  await waitForCardAssets();
}
