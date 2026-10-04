import { $view } from "../dom.js";
import { STALE, can, data, latest } from "../data.js";
import { stateHtml } from "../components/states.js";
import { isAbort } from "../api.js";
import { RUN_HEAD, runRow } from "../chips.js";
import { bindFlags, loadFlags } from "../flags.js";
import { esc } from "../util.js";

export async function viewRuns(params) {
  const groups = await data.groups();
  await loadFlags();
  const group = params.get("group") || "";
  const status = params.get("status") || "";
  const task = params.get("task") || "";
  const q = params.get("q") || "";

  $view.innerHTML = `
    <h1>Runs</h1>
    <div class="filters">
      <select id="f-group"><option value="">All groups</option>
        ${groups.map(g => `<option ${g.group === group ? "selected" : ""}>${esc(g.group)}</option>`).join("")}</select>
      <input type="search" id="f-q" placeholder="Task, model, or reason…" value="${esc(q)}">
      <select id="f-status">
        ${["", "running", "finished", "passed", "failed", "cancelled"].map(s =>
          `<option value="${s}" ${s === status ? "selected" : ""}>${s ? s[0].toUpperCase() + s.slice(1) : "Any status"}</option>`).join("")}
      </select>
      <input type="search" id="f-task" placeholder="Task ID…" value="${esc(task)}" style="min-width:150px">
    </div>
    <div class="panel"><table class="data">${RUN_HEAD}<tbody id="runs-body">
      <tr><td colspan="9" class="empty">Loading…</td></tr></tbody></table></div>`;

  const newest = latest();
  async function load(initial) {
    const filters = {
      group: document.getElementById("f-group").value,
      status: document.getElementById("f-status").value,
      task: document.getElementById("f-task").value,
      q: document.getElementById("f-q").value,
    };
    let rows;
    try { rows = await newest(signal => data.runs(filters, { signal })); }
    catch (e) {
      if (initial || isAbort(e)) throw e;
      const failed = document.getElementById("runs-body");
      if (failed) failed.innerHTML = `<tr><td colspan="9" class="empty" role="alert">${esc(e.message)}</td></tr>`;
      return;
    }
    if (rows === STALE) return;
    const body = document.getElementById("runs-body");
    if (body) {
      const filtered = Object.values(filters).some(Boolean);
      const none = filtered
        ? stateHtml("nomatch", { title: "No runs match these filters", body: "Change a filter, or clear them all.",
            action: { label: "Clear filters", href: "#/runs" } })
        : stateHtml("empty", { title: "No runs yet", body: "Runs appear here as soon as one is recorded.",
            action: can("launch") ? { label: "Start a run", href: "#/new" } : { label: "Read the guide", href: "#/about" } });
      body.innerHTML = rows.map(runRow).join("") || `<tr><td colspan="9">${none}</td></tr>`;
      bindFlags(body);
    }
  }
  for (const id of ["f-group", "f-status", "f-task", "f-q"]) {
    document.getElementById(id).addEventListener("input", () => load());
  }
  await load(true);
}
