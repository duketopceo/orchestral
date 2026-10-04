import { $jobs } from "./dom.js";
import { data, can } from "./data.js";
import { esc } from "./util.js";
import { start } from "./poller.js";

export async function refreshJobs(signal) {
  const ov = await data.overview({ signal });
  const jobs = (ov.jobs || []).filter(j => j.status === "running" || j.cancellable);
  $jobs.innerHTML = jobs.length
    ? jobs.map(j => `<div class="rail-job"><span class="dot dot-run pulse"></span><span class="jl">${esc(j.label)}</span></div>`).join("")
    : `<div class="rail-empty">No active jobs</div>`;
}

/* The rail is live only where there is a server to ask. */
export function startRail() {
  const rail = document.getElementById("rail-live");
  if (!can("live_stream")) { if (rail) rail.hidden = true; return; }
  start("rail", refreshJobs, { ms: 5000, scope: "app", immediate: true });
}
