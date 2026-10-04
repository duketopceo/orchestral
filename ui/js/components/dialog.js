/* Dialog pattern: native <dialog> opened modally, Escape closes, a backdrop
   click closes, Tab is trapped inside, and focus returns to what opened it. */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';
const openers = new WeakMap();

export function focusables(dlg) {
  return [...dlg.querySelectorAll(FOCUSABLE)].filter(e => e.offsetParent !== null || e === document.activeElement);
}

export function openDialog(dlg, opener = document.activeElement) {
  if (dlg.open) return;
  openers.set(dlg, opener);
  dlg.showModal();
  const first = dlg.querySelector("[autofocus]") || focusables(dlg)[0];
  first?.focus();
}

export function closeDialog(dlg) {
  if (dlg.open) dlg.close();
}

export function initDialog(dlg) {
  if (dlg.dataset.bound) return;
  dlg.dataset.bound = "1";
  dlg.addEventListener("close", () => {
    const o = openers.get(dlg);
    openers.delete(dlg);
    if (o && o.isConnected && typeof o.focus === "function") o.focus();
  });
  dlg.addEventListener("click", e => {
    if (e.target === dlg || e.target.closest("[data-close]")) closeDialog(dlg);
    else if (e.target.closest("a[href]")) closeDialog(dlg);
  });
  dlg.addEventListener("keydown", e => {
    if (e.key !== "Tab") return;
    const f = focusables(dlg);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || !dlg.contains(document.activeElement))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || !dlg.contains(document.activeElement))) {
      e.preventDefault(); first.focus();
    }
  });
}
