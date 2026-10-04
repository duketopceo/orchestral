import * as F from "./format.js";

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// detail views always offer a way back to their section
export function crumb(href, label) {
  return `<a class="crumb" href="${href}">← ${esc(label)}</a>`;
}

// Failure categories (orchestral/taxonomy.py) in plain words. The raw code
// stays available as a tooltip for anyone grepping logs.
const FAILURE_TEXT = {
  rate_limit: "The provider rate-limited the request.",
  auth: "The provider rejected the API key.",
  timeout: "A model call timed out.",
  transport: "A network error interrupted a model call.",
  provider_error: "The model provider returned an error.",
  submitted_job: "A submitted provider job failed.",
  malformed_output: "A model returned output that could not be parsed.",
  validation: "The artifact failed the task's checks.",
  empty_output: "A model returned an empty response.",
  config: "The run was misconfigured (model or task settings).",
  executor_preflight: "The agent executor was not ready to run.",
  executor_exit: "The agent executor exited with an error.",
  executor_timeout: "The agent executor timed out.",
  executor_no_output: "The agent executor produced no output.",
  spawn_failed: "The agent executor could not be started.",
  workspace: "The run workspace could not be prepared.",
  cancelled: "The run was cancelled.",
  unknown: "The run failed for an unrecognized reason.",
};

export function failureText(reason) {
  const code = String(reason || "").replace(/^exception:/, "");
  return FAILURE_TEXT[code] || String(reason || "");
}

// Provider errors surfaced by the thread writer, e.g. "HTTP 402 ...".
export function providerErrorText(raw) {
  const s = String(raw || "");
  if (/\b401\b|\b403\b|unauthori[sz]ed|invalid api key/i.test(s)) return "the API key was rejected";
  if (/\b402\b|insufficient|credit/i.test(s)) return "the account is out of credit";
  if (/\b429\b|rate.?limit/i.test(s)) return "the provider rate-limited the request";
  if (/timeout|timed out/i.test(s)) return "the request timed out";
  if (/no posts/i.test(s)) return "the writer model returned no usable posts";
  return s.split("\n")[0].slice(0, 160) || "the writer model failed";
}

export function fmtEstimate(usd) {
  if (usd == null) return "Unknown";
  return usd === 0 ? F.money(0) : `about ${F.money(usd)}`;
}

// Designed null glyph for HTML cells (text surfaces use F.NULL_GLYPH).
export const NIL = '<span class="nil" role="img" aria-label="no data"></span>';

// A stat line that has no value shows the designed mark, never a text hyphen.
// Pass the already formatted string: the formatters return F.NULL_GLYPH for null.
export function nilOr(text) { return text === F.NULL_GLYPH ? NIL : text; }

export function fmtMoney(v) { return F.money(v); }

// Billed spend (failed runs included). `cost_basis` says whether every call was
// priced by the provider ("billed") or some were scaled from the rate card.
export function billedOf(r) { return r.billed_cost_usd ?? r.total_cost_usd; }

export function basisNote(r) {
  return { billed: "Billed by the provider", mixed: "Part billed, part calibrated from the rate card",
           calibrated: "Calibrated from the rate card" }[r.cost_basis] || "";
}

export function fmtUsdRange(lo, hi) {
  if (lo == null || hi == null) return "Unknown";
  return `${F.money(lo)} to ${F.money(hi)}`;
}

// One key per confirmed action, reused by its retries so a dropped
// connection can never start a second paid run.
export function newIdempotencyKey() {
  // randomUUID needs a secure context; plain http on a LAN host falls back
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `k${Date.now()}${Math.random().toString(16).slice(2)}`;
}

export function spendContextRows(est) {
  if (est.month_to_date_billed_usd == null) return [];
  return [["Spent this month", `$${Number(est.month_to_date_billed_usd).toFixed(2)} of $${Number(est.monthly_cap_usd).toFixed(0)} eval cap (billed spend recorded in this index)`]];
}

export function fmtPct(v) { return F.percent(v); }

export function fmtScore(v) { return F.score(v); }

export function fmtMs(v) { return F.duration(v); }

export function fmtTok(v) { return F.tokens(v); }

export function fmtWhen(iso) {
  if (!iso) return F.NULL_GLYPH;
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
    " " + d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function slug(s) { return s ? F.shortSlug(s) : ""; }

// Judge-axis caveat text: calibrated judges earn "calibrated" language,
// everything else stays advisory — provenance over thresholds.
function calAxis(d) {
  const cal = d.judge_calibration || {};
  const bits = Object.entries(cal).map(([m, s]) =>
    s.calibrated ? `${esc(slug(m))} κ=${Number(s.kappa).toFixed(2)}`
                 : `${esc(slug(m))} uncalibrated (${s.verdict_pairs} pairs)`);
  const calibrated = Object.values(cal).some(s => s.calibrated);
  const axis = calibrated ? "Calibrated semantic axis" : "Advisory semantic axis";
  return bits.length ? `${bits.join(" · ")} · ${axis}` : axis;
}

/* The one URL form for a pairing facet is `orchestrator|worker` (URL-encoded by the caller).
   The matrix payload key `orchestrator \u2192 worker` is accepted as a legacy alias. */
export const pairingParam = (orch, worker) => `${orch}|${worker}`;
export function normPairing(v) {
  const s = String(v || "");
  if (s.includes("|")) return s;
  const i = s.indexOf(" \u2192 ");
  return i < 0 ? s : `${s.slice(0, i)}|${s.slice(i + 3)}`;
}
