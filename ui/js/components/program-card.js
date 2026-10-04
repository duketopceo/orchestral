/* The Program note card (DESIGN.md A6). One component renders every share card
   (run group, pairing, run) and its previews, so the series reads as one.

   The card is designed once at 1200x675 and never reflows: in the app it is
   scaled to fit its frame (fitCard), in capture mode (?capture=1) it is drawn at
   its true size. Nothing here fetches; callers pass the card payload. */
import * as F from "../format.js";
import { NIL, esc, fmtMoney, fmtScore, slug } from "../util.js";

export const CARD_W = 1200;
export const CARD_H = 675;

// The ictus mark on the 24-unit master grid (ui/brand/mark.svg): inline, so a
// captured PNG never depends on an image request.
const MARK = `<svg class="pc-mark" viewBox="0 0 24 24" width="28" height="28" fill="none" aria-hidden="true" focusable="false"><path fill="currentColor" d="M2 12h20v2H2zM2 18h20v2H2zM4.38 2.62L2.62 4.38L10.23 12h3.54z"/></svg>`;
// A baton between the orchestrator and the worker: the same 45 degree band as the mark.
const BATON = `<svg class="pc-baton" viewBox="0 0 40 24" width="40" height="24" fill="none" aria-hidden="true" focusable="false"><path fill="currentColor" d="M3 21.4L18.4 6l1.6 1.6L4.6 23z"/><circle cx="21.5" cy="5.5" r="2.5" fill="currentColor"/><path fill="currentColor" d="M25 11h13v2H25z"/></svg>`;

const SCOPE = { group: "Run group", pairing: "Pairing", run: "Run" };

function pct(v) { return Math.round(Number(v) * 100); }

/* Normalize a card payload (and optional stored evidence) into what the card
   draws. Pure: the same payload always gives the same model. */
export function cardModel(d, kind, lens, evidence) {
  const story = d.story || {
    claim: d.verdict_line || "Evidence is still incomplete.",
    caption: d.description || "",
    alt: "",
    lens: { id: lens, label: lens, reason: "" },
    cohort: {}, metrics: [], signals: [], caveats: [], proof: null,
  };
  const metrics = story.metrics?.length ? story.metrics : [
    { id: "mechanical", label: "Mechanical", value: d.passes == null ? F.NULL_GLYPH : d.passes ? "PASS" : "FAIL", detail: "Execution gate", tone: "mech" },
    { id: "judge", label: "Judge", value: fmtScore(d.judge_score), detail: d.judge_state || "not judged", tone: "judge" },
    { id: "cost", label: "Cost", value: fmtMoney(d.cost_usd), detail: "Observed spend", tone: "cost" },
  ];
  const byId = Object.fromEntries(metrics.map(m => [m.id, m]));
  const conf = story.confidence?.mechanical || {};
  const ci = Array.isArray(d.pass_ci) && d.pass_ci.length === 2 ? d.pass_ci : null;
  const aggregate = kind !== "run";
  const finished = Number(d.finished ?? story.cohort?.finished ?? 0);
  const mech = aggregate
    ? {
      id: "mechanical", tone: "mech", label: "Mechanical pass",
      value: d.pass_rate == null ? F.NULL_GLYPH : `${pct(d.pass_rate)}%`,
      detail: `${d.passed ?? 0} of ${finished}`,
      ci, point: d.pass_rate, thin: conf.level === "low" || finished < F.LOW_N_BEST,
    }
    : { ...(byId.mechanical || {}), id: "mechanical", tone: "mech", label: "Mechanical" };
  const judge = { ...(byId.judge || {}), id: "judge", tone: "judge" };
  const cost = { ...(byId.cost || {}), id: "cost", tone: "cost" };
  const caveats = story.caveats || [];
  const caveat = caveats.find(c => !c.startsWith("Proof is one")) || caveats[0]
    || "Mechanical and judge axes are reported separately.";
  const latest = String(story.cohort?.latest || "").slice(0, 10);
  const prov = [
    aggregate ? `n=${finished}` : null,
    d.suite ? `suite ${d.suite}` : (story.provenance?.suite ? `suite ${story.provenance.suite}` : null),
    latest || null,
  ].filter(Boolean).join(" · ");
  return {
    kind, story, claim: story.claim, alt: story.alt || "", caption: story.caption || d.description || "",
    measures: [mech, judge, cost], caveat, prov,
    title: titleOf(d, kind),
    proof: proofOf(story.proof, evidence),
    lensId: story.lens?.id || lens,
  };
}

function titleOf(d, kind) {
  if (kind === "pairing") return { pair: [slug(d.orchestrator), slug(d.worker)] };
  if (kind === "run") return { text: d.task_title || d.task_id || d.target };
  return { text: d.group_label || d.target };
}

/* The proof strip: a transcript excerpt and/or a stored artifact. Returns null
   when neither exists; a published card never draws a panel that says "nothing". */
function proofOf(proof, evidence) {
  const res = evidence || proof || {};
  const transcript = (res.transcript || proof?.transcript)?.text || "";
  const artifact = res.artifact || proof?.artifact;
  const bytes = artifact && artifact.bytes != null ? Number(artifact.bytes) : null;
  let art = null;
  if (artifact && bytes !== 0) {
    if (artifact.media_type === "image" || artifact.kind === "image") art = { image: artifact.url, name: artifact.name };
    else if (artifact.preview) art = { text: artifact.preview, name: artifact.name };
  }
  if (!transcript && !art) return null;
  return { transcript: transcript.slice(0, 900), art, runId: res.run_id || proof?.run_id || "" };
}

