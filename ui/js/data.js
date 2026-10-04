/* Data layer: the capabilities document and the adapter that fetches through it.
   `data.<resource>()` is the only way views read; each resource has one call.
   The adapter is picked from meta.mode (never from an HTTP status): the local
   adapter queries the server, the hosted adapter reads the static key tree that
   orchestral/web/snapshot.py writes (served by the Worker as /api/<name>) and
   does filtering client-side. */
import { ApiError, api, isAbort } from "./api.js";

const DEFAULT_META = {
  mode: "local", synced_at: null, source_commit: null,
  capabilities: { launch: true, cancel: true, flag_write: true, thread: true, png_capture: true, live_stream: true },
  low_n: { cell: 3, best: 10 },
};

let META = DEFAULT_META;
let ACTIVE = null; // the AbortSignal of the render in flight
let adapter = null;

export const meta = () => META;
export const mode = () => META.mode;
export const can = cap => !!META.capabilities?.[cap];
export const isHosted = () => META.mode === "hosted";

/* The router sets the signal of the render in flight; every read that is not
   given its own signal inherits it, so a superseded render's responses are
   dropped by the browser rather than painted. */
export function setSignal(signal) { ACTIVE = signal; }
const sig = opts => opts?.signal ?? ACTIVE ?? undefined;

/* Percent-encode a name inside a snapshot key exactly as snapshot.py does
   (encodeURIComponent plus the characters it leaves bare). */
export function keyEnc(name) {
  return encodeURIComponent(String(name)).replace(/[!'()*]/g, c => "%" + c.charCodeAt(0).toString(16).toUpperCase());
}

/* ---------- meta ---------- */

export async function loadMeta() {
  for (const url of ["/api/meta", "/api/meta.json"]) {
    try {
      const m = await api(url);
      if (m && (m.mode === "local" || m.mode === "hosted")) {
        META = { ...DEFAULT_META, ...m, capabilities: { ...DEFAULT_META.capabilities, ...m.capabilities } };
        break;
      }
    } catch { /* try the next key */ }
  }
  adapter = META.mode === "hosted" ? hostedAdapter() : localAdapter();
  return META;
}

/* ---------- local adapter ---------- */

const qs = obj => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== undefined && v !== null && v !== "") q.set(k, v);
  const s = q.toString();
  return s ? `?${s}` : "";
};

export function localAdapter() {
  const get = (path, opts) => api(path, { signal: sig(opts) });
  // Writes never inherit the render signal: navigating away must not abort a paid launch in flight.
  const post = (path, init) => api(path, { ...init, method: "POST" });
  return {
    mode: "local",
    overview: o => get("/api/overview", o),
    runs: (f = {}, o) => get(`/api/runs${qs({ group: f.group, status: f.status, task: f.task, q: f.q })}`, o),
    groups: o => get("/api/groups", o),
    matrix: o => get("/api/matrix", o),
    experiments: o => get("/api/experiments", o),
    experiment: (name, o) => get(`/api/experiment${qs({ matrix: name })}`, o),
    run: (id, o) => get(`/api/run/${encodeURIComponent(id)}`, o),
    runLive: (id, after = 0, o) => get(`/api/run/${encodeURIComponent(id)}/live?after=${after}`, o),
    runEvidence: (id, { maxBytes = 6000, maxLines = 80 } = {}, o) =>
      get(`/api/run/${encodeURIComponent(id)}/evidence?max_bytes=${maxBytes}&max_lines=${maxLines}`, o),
    compare: (a, b, o) => get(`/api/compare${qs({ a, b })}`, o),
    leaderboard: (sort, o) => get(`/api/leaderboard${qs({ sort })}`, o),
    pairings: (group, o) => get(`/api/pairings${qs({ group })}`, o),
    cards: (f = {}, o) => get(`/api/cards${qs({ group: f.group, scope: f.scope, lens: f.lens, flagged: f.flagged ? "1" : "" })}`, o),
    card: (f, o) => get(`/api/card${qs({ kind: f.kind, target: f.target, lens: f.lens, group: f.group })}`, o),
    flags: o => get("/api/flags", o),
    modelsCatalog: o => get("/api/models-catalog", o),
    tasks: o => get("/api/tasks", o),
    models: (role, o) => get(`/api/models${qs({ role })}`, o),
    estimate: (query, o) => get(`/api/estimate?${query}`, o),
    threadEstimate: (model, o) => get(`/api/thread-estimate?${new URLSearchParams({ model })}`, o),
    // writes: local only
    cancelRun: id => post(`/api/run/${encodeURIComponent(id)}/cancel`),
    abandonRun: id => post(`/api/run/${encodeURIComponent(id)}/abandon`),
    launch: init => post("/api/run", init),
    setFlag: init => post("/api/flag", init),
    thread: init => post("/api/thread", init),
  };
}

/* ---------- hosted adapter ---------- */

const READ_ONLY = () => new ApiError("Not available on this read-only build.", 501, {});

// Keys the hosted adapter asks the Worker for, as /api/<name>. Exported so the
// snapshot contract test can assert snapshot.py writes every one of them.
export const HOSTED_KEYS = {
  overview: () => "overview",
  runs: () => "runs",
  groups: () => "groups",
  matrix: () => "matrix",
  experiments: () => "experiments",
  experiment: name => `experiment.${keyEnc(name || "jev-ab")}`,
  run: id => `run/${keyEnc(id)}`,
  runLive: id => `run/${keyEnc(id)}/live`,
  runEvidence: id => `run/${keyEnc(id)}/evidence`,
  compare: (a, b) => `compare.${keyEnc(a)}.${keyEnc(b)}`,
  leaderboard: () => "leaderboard",
  pairings: group => (group ? `pairings.${keyEnc(group)}` : "pairings"),
  cards: lens => `cards.${keyEnc(lens || "overall")}`,
  card: (kind, target, lens) => `card/${keyEnc(kind)}/${keyEnc(target)}.${keyEnc(lens || "overall")}`,
  flags: () => "flags",
  modelsCatalog: () => "models-catalog",
};

