import { esc } from "./util.js";

// Modal confirm for anything that bills a provider. Cancel holds initial
// focus so Enter alone never spends; Escape cancels. Resolves true only on
// an explicit activation of the spend button.
export function confirmSpend({ title, rows, note, confirmLabel }) {
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
        <button type="button" class="primary" data-act="confirm">${esc(confirmLabel)}</button>
      </div>`;
    document.body.appendChild(dlg);
    let result = false;
    dlg.addEventListener("click", e => {
      const act = e.target.closest?.("button")?.dataset.act;
      if (act) { result = act === "confirm"; dlg.close(); }
    });
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    dlg.showModal();
    dlg.querySelector('[data-act="cancel"]').focus();
  });
}
