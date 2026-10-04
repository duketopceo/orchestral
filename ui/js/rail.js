import { $jobs } from "./dom.js";
import { api } from "./api.js";
import { esc } from "./util.js";

export async function refreshJobs() {
  try {
    const ov = await api("/api/overview");
    const jobs = (ov.jobs || []).filter(j => j.status === "running" || j.cancellable);
    $jobs.innerHTML = jobs.length
      ? jobs.map(j => `<div class="rail-job"><span class="dot dot-run pulse"></span><span class="jl">${esc(j.label)}</span></div>`).join("")
      : `<div class="rail-empty">No active jobs</div>`;
  } catch { /* rail is best-effort */ }
}
