/* The one poller. Every live surface registers a task here instead of owning
   a timer. Tasks run on a setTimeout chain (never overlapping), back off on
   failure, stop themselves by returning STOP, and are all paused while the tab
   is hidden. Coming back to the tab polls immediately, so a transition that
   happened while hidden is noticed at once. */
import { isAbort } from "./api.js";
import { setConnection } from "./status.js";

export const STOP = Symbol("poller.stop");
const MAX_DELAY_MS = 30000;
const tasks = new Map();

const hidden = () => typeof document !== "undefined" && document.hidden;

function delayFor(t) {
  return Math.min(MAX_DELAY_MS, t.ms * 2 ** Math.min(t.failures, 5));
}

function schedule(t, delay) {
  clearTimeout(t.timer);
  t.timer = null;
  if (t.stopped || hidden()) return; // resumed by visibilitychange
  t.timer = setTimeout(() => tick(t), delay);
}

async function tick(t) {
  t.timer = null;
  if (t.stopped || hidden()) return;
  t.ctl = new AbortController();
  try {
    const r = await t.fn(t.ctl.signal);
    if (t.stopped) return;
    t.failures = 0;
    setConnection("ok");
    if (r === STOP) { stop(t.key); return; }
  } catch (e) {
    if (t.stopped || isAbort(e)) return;
    t.failures += 1;
    setConnection(t.failures >= 2 ? "offline" : "reconnecting");
  }
  schedule(t, delayFor(t));
}

/* start(key, fn, { ms, scope }): fn(signal) runs every `ms` while the tab is
   visible. scope "route" tasks are dropped on navigation, "app" tasks persist;
   `immediate` runs the first tick at once (still not while the tab is hidden). */
export function start(key, fn, { ms, scope = "route", immediate = false }) {
  stop(key);
  const t = { key, fn, ms, scope, failures: 0, timer: null, ctl: null, stopped: false };
  tasks.set(key, t);
  schedule(t, immediate ? 0 : ms);
  return t;
}

export function stop(key) {
  const t = tasks.get(key);
  if (!t) return;
  t.stopped = true;
  clearTimeout(t.timer);
  t.ctl?.abort();
  tasks.delete(key);
}

export function stopScope(scope = "route") {
  for (const t of [...tasks.values()]) if (t.scope === scope) stop(t.key);
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    for (const t of tasks.values()) {
      if (hidden()) { clearTimeout(t.timer); t.timer = null; t.ctl?.abort(); }
      else schedule(t, 0);
    }
  });
}
