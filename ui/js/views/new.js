import * as F from "../format.js";
import { $view } from "../dom.js";
import { data, optional } from "../data.js";
import { mountCombobox } from "../components/combobox.js";
import { confirmSpend, setSpendButton, spendButtonHtml } from "../components/spend.js";
import { termHelp } from "../components/term.js";
import { announce } from "../shell.js";
import { esc, fmtEstimate, fmtUsdRange, newIdempotencyKey, spendContextRows } from "../util.js";

const DIFFICULTY_ORDER = { easy: 0, standard: 1, medium: 2, hard: 3, expert: 4 };

function taskItems(rows) {
  return [...rows].sort((a, b) => a.type.localeCompare(b.type) || a.family.localeCompare(b.family)
    || (DIFFICULTY_ORDER[a.difficulty] ?? 9) - (DIFFICULTY_ORDER[b.difficulty] ?? 9) || a.id.localeCompare(b.id))
    .map(t => ({
      value: t.id, label: t.id,
      sub: [t.title, t.family].filter(Boolean).join(" · "),
      meta: [t.difficulty, t.expected_cost_usd == null ? "cost unknown" : `about ${F.money(t.expected_cost_usd)}`].filter(Boolean).join(" · "),
      group: t.type,
    }));
}

/* Qualified models for one role, the ones that have run in it first. */
function modelItems(models, catalog, role) {
  const usage = slug => ((catalog.get(slug) || {}).usage || {})[role] || {};
  const ran = m => (usage(m.slug).runs || usage(m.slug).calls || 0) > 0;
  const tag = m => [m.executor ? "executor" : "", (catalog.get(m.slug) || {}).vision ? "vision" : ""].filter(Boolean).join(" · ");
  return [...models].sort((a, b) => Number(ran(b)) - Number(ran(a)))
    .map(m => ({
      value: m.slug, label: m.slug, meta: tag(m),
      group: catalog.size ? (ran(m) ? `Has run as ${role}` : `Not yet run as ${role}`) : "",
    }));
}

