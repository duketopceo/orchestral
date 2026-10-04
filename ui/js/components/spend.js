/* The spend button and the confirm sheet (DESIGN.md 6.10).
   The button carries a price segment ("Launch paid run | est. $0.12") and is
   disabled while the estimate loads. The sheet lists what will run, the
   estimate range and the month's spend; Cancel holds initial focus so Enter
   alone never spends, and "Run dry first" is the free way out. */
import * as F from "../format.js";
import { esc } from "../util.js";

export function spendButtonHtml(id) {
  return `<button type="submit" class="primary spend-btn" id="${esc(id)}">
    <span class="spend-label">Launch dry run</span><span class="spend-price" aria-live="polite"></span></button>`;
}

/* state: { dry, status: "ready"|"loading"|"error", estimate } */
export function setSpendButton(btn, { dry, status = "ready", estimate = null }) {
  btn.querySelector(".spend-label").textContent = dry ? "Launch dry run" : "Launch paid run";
  const price = btn.querySelector(".spend-price");
  btn.dataset.spend = dry ? "free" : "paid";
  if (dry) price.textContent = "no spend";
  else if (status === "loading") price.textContent = "estimating";
  else if (status === "error") price.textContent = "est. unavailable";
  else price.textContent = estimate && estimate.total_usd != null ? `est. ${F.money(estimate.total_usd)}` : "est. unknown";
  btn.disabled = !dry && status === "loading";
}

/* Resolves "confirm", "dry" or null (cancelled). */
export function confirmSpend({ title, rows, note, confirmLabel, dryLabel = "Run dry first" }) {
  return new Promise(resolve => {
    const dlg = document.createElement("dialog");
    dlg.className = "spend-dialog";
    dlg.setAttribute("aria-labelledby", "spend-title");
    dlg.innerHTML = `
      <h2 id="spend-title">${esc(title)}</h2>
      <dl class="spend-rows">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      ${note ? `<p class="spend-note">${esc(note)}</p>` : ""}
      <div class="spend-actions">
        <button type="button" data-act="cancel" autofocus>Cancel</button>
        <button type="button" data-act="dry">${esc(dryLabel)}</button>
        <button type="button" class="primary" data-act="confirm">${esc(confirmLabel)}</button>
      </div>`;
    document.body.appendChild(dlg);
    let result = null;
    dlg.addEventListener("click", e => {
      const act = e.target.closest?.("button")?.dataset.act;
      if (act) { result = act === "cancel" ? null : act; dlg.close(); }
    });
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    dlg.showModal();
    dlg.querySelector('[data-act="cancel"]').focus();
  });
}
