/* Confirm for an action that is not a spend (cancel a run, mark one abandoned).
   Built on the dialog pattern: modal, Escape and backdrop close, Tab trapped,
   focus returns to the opener. "Keep" holds initial focus so Enter alone never
   acts. Resolves true only when the action button is pressed. */
import { esc } from "../util.js";
import { closeDialog, initDialog, openDialog } from "./dialog.js";

export function confirmAction({ title, body, confirmLabel, keepLabel = "Keep it", opener = document.activeElement }) {
  return new Promise(resolve => {
    const dlg = document.createElement("dialog");
    dlg.className = "sheet action-dialog";
    dlg.setAttribute("aria-labelledby", "action-title");
    dlg.innerHTML = `
      <h2 id="action-title">${esc(title)}</h2>
      <p class="action-body">${esc(body)}</p>
      <div class="action-buttons">
        <button type="button" data-act="keep" autofocus>${esc(keepLabel)}</button>
        <button type="button" class="danger" data-act="confirm">${esc(confirmLabel)}</button>
      </div>`;
    document.body.appendChild(dlg);
    initDialog(dlg);
    let result = false;
    dlg.addEventListener("click", e => {
      const act = e.target.closest?.("button")?.dataset.act;
      if (act) { result = act === "confirm"; closeDialog(dlg); }
    });
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    openDialog(dlg, opener);
  });
}
