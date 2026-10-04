import { $jobs } from "./dom.js";
import { data, can } from "./data.js";
import { esc, fmtMoney } from "./util.js";
import * as F from "./format.js";
import { start } from "./poller.js";

/* One Activity row per live_runs entry. A stalled row carries data-rest="stalled"
   and a text word; unowned is always said in words. TODO(U7): swap the dot for
   the sprite's `stalled` glyph (shape must differ from running) once U4 lands. */
export function jobRow(j) {
  const stalled = j.state === "stalled";
  const idle = j.idle_s == null ? "" : `${stalled ? "quiet for " : "last event "}${F.duration(j.idle_s * 1000)}`;
  const bits = [
    stalled ? "stalled" : "live",
    j.owned ? "" : "unowned",
    idle,
    j.spend_usd ? fmtMoney(j.spend_usd) : "",
  ].filter(Boolean);
  const name = j.run_id
    ? `<a class="jl" href="#/run/${encodeURIComponent(j.run_id)}">${esc(j.label)}</a>`
    : `<span class="jl">${esc(j.label)}</span>`;
  const abandon = j.abandonable && can("flag_write")
    ? `<button class="abandon" type="button" data-run="${esc(j.run_id)}">Mark abandoned</button>` : "";
  const cancel = j.cancellable && j.run_id && can("cancel")
    ? `<button class="cancel" type="button" data-run="${esc(j.run_id)}">Cancel</button>` : "";
  return `<div class="rail-job${stalled ? " stalled" : ""}" data-run="${esc(j.run_id || "")}" data-state="${esc(j.state)}">
    <span class="dot ${stalled ? "dot-stalled" : "dot-run pulse"}" data-rest="${stalled ? "stalled" : "running"}"></span>
    ${name}
    <span class="rj-meta">${esc(bits.join(" · "))}</span>
    ${abandon}${cancel}
    <span class="rj-err" role="alert" hidden></span>
  </div>`;
}

export async function refreshJobs(signal) {
  const ov = await data.overview({ signal });
  const jobs = ov.jobs || [];
  $jobs.innerHTML = jobs.length
    ? jobs.map(jobRow).join("")
    : `<div class="rail-empty">No active jobs</div>`;
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

$jobs.addEventListener("click", ev => {
  const btn = ev.target.closest("button");
  if (!btn || !btn.dataset.run) return;
  if (btn.classList.contains("abandon")) act(btn, id => data.abandonRun(id));
  else if (btn.classList.contains("cancel")) act(btn, id => data.cancelRun(id));
});

/* The rail is live only where there is a server to ask. */
export function startRail() {
  const rail = document.getElementById("rail-live");
  if (!can("live_stream")) { if (rail) rail.hidden = true; return; }
  start("rail", refreshJobs, { ms: 5000, scope: "app", immediate: true });
}
