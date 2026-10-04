import * as F from "../format.js";
import { $view } from "../dom.js";
import { api } from "../api.js";
import { confirmSpend } from "../confirm.js";
import { esc, fmtEstimate, fmtUsdRange, newIdempotencyKey, spendContextRows } from "../util.js";

export async function viewNew() {
  const [tasks, orchs, workers, models] = await Promise.all([
    api("/api/tasks"), api("/api/models?role=orchestrator"),
    api("/api/models?role=worker"), api("/api/models"),
  ]);
  $view.innerHTML = `
    <h1>New Run</h1>
    <p class="page-sub">Launch an evaluation. Replicates &gt; 1 creates a run group.</p>
    <div class="panel panel-pad"><form id="launch" class="form-grid">
      <label class="f">Task<select name="task" required>${tasks.map(t => `<option>${esc(t)}</option>`).join("")}</select></label>
      <label class="f">Orchestrator<select name="orchestrator" required>${orchs.map(m => `<option value="${esc(m.slug)}">${esc(m.slug)}</option>`).join("")}</select></label>
      <label class="f">Worker<select name="worker" required>${workers.map(m => `<option value="${esc(m.slug)}">${esc(m.slug)}${m.executor ? " · executor" : ""}</option>`).join("")}</select></label>
      <label class="f">Judge (optional)<select name="judge"><option value="">None</option>${models.map(m => `<option value="${esc(m.slug)}"${m.default ? " selected" : ""}>${esc(m.slug)}</option>`).join("")}</select></label>
      <label class="f">Replicates<input type="number" name="replicates" value="1" min="1" max="50"></label>
      <label class="f">Seed (optional)<input type="number" name="seed" placeholder="auto"></label>
      <label class="f wide check-line"><input type="checkbox" name="dry_run" value="1" checked aria-describedby="spend-line"> Dry run: stub models, no API calls, no cost</label>
      <p class="wide spend-line" id="spend-line" aria-live="polite"></p>
      <div class="wide form-actions">
        <button type="submit" class="primary" id="launch-btn">Launch dry run</button>
      </div>
      <p class="wide form-error" id="launch-err" role="alert"></p>
    </form></div>`;

  const form = document.getElementById("launch");
  const dry = form.elements.dry_run;
  const line = document.getElementById("spend-line");
  const btn = document.getElementById("launch-btn");
  const formBody = () => {
    const body = new URLSearchParams();
    for (const [k, v] of new FormData(form)) body.set(k, v);
    return body;
  };
  const estimate = () => api(`/api/estimate?${formBody().toString()}`);
  let seq = 0;
  const refresh = async () => {
    btn.textContent = dry.checked ? "Launch dry run" : "Launch paid run";
    line.classList.toggle("paid", !dry.checked);
    if (dry.checked) { seq++; btn.disabled = false; line.textContent = "No API spend. Turn off dry run to call real models."; return; }
    const mine = ++seq;
    btn.disabled = true;
    line.textContent = "Paid run. Estimating cost…";
    try {
      const est = await estimate();
      if (mine !== seq) return;
      line.textContent = est.total_usd == null
        ? `Paid run. Estimated cost: unknown. ${est.basis_label} You will be asked to confirm before launch.`
        : `Paid run. Estimated cost: ${fmtUsdRange(est.total_low_usd, est.total_high_usd)} for ${est.replicates} run${est.replicates === 1 ? "" : "s"}. You will be asked to confirm before launch.`;
    } catch (ex) {
      if (mine === seq) line.textContent = `Paid run. Could not estimate cost: ${ex.message}`;
    } finally {
      if (mine === seq) btn.disabled = false;
    }
  };
  form.addEventListener("change", refresh);
  refresh();

  form.addEventListener("submit", async e => {
    e.preventDefault();
    const err = document.getElementById("launch-err");
    err.textContent = "";
    const body = formBody();
    const launch = () => api("/api/run", { method: "POST", body });
    // set once the person confirms; every retry of this launch carries it
    const keyed = () => { if (!body.has("idempotency_key")) body.set("idempotency_key", newIdempotencyKey()); };
    const confirmPaid = async est => confirmSpend({
      title: "Launch a paid run?",
      rows: [
        ["Task", body.get("task")],
        ["Pairing", `${body.get("orchestrator")} → ${body.get("worker")}`],
        ["Judge", body.get("judge") || "None"],
        ["Runs", String(est.replicates ?? body.get("replicates") ?? 1)],
        ["Estimated cost", est.total_usd == null ? "Unknown" : fmtEstimate(est.total_usd)],
        ["Range", fmtUsdRange(est.total_low_usd, est.total_high_usd)],
        ["Estimate basis", est.basis_label || F.NULL_GLYPH],
        ...spendContextRows(est),
      ],
      note: est.caveat || "",
      confirmLabel: "Spend and launch",
    });
    btn.disabled = true;
    try {
      if (!dry.checked) {
        const est = await estimate();
        if (!(await confirmPaid(est))) { btn.disabled = false; return; }
        body.set("confirm_spend", "1");
        keyed();
      }
      let r;
      try { r = await launch(); }
      catch (ex) {
        // estimate changed or a stale form: the server asks again
        if (ex.status === 409 && ex.body.needs_confirm && await confirmPaid(ex.body.estimate || {})) {
          body.set("confirm_spend", "1");
          keyed();
          r = await launch();
        } else throw ex;
      }
      location.hash = r.run_id ? `#/run/${r.run_id}` : "#/runs";
    } catch (ex) {
      err.textContent = ex.status === 409 && ex.body.needs_confirm ? "Launch cancelled. Nothing was spent." : ex.message;
    } finally {
      btn.disabled = false;
    }
  });
}
