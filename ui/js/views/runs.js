import { $view } from "../dom.js";
import { api } from "../api.js";
import { RUN_HEAD, runRow } from "../chips.js";
import { bindFlags, loadFlags } from "../flags.js";
import { esc } from "../util.js";

export async function viewRuns(params) {
  const groups = await api("/api/groups");
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

  async function load() {
    const qs = new URLSearchParams();
    const gv = document.getElementById("f-group").value;
    const sv = document.getElementById("f-status").value;
    const tv = document.getElementById("f-task").value;
    const qv = document.getElementById("f-q").value;
    if (gv) qs.set("group", gv);
    if (sv) qs.set("status", sv);
    if (tv) qs.set("task", tv);
    if (qv) qs.set("q", qv);
    const rows = await api("/api/runs?" + qs);
    const body = document.getElementById("runs-body");
    if (body) {
      body.innerHTML =
        rows.map(runRow).join("") || `<tr><td colspan="9" class="empty">No matching runs</td></tr>`;
      bindFlags(body);
    }
  }
  for (const id of ["f-group", "f-status", "f-task", "f-q"]) {
    document.getElementById(id).addEventListener("input", () => load());
  }
  await load();
}