// Mirrors orchestral.tui.state.filter_runs and state.runs_payload's status filter.
export function filterRuns(rows, f = {}) {
  const q = String(f.q || "").trim().toLowerCase();
  return rows.filter(r => {
    if (f.group && r.run_group !== f.group) return false;
    if (f.task && r.task_id !== f.task) return false;
    if (q && ![r.run_id, r.task_id, r.orchestrator, r.worker, r.run_group, r.status, r.failure_reason]
      .some(v => String(v || "").toLowerCase().includes(q))) return false;
    if (f.status === "passed") return r.status === "finished" && !!r.passes;
    if (f.status === "failed") return r.status === "failed" || (r.status === "finished" && !r.passes);
    if (f.status) return r.status === f.status;
    return true;
  });
}

export function hostedAdapter() {
  const get = async (key, o, missing) => {
    try { return await api(`/api/${key}`, { signal: sig(o) }); }
    catch (e) {
      if (e instanceof ApiError && e.status === 404 && missing) {
        throw new ApiError(missing, 404, { error: missing });
      }
      throw e;
    }
  };
  return {
    mode: "hosted",
    overview: o => get(HOSTED_KEYS.overview(), o),
    runs: async (f = {}, o) => filterRuns(await get(HOSTED_KEYS.runs(), o), f),
    groups: o => get(HOSTED_KEYS.groups(), o),
    matrix: o => get(HOSTED_KEYS.matrix(), o),
    experiments: o => get(HOSTED_KEYS.experiments(), o),
    experiment: (name, o) => get(HOSTED_KEYS.experiment(name), o,
      `There is no experiment named ${name || "jev-ab"} in this snapshot.`),
    run: (id, o) => get(HOSTED_KEYS.run(id), o, "That run is not part of this snapshot."),
    runLive: async (id, after = 0, o) => {
      const p = await get(HOSTED_KEYS.runLive(id), o);
      if (!after) return p;
      return { ...p, rows: p.rows.slice(after), details: p.details.slice(after) };
    },
    runEvidence: (id, _opts, o) => get(HOSTED_KEYS.runEvidence(id), o),
    compare: (a, b, o) => get(HOSTED_KEYS.compare(a, b), o,
      "That comparison is not part of this snapshot. Pick two groups from the lists."),
    leaderboard: async (sort, o) => {
      const rows = await get(HOSTED_KEYS.leaderboard(), o);
      return sort && rows.length && sort in rows[0] ? [...rows].sort((x, y) => (x[sort] ?? 0) - (y[sort] ?? 0)) : rows;
    },
    pairings: (group, o) => get(HOSTED_KEYS.pairings(group), o, "No pairings were synced for that group."),
    cards: async (f = {}, o) => {
      const cat = await get(HOSTED_KEYS.cards(f.lens), o);
      const scope = f.scope || "all";
      let cards = cat.cards || [];
      if (scope === "group") cards = cards.filter(c => c.kind === "group");
      if (scope === "pairing") cards = cards.filter(c => c.kind === "pairing");
      if (f.group) cards = cards.filter(c => c.kind !== "group" || c.target === f.group);
      if (f.flagged) cards = cards.filter(c => c.flag);
      return { ...cat, cards, filters: { group: f.group || "", scope, lens: f.lens || "overall", flagged: !!f.flagged } };
    },
    card: (f, o) => get(HOSTED_KEYS.card(f.kind, f.target, f.lens), o, "That card is not part of this snapshot."),
    flags: o => get(HOSTED_KEYS.flags(), o),
    modelsCatalog: o => get(HOSTED_KEYS.modelsCatalog(), o),
    tasks: async () => { throw READ_ONLY(); },
    models: async () => { throw READ_ONLY(); },
    estimate: async () => { throw READ_ONLY(); },
    threadEstimate: async () => { throw READ_ONLY(); },
    cancelRun: async () => { throw READ_ONLY(); },
    abandonRun: async () => { throw READ_ONLY(); },
    launch: async () => { throw READ_ONLY(); },
    setFlag: async () => { throw READ_ONLY(); },
    thread: async () => { throw READ_ONLY(); },
  };
}

export const STALE = Symbol("stale");

/* latest(): for work a view re-runs on input (a filter box). Each call aborts
   the previous one and resolves STALE if a newer call took over, so only the
   newest response is ever painted. Also aborted when the route changes. */
export function latest() {
  let ctl = null;
  const parent = ACTIVE;
  return async fn => {
    ctl?.abort();
    const mine = ctl = new AbortController();
    parent?.addEventListener("abort", () => mine.abort(), { once: true });
    try {
      const v = await fn(mine.signal);
      return mine.signal.aborted ? STALE : v;
    } catch (e) {
      if (mine.signal.aborted) return STALE;
      throw e;
    }
  };
}

/* Await an optional resource: a missing or failing one yields null, but an
   abort still rejects so a superseded render stops instead of painting. */
export function optional(promise) {
  return promise.catch(e => { if (isAbort(e)) throw e; return null; });
}

/* `data.<resource>(...)` dispatches to the adapter chosen by loadMeta(). */
export const data = new Proxy({}, {
  get: (_t, name) => (...args) => {
    if (!adapter) adapter = localAdapter();
    return adapter[name](...args);
  },
});

