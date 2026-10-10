import { $jobs } from "./dom.js";
import { data, can, isHosted } from "./data.js";
import { esc, fmtMoney } from "./util.js";
import * as F from "./format.js";
import { start } from "./poller.js";
import { liveGlyph } from "./components/states.js";
import { announce } from "./shell.js";

/* Activity: one row per live_runs entry, in the rail and in the More sheet.
   Live shows the baton ring (a static dot plus the word "live" under reduced
   motion); stalled shows the `stalled` glyph, which differs from running by
   shape. Unowned rows say so in words and never offer Cancel. On the hosted
   mirror nothing is live, so rows say "Running at last sync" and never move. */
export const UNOWNED_NOTE = "Started from the CLI. Stop it there.";
export const HOSTED_NOTE = "Running at last sync";

export function jobRow(j, { hosted = false } = {}) {
  const stalled = j.state === "stalled" || j.state === "lost";
  const lost = j.state === "lost";
  const idle = j.idle_s == null || hosted ? "" : `${stalled ? "quiet for " : "last event "}${F.duration(j.idle_s * 1000)}`;
  const bits = [
    hosted ? HOSTED_NOTE : lost ? "lost" : stalled ? "stalled" : "live",
    idle,
    j.spend_usd ? fmtMoney(j.spend_usd) : "",
  ].filter(Boolean);
  const glyph = liveGlyph({ stalled, still: hosted });
  const name = j.run_id
    ? `<a class="jl" href="#/run/${encodeURIComponent(j.run_id)}">${esc(j.label)}</a>`
    : `<span class="jl">${esc(j.label)}</span>`;
  const note = !hosted && !j.owned ? `<span class="rj-note">${UNOWNED_NOTE}</span>` : "";
  const abandon = !hosted && j.abandonable && can("flag_write")
    ? `<button class="abandon" type="button" data-run="${esc(j.run_id)}">Mark abandoned</button>` : "";
  const cancel = !hosted && j.cancellable && j.run_id && can("cancel")
    ? `<button class="cancel" type="button" data-run="${esc(j.run_id)}">Cancel</button>` : "";
  return `<div class="rail-job${stalled ? " stalled" : ""}" data-run="${esc(j.run_id || "")}" data-state="${esc(j.state)}">
    ${glyph}
    ${name}
    <span class="rj-meta">${esc(bits.join(" · "))}</span>
    ${note}${abandon}${cancel}
    <span class="rj-err" role="alert" hidden></span>
  </div>`;
}

const hosts = () => [$jobs, document.getElementById("more-jobs")].filter(Boolean);
let known = new Map(); // run_id -> state, so only phase-level changes are announced

function announceChanges(jobs) {
  const next = new Map(jobs.map(j => [j.run_id, j.state]));
  for (const j of jobs) {
    if (known.has(j.run_id) && known.get(j.run_id) !== j.state && j.state === "stalled")
      announce(`${j.label} has gone quiet.`);
  }
  for (const [id] of known) {
    if (!next.has(id)) announce("A run left the activity list.");
  }
  known = next;
}

export async function refreshJobs(signal) {
  const ov = await data.overview({ signal });
  const jobs = ov.jobs || [];
  const hosted = isHosted();
  const html = jobs.length
    ? jobs.map(j => jobRow(j, { hosted })).join("")
    : `<div class="rail-empty">No active jobs</div>`;
  for (const h of hosts()) h.innerHTML = html;
  if (!hosted) announceChanges(jobs);
  document.getElementById("rail")?.toggleAttribute("data-has-live", jobs.some(j => j.state !== "stalled"));
}

async function act(btn, call) {
  const row = btn.closest(".rail-job");
  btn.disabled = true;
  try {
    await call(btn.dataset.run);
    await refreshJobs();
  } catch (e) {
    const err = row.querySelector(".rj-err");
    err.textContent = e.message || "That did not work. Try again.";
    err.hidden = false;
    btn.disabled = false;
  }
}

for (const h of hosts()) {
  h.addEventListener("click", ev => {
    const btn = ev.target.closest("button");
    if (!btn || !btn.dataset.run) return;
    if (btn.classList.contains("abandon")) act(btn, id => data.abandonRun(id));
    else if (btn.classList.contains("cancel")) act(btn, id => data.cancelRun(id));
  });
}

/* The rail is live only where there is a server to ask. The hosted mirror
   shows one static read of what was running at the last sync. */
export function startRail() {
  const rail = document.getElementById("rail-live");
  if (can("live_stream")) {
    start("rail", refreshJobs, { ms: 5000, scope: "app", immediate: true });
  } else if (isHosted()) {
    refreshJobs().catch(() => { if (rail) rail.hidden = true; });
  } else if (rail) {
    rail.hidden = true;
  }
}