export async function viewNew() {
  const [picker, orchs, workers, models, cat] = await Promise.all([
    data.taskPicker(), data.models("orchestrator"), data.models("worker"), data.models(),
    optional(data.modelsCatalog()),
  ]);
  const catalog = new Map(((cat && cat.models) || []).map(m => [m.slug, m]));
  const judgeDefault = (models.find(m => m.default) || {}).slug || "";
  $view.innerHTML = `
    <h1>New run</h1>
    <p class="page-sub">Launch an evaluation. Replicates above 1 create a run group. Dry run is on by default and costs nothing.</p>
    <div class="panel panel-pad"><form id="launch" class="form-grid" novalidate>
      <div class="f wide"><div id="cb-task"></div><p class="f-err" id="err-task"></p></div>
      <div class="f"><div id="cb-orchestrator"></div><p class="f-err" id="err-orchestrator"></p></div>
      <div class="f"><div id="cb-worker"></div><p class="f-err" id="err-worker"></p></div>
      <div class="f"><div class="f-head"><span class="f-title">Judge (optional)</span>${termHelp("judge")}</div><div id="cb-judge"></div></div>
      <div class="f"><div class="f-head"><label for="launch-replicates">Replicates</label>${termHelp("replicates")}</div>
        <input type="number" id="launch-replicates" name="replicates" value="1" min="1" max="50" aria-describedby="err-replicates"><p class="f-err" id="err-replicates"></p></div>
      <label class="f">Seed (optional)<input type="number" name="seed" placeholder="auto"></label>
      <label class="f wide check-line"><input type="checkbox" name="dry_run" value="1" checked aria-describedby="spend-line"> Dry run: stub models, no API calls, no cost</label>
      <p class="wide spend-line" id="spend-line" aria-live="polite"></p>
      <div class="wide form-actions">${spendButtonHtml("launch-btn")}</div>
      <p class="wide form-error" id="launch-err" role="alert"></p>
    </form></div>`;

  const form = document.getElementById("launch");
  const dry = form.elements.dry_run;
  const line = document.getElementById("spend-line");
  const btn = document.getElementById("launch-btn");
  const err = document.getElementById("launch-err");

  const pick = (host, id, name, label, items, opts = {}) => mountCombobox(document.getElementById(host), {
    id, name, label, items, required: !!opts.required, describedBy: opts.required ? `err-${name}` : "",
    placeholder: opts.placeholder || "", value: opts.value,
  });
  const first = items => (items[0] || {}).value;
  const oItems = modelItems(orchs, catalog, "orchestrator");
  const wItems = modelItems(workers, catalog, "worker");
  const jItems = modelItems(models, catalog, "judge");
  const boxes = {
    task: pick("cb-task", "launch-task", "task", "Task", taskItems(picker.tasks), { required: true, placeholder: `Search ${picker.tasks.length} tasks by id, title or type` }),
    orchestrator: pick("cb-orchestrator", "launch-orchestrator", "orchestrator", "Orchestrator", oItems, { required: true, value: first(oItems) }),
    worker: pick("cb-worker", "launch-worker", "worker", "Worker", wItems, { required: true, value: first(wItems) }),
    judge: pick("cb-judge", "launch-judge", "judge", "", jItems, { value: judgeDefault, placeholder: "None" }),
  };
  boxes.judge.input.setAttribute("aria-label", "Judge (optional)");

  /* ---- validation: on blur, and again on submit ---- */
  const RULES = {
    task: () => boxes.task.value ? "" : "Pick a task to run.",
    orchestrator: () => boxes.orchestrator.value ? "" : "Pick an orchestrator.",
    worker: () => boxes.worker.value ? "" : "Pick a worker.",
    replicates: () => {
      const n = Number(form.elements.replicates.value);
      return Number.isInteger(n) && n >= 1 && n <= 50 ? "" : "Replicates must be a whole number from 1 to 50.";
    },
  };
  const fieldOf = name => boxes[name] ? boxes[name].input : form.elements[name];
  function validate(name) {
    const msg = RULES[name]();
    document.getElementById(`err-${name}`).textContent = msg;
    if (msg) fieldOf(name).setAttribute("aria-invalid", "true"); else fieldOf(name).removeAttribute("aria-invalid");
    return msg;
  }
  form.addEventListener("focusout", e => {
    const name = Object.keys(RULES).find(n => fieldOf(n) === e.target);
    if (name) validate(name);
  });

  /* ---- estimate, price segment ---- */
  const formBody = () => {
    const body = new URLSearchParams();
    for (const [k, v] of new FormData(form)) body.set(k, v);
    return body;
  };
  const estimate = () => data.estimate(formBody().toString());
  let seq = 0;
  const refresh = async () => {
    line.classList.toggle("paid", !dry.checked);
    if (dry.checked) {
      seq++;
      setSpendButton(btn, { dry: true });
      line.textContent = "No API spend. Turn off dry run to call real models.";
      return;
    }
    if (RULES.task() || RULES.orchestrator() || RULES.worker() || RULES.replicates()) {
      seq++;
      setSpendButton(btn, { dry: false, status: "ready" });
      line.textContent = "Paid run. Pick a task, orchestrator and worker to see an estimate.";
      return;
    }
    const mine = ++seq;
    setSpendButton(btn, { dry: false, status: "loading" });
    line.textContent = "Paid run. Estimating cost…";
    try {
      const est = await estimate();
      if (mine !== seq) return;
      setSpendButton(btn, { dry: false, estimate: est });
      line.textContent = est.total_usd == null
        ? `Paid run. Estimated cost: unknown. ${est.basis_label} You will be asked to confirm before launch.`
        : `Paid run. Estimated cost: ${fmtUsdRange(est.total_low_usd, est.total_high_usd)} for ${est.replicates} run${est.replicates === 1 ? "" : "s"}. You will be asked to confirm before launch.`;
    } catch (ex) {
      if (mine !== seq) return;
      setSpendButton(btn, { dry: false, status: "error" });
      line.textContent = `Paid run. Could not estimate cost: ${ex.message}`;
    }
  };
  form.addEventListener("change", refresh);
  refresh();

  form.addEventListener("submit", async e => {
    e.preventDefault();
    err.textContent = "";
    const bad = Object.keys(RULES).filter(n => validate(n));
    if (bad.length) {
      err.textContent = document.getElementById(`err-${bad[0]}`).textContent;
      announce(err.textContent);
      fieldOf(bad[0]).focus();
      return;
    }
    const body = formBody();
    const launch = () => data.launch({ body });
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
    const dryInstead = () => { body.set("dry_run", "1"); body.delete("confirm_spend"); body.delete("idempotency_key"); };
    btn.disabled = true;
    try {
      if (!dry.checked) {
        const est = await estimate();
        const choice = await confirmPaid(est);
        if (!choice) throw cancelled();
        if (choice === "dry") dryInstead();
        else { body.set("confirm_spend", "1"); keyed(); }
      }
      let r;
      try { r = await launch(); }
      catch (ex) {
        // estimate changed or a stale form: the server asks again
        if (ex.status === 409 && ex.body.needs_confirm) {
          const choice = await confirmPaid(ex.body.estimate || {});
          if (!choice) throw ex;
          if (choice === "dry") dryInstead();
          else { body.set("confirm_spend", "1"); keyed(); }
          r = await launch();
        } else throw ex;
      }
      location.hash = r.run_id ? `#/run/${r.run_id}` : "#/runs";
    } catch (ex) {
      err.textContent = ex.cancelled || (ex.status === 409 && ex.body.needs_confirm) ? "Launch cancelled. Nothing was spent." : ex.message;
    } finally {
      btn.disabled = false;
    }
  });
}

function cancelled() { return Object.assign(new Error("cancelled"), { cancelled: true }); }