function measure(m) {
  let bar = "";
  if (m.id === "mechanical" && m.ci && m.point != null) {
    const lo = pct(m.ci[0]), hi = pct(m.ci[1]);
    bar = `<div class="pc-interval${m.thin ? " thin" : ""}" role="img" aria-label="95 percent interval ${lo} to ${hi} percent">
      <span class="pc-span" style="left:${lo}%;width:${Math.max(hi - lo, 1)}%"></span><span class="pc-point" style="left:${pct(m.point)}%"></span></div>`;
  }
  const value = m.value === F.NULL_GLYPH || m.value == null ? NIL : esc(m.value);
  const detail = m.id === "mechanical" && m.ci
    ? `${esc(m.detail)} · 95% interval ${pct(m.ci[0])} to ${pct(m.ci[1])}%${m.thin ? " · thin sample" : ""}`
    : esc(m.detail || "");
  return `<div class="pc-measure ${esc(m.tone)}">
    <span class="pc-label">${esc(m.label)}</span>
    <strong class="pc-num">${value}</strong>${bar}
    <span class="pc-detail">${detail}</span></div>`;
}

function proofHtml(p) {
  if (!p) return "";
  const panels = [];
  if (p.transcript) {
    panels.push(`<section class="pc-proof-panel"><h3 class="pc-proof-head">Terminal and tests</h3><pre class="pc-code">${esc(p.transcript)}</pre></section>`);
  }
  if (p.art) {
    const body = p.art.image
      ? `<img class="pc-art" src="${esc(p.art.image)}" alt="Stored artifact ${esc(p.art.name)}">`
      : `<pre class="pc-code">${esc(p.art.text)}</pre>`;
    panels.push(`<section class="pc-proof-panel"><h3 class="pc-proof-head">${esc(p.art.name)}</h3>${body}</section>`);
  }
  return `<div class="pc-proof${panels.length === 1 ? " one" : ""}">${panels.join("")}</div>`;
}

function titleHtml(t) {
  if (t.pair) {
    return `<h2 class="pc-title pc-pair"><span>${esc(t.pair[0])}</span>${BATON}<span>${esc(t.pair[1])}</span></h2>`;
  }
  return `<h2 class="pc-title">${esc(t.text)}</h2>`;
}

/* Markup of one card. ``capture`` draws it bare at true size; otherwise the
   caller wraps it in a fit frame (see fitFrame). */
export function programCard(model) {
  return `<article class="xcard pcard${model.proof ? "" : " no-proof"}" data-card-scope="${esc(model.kind)}" data-card-lens="${esc(model.lensId)}">
    <header class="pc-top">
      <div class="pc-brand">${MARK}<span class="pc-word">orchestral</span></div>
      <div class="pc-meta"><span class="pc-scope">${esc(SCOPE[model.kind] || model.kind)}</span></div>
    </header>
    ${titleHtml(model.title)}
    <p class="pc-claim">${esc(model.claim)}</p>
    <div class="pc-measures">${model.measures.map(measure).join("")}</div>
    ${proofHtml(model.proof)}
    <footer class="pc-foot"><span class="pc-caveat">${esc(model.caveat)}</span><span class="pc-prov">${esc(model.prov)}</span></footer>
  </article>`;
}

/* A fit frame: the fixed-size card scaled to the frame's width, never wider
   than ``max`` px. The frame reserves the scaled height, so nothing reflows
   and nothing can clip or push the page wider. */
export function fitFrame(html, { max = 600, id = "", label = "" } = {}) {
  return `<div class="pcard-fit"${id ? ` id="${esc(id)}"` : ""} style="--fit-max:${max}px"${label ? ` data-label="${esc(label)}"` : ""}>${html}</div>`;
}

export function bindFit(root) {
  for (const frame of root.querySelectorAll(".pcard-fit")) {
    const card = frame.querySelector(".xcard");
    if (!card) continue;
    const apply = () => {
      const scale = frame.clientWidth / CARD_W;
      frame.style.setProperty("--s", String(scale));
      frame.style.height = `${Math.round(CARD_H * scale)}px`;
    };
    apply();
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(apply).observe(frame);
  }
}

export async function waitForCardAssets(root = document) {
  const images = [...root.querySelectorAll(".xcard img")];
  await Promise.all(images.map(image => image.complete
    ? Promise.resolve()
    : new Promise(resolve => { image.addEventListener("load", resolve, { once: true }); image.addEventListener("error", resolve, { once: true }); })));
  if (document.fonts) {
    // load the faces the card draws with, then wait: a capture must never
    // happen on the fallback face
    await Promise.all([
      document.fonts.load('600 36px "Instrument Sans"'), document.fonts.load('400 22px "Instrument Sans"'),
      document.fonts.load('400 12px "IBM Plex Mono"'), document.fonts.load('600 56px "IBM Plex Mono"'),
    ].map(p => p.catch(() => null)));
    await document.fonts.ready;
  }
}
