import * as F from "../format.js";
import { $view } from "../dom.js";
import { can, data, optional } from "../data.js";
import { confirmSpend } from "../confirm.js";
import { bindFlags, flagOf, flagWidget, loadFlags } from "../flags.js";
import { esc, fmtEstimate, fmtUsdRange, newIdempotencyKey, providerErrorText, slug, spendContextRows } from "../util.js";
import { bindFit, cardModel, fitFrame, programCard, waitForCardAssets } from "../components/program-card.js";

const FIELD_MAX = 1000;
const DL_PROBLEMS = {
  playwright_missing: "Playwright is not installed, so the PNG cannot be made here. Install it, then try again:",
  chromium_missing: "Chromium is not installed for Playwright, so the PNG cannot be made here. Install it, then try again:",
  timeout: "Making the PNG took too long. The card is fine; try again.",
  view_error: "The card view reported an error, so no PNG was made. Reload the card and try again.",
  withheld: "This card is withheld and cannot be published.",
};

/* Capture mode (?capture=1): the bare card at its true 1200x675 size on the
   paper theme, with no shell around it. Playwright screenshots `.xcard`. */
function captureMode() {
  const root = document.documentElement;
  root.dataset.capture = "1";
  root.dataset.theme = "paper";
}

function counted(id, value) {
  return `<div class="pub-field">
    <label for="${id}-text">${id === "alt" ? "Alt text" : "Caption"}</label>
    <textarea id="${id}-text" rows="${id === "alt" ? 6 : 5}" maxlength="${FIELD_MAX}" spellcheck="true">${esc(value)}</textarea>
    <div class="pub-field-foot"><span id="${id}-count" class="pub-count">${value.length} / ${FIELD_MAX}</span>
      ${id === "alt" ? `<span id="alt-error" class="form-error" role="alert"></span>` : ""}
      <button type="button" class="btn" id="copy-${id}">Copy ${id === "alt" ? "alt text" : "caption"}</button></div>
  </div>`;
}

async function download(route, status) {
  status.textContent = "Making the PNG…";
  let res;
  try { res = await fetch(`/api/shot.png?route=${encodeURIComponent(route)}`); }
  catch { status.textContent = "The request did not reach the observatory. Is it still running?"; return; }
  if (!res.ok) {
    let body = {};
    try { body = await res.json(); } catch { /* not JSON */ }
    const lead = DL_PROBLEMS[body.code] || body.error || `The PNG could not be made (status ${res.status}).`;
    status.textContent = body.install ? `${lead} ${body.install}` : lead;
    return;
  }
  const blob = await res.blob();
  const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "orchestral-card.png";
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  status.textContent = `Saved ${name}.`;
}

export async function viewCard(params) {
  const kind = params.get("kind") || (params.get("run") ? "run" : "group");
  const target = params.get("target") || params.get("run") || params.get("group") || "";
  if (!target) { location.hash = "#/cards"; return; }
  const capture = params.get("capture") === "1";
  const scopedGroup = params.get("group") || "";
  const lens = params.get("lens") || "overall";
  const d = await data.card({ kind, target, lens, group: scopedGroup });
  const proof = d.story?.proof;
  let evidence = proof;
  if (proof?.run_id) {
    evidence = (await optional(data.runEvidence(proof.run_id))) ?? proof;
  }
  const model = cardModel(d, kind, lens, evidence);
  if (capture) {
    captureMode();
    $view.innerHTML = programCard(model);
    await waitForCardAssets($view);
    return;
  }
  await loadFlags();
  const flag = flagOf(kind, target);
  const inspectHref = kind === "group"
    ? `#/runs?group=${encodeURIComponent(target)}`
    : kind === "pairing"
      ? `#/leaderboard?${new URLSearchParams({ group: scopedGroup, lens }).toString()}`
      : `#/run/${encodeURIComponent(target)}`;
  const route = `${location.hash.slice(1).replace(/&?capture=1/, "")}`;

  $view.innerHTML = `<div class="card-stage">
    <h1 class="sr-only">${esc(kind[0].toUpperCase() + kind.slice(1))} card: ${esc(target)}</h1>
    <div class="card-toolbar">
      ${flagWidget(kind, target)}
      <a class="btn" href="#/cards">Publish</a>
      <a class="btn" href="${inspectHref}">Inspect</a>
      ${can("thread") ? `<input id="thread-model" class="thread-model" placeholder="Writer model (optional, paid)" aria-label="Writer model slug. Leave blank to use free templates." value="">
      <select id="thread-n" class="thread-n" title="Follow-up posts (the card is post 1)">
        <option value="3" selected>4-post thread</option>
        <option value="4">5-post thread</option>
        <option value="2">3-post thread</option>
      </select>
      <button class="btn primary" id="btn-thread">Write thread</button>` : ""}
    </div>
    <div class="pub-editor">
      <figure class="pub-feed">
        ${fitFrame(programCard(model), { max: 600, id: "feed-preview" })}
        <figcaption class="hint">Feed size, 600 px wide. ${can("png_capture") ? `The PNG is 2400 by 1350 (1200 by 675 at 2x). ${flag === "interesting" ? "Flagged story." : "Local export."}` : "PNGs are made on the machine that holds the runs."}</figcaption>
      </figure>
      <div class="pub-side">
        ${counted("alt", model.alt)}
        ${counted("caption", model.caption)}
        <div class="pub-actions">
          ${can("png_capture") ? `<button type="button" class="btn primary" id="download-png">Download PNG</button>` : ""}
          <span id="dl-status" class="pub-status" role="status" aria-live="polite"></span>
        </div>
      </div>
    </div>
    <div id="thread-panel"></div>
  </div>`;

  const alt = document.getElementById("alt-text");
  const caption = document.getElementById("caption-text");
  const dl = document.getElementById("download-png");
  const syncAlt = () => {
    document.getElementById("alt-count").textContent = `${alt.value.length} / ${FIELD_MAX}`;
    const empty = !alt.value.trim();
    document.getElementById("alt-error").textContent = empty ? "Alt text is required before you publish." : "";
    alt.setAttribute("aria-invalid", empty ? "true" : "false");
    if (dl) dl.disabled = empty;
    document.getElementById("copy-alt").disabled = empty;
  };
  alt.addEventListener("input", syncAlt);
  caption.addEventListener("input", () => {
    document.getElementById("caption-count").textContent = `${caption.value.length} / ${FIELD_MAX}`;
  });
  syncAlt();
  for (const [id, field] of [["copy-alt", alt], ["copy-caption", caption]]) {
    document.getElementById(id).addEventListener("click", async e => {
      const button = e.currentTarget;
      const label = button.textContent;
      try { await navigator.clipboard?.writeText(field.value); button.textContent = "Copied"; }
      catch { button.textContent = "Copy failed"; }
      button.addEventListener("blur", () => { button.textContent = label; }, { once: true });
    });
  }
  if (dl) {
    dl.addEventListener("click", async () => {
      dl.disabled = true;
      try { await download(route, document.getElementById("dl-status")); }
      finally { syncAlt(); }
    });
  }
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
  bindFit($view);
  await waitForCardAssets($view);
}
