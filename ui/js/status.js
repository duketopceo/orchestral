/* The persistent status line (R15): reconnecting or offline on a local server,
   sync age on the hosted mirror, escalating once a snapshot is over 24 hours
   old. Hidden when there is nothing to say. */
import { esc } from "./util.js";

const DAY_MS = 24 * 3600 * 1000;
let el = null;
let meta = null;
let conn = "ok";

export function ago(iso, now = Date.now()) {
  const ms = now - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return null;
  const mins = Math.max(0, Math.round(ms / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs} hour${hrs === 1 ? "" : "s"} ago`;
  const days = Math.round(hrs / 24);
  return `${days} days ago`;
}

export function initStatus(node, loadedMeta) {
  el = node;
  meta = loadedMeta;
  render();
}

export function setConnection(next) {
  if (next === conn) return;
  conn = next;
  render();
}

function signInAgain() {
  return `<button type="button" class="link" data-act="signin">Sign in again</button>`;
}

function render() {
  if (!el || !meta) return;
  const hosted = meta.mode === "hosted";
  let tone = "", html = "";
  if (hosted && conn !== "ok") {
    tone = "warn";
    html = `Could not reach the hosted observatory. You may be offline, or your session expired. ${signInAgain()}`;
  } else if (hosted) {
    const age = meta.synced_at ? ago(meta.synced_at) : null;
    const stale = meta.synced_at && Date.now() - new Date(meta.synced_at).getTime() > DAY_MS;
    tone = stale ? "warn" : "info";
    html = age
      ? `Read-only snapshot, synced ${esc(age)}.${stale ? " Newer runs may exist locally." : ""}`
      : "Read-only snapshot. Sync time unknown.";
  } else if (conn === "reconnecting") {
    tone = "warn";
    html = "Reconnecting to the observatory server.";
  } else if (conn === "offline") {
    tone = "warn";
    html = "Offline. The observatory server is not responding. Still trying; the page updates when it returns.";
  }
  el.hidden = !html;
  el.dataset.tone = tone;
  el.dataset.conn = conn;
  el.innerHTML = html;
}

document.addEventListener("click", e => {
  if (e.target.closest?.('[data-act="signin"]')) location.reload();
});
