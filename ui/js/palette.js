/* The command palette (U14). One combobox over everything a person might jump
   to: runs, tasks, pairings, run groups, models, views, Guide sections and
   commands. The index is built client-side from the same adapter the views use,
   so it behaves identically on the local server and on the hosted snapshot.

   Ranking: an exact run id first, then a run id prefix, then by how well the
   label matches (exact, prefix, word start, substring, every word, in-order
   letters). Ties keep a fixed kind order, then shorter labels. At most 50 rows.
   Recent picks are remembered per device and lead the empty list. */
import { can, data, meta } from "./data.js";
import { closeDialog, initDialog, openDialog } from "./components/dialog.js";
import { esc, pairingParam } from "./util.js";

const MAX_RESULTS = 50;
const MAX_RECENT = 6;
const RECENT_KEY = "orchestral.palette.recent";
const INDEX_TTL_MS = 15000; // a local index is rebuilt on open once it is this old

const KIND = {
  route: { label: "View", icon: "overview", rank: 0 },
  command: { label: "Command", icon: "keyboard", rank: 1 },
  task: { label: "Task", icon: "artifact", rank: 2 },
  pairing: { label: "Pairing", icon: "leaderboard", rank: 3 },
  group: { label: "Group", icon: "compare", rank: 4 },
  model: { label: "Model", icon: "models", rank: 5 },
  run: { label: "Run", icon: "runs", rank: 6 },
};

const VIEWS = [
  ["Now", "#/"], ["Runs", "#/runs"], ["Pairings", "#/leaderboard"], ["Compare", "#/compare"],
  ["Experiments", "#/experiment"], ["Publish", "#/cards"], ["Models", "#/models"],
  ["New run", "#/new", "launch"], ["Guide", "#/about"],
];
const GUIDE = [
  ["axes", "The two axes"], ["judge", "Judge states"], ["naming", "Names"],
  ["numbers", "Reading the numbers"], ["legend", "Glyphs and Rests"],
];
const COMMANDS = [
  { id: "theme", label: "Toggle colour theme", words: "dark light paper stage appearance",
    run: () => document.querySelector("[data-theme-cycle]")?.click() },
  { id: "keys", label: "Show keyboard shortcuts", words: "help keys shortcuts",
    run: () => document.querySelector("[data-open-keymap]")?.click() },
  { id: "link", label: "Copy link to this view", words: "share url clipboard",
    run: () => { try { navigator.clipboard?.writeText(location.href); } catch { /* clipboard may be blocked */ } } },
];

const staticEntries = () => [
  ...VIEWS.filter(([, , need]) => !need || can(need)).map(([label, href]) => ({ kind: "route", label, href, sub: "" })),
  ...GUIDE.map(([k, label]) => ({ kind: "route", label: `Guide: ${label}`, href: `#/about?s=${k}`, sub: "" })),
  ...COMMANDS.map(c => ({ kind: "command", label: c.label, href: "#", command: c.id, sub: "", words: c.words })),
];

/* ---------- the index ---------- */

let index = null;     // { at, entries }
let building = null;  // the build in flight
let ctl = null;

async function build() {
  ctl?.abort();
  const mine = ctl = new AbortController();
  const o = { signal: mine.signal };
  const soft = p => p.catch(() => null);
  const [runs, groups, matrix, pairings, models] = await Promise.all([
    soft(data.runs({}, o)), soft(data.groups(o)), soft(data.matrix(o)),
    soft(data.pairings(undefined, o)), soft(data.modelsCatalog(o)),
  ]);
  const out = [];
  for (const r of runs || []) {
    out.push({
      kind: "run", label: r.run_id, href: `#/run/${encodeURIComponent(r.run_id)}`,
      sub: [r.task_id, r.orchestrator && r.worker ? `${r.orchestrator} → ${r.worker}` : "", r.status].filter(Boolean).join(" · "),
    });
  }
  for (const t of (matrix && matrix.tasks) || []) {
    out.push({ kind: "task", label: t.task_id, sub: [t.task_title, t.task_type].filter(Boolean).join(" · "),
      href: `#/runs?task=${encodeURIComponent(t.task_id)}` });
  }
  for (const p of (pairings && pairings.rows) || []) {
    out.push({ kind: "pairing", label: `${p.orchestrator} → ${p.worker}`, sub: `${p.runs} run${p.runs === 1 ? "" : "s"}`,
      href: `#/runs?pairing=${encodeURIComponent(pairingParam(p.orchestrator, p.worker))}` });
  }
  for (const g of groups || []) {
    if (g.group === "(ungrouped)") continue;
    out.push({ kind: "group", label: g.group, sub: [g.display_label !== g.group ? g.display_label : "", `${g.runs} run${g.runs === 1 ? "" : "s"}`].filter(Boolean).join(" · "),
      href: `#/runs?group=${encodeURIComponent(g.group)}` });
  }
  for (const m of (models && models.models) || []) {
    out.push({ kind: "model", label: m.slug, sub: m.name && m.name !== m.slug ? m.name : "", href: `#/models?q=${encodeURIComponent(m.slug)}` });
  }
  if (mine.signal.aborted) return null;
  return { at: Date.now(), entries: out };
}

function ensureIndex() {
  const fresh = index && (meta().mode === "hosted" || Date.now() - index.at < INDEX_TTL_MS);
  if (fresh) return Promise.resolve(index);
  building ||= build().then(i => { if (i) index = i; return index; }).finally(() => { building = null; });
  return building;
}

/* ---------- ranking ---------- */

const norm = s => String(s ?? "").toLowerCase();

function textScore(label, q, tokens, extra) {
  const l = norm(label);
  if (l === q) return 1;
  if (l.startsWith(q)) return 2;
  if (new RegExp(`[\\s/|_.:>-]${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`).test(l)) return 3;
  if (l.includes(q)) return 4;
  const hay = `${l} ${norm(extra)}`;
  if (tokens.every(t => hay.includes(t))) return 5;
  if (q.length >= 3 && /^[a-z0-9]/.test(q)) {
    // in-order letters, but only from the start of a word, so "land" does not find "Glyphs and Rests"
    for (const m of l.matchAll(/(?:^|[\s/|_.:>-])([a-z0-9])/g)) {
      if (m[1] !== q[0]) continue;
      let i = 0;
      for (const ch of l.slice(m.index)) if (ch === q[i]) i++;
      if (i === q.length) return 6;
    }
  }
  return 99;
}

export function rank(entries, query) {
  const q = norm(query).trim();
  if (!q) return [];
  const tokens = q.split(/\s+/);
  const scored = [];
  entries.forEach((e, order) => {
    let s;
    if (e.kind === "run") {
      const id = norm(e.label);
      s = id === q ? 0 : id.startsWith(q) ? 0.5 : 99;
      // a run found only through its task or models never outranks a named thing
      if (s === 99) { s = textScore(e.label, q, tokens, e.sub); if (s < 99) s += 3; }
    } else s = textScore(e.label, q, tokens, `${e.sub || ""} ${e.words || ""}`);
    if (s < 99) scored.push({ e, s, order });
  });
  scored.sort((a, b) => a.s - b.s || KIND[a.e.kind].rank - KIND[b.e.kind].rank
    || a.e.label.length - b.e.label.length || a.order - b.order);
  return scored.slice(0, MAX_RESULTS).map(x => x.e);
}

/* ---------- recents (per device) ---------- */

function readRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").filter(r => r && r.href && r.kind && KIND[r.kind]); }
  catch { return []; }
}
function remember(e) {
  if (e.kind === "command") return;
  const keep = { kind: e.kind, label: e.label, sub: e.sub || "", href: e.href };
  const next = [keep, ...readRecent().filter(r => r.href !== e.href)].slice(0, MAX_RECENT);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(next)); } catch { /* storage may be blocked */ }
}

/* ---------- the dialog ---------- */

let shown = []; // entries behind the options on screen

function paletteBody() {
  return `<form method="dialog" class="palette-form" role="search">
    <label class="sr-only" for="palette-q">Jump to</label>
    <input id="palette-q" type="text" role="combobox" autocomplete="off" spellcheck="false" aria-autocomplete="list"
      aria-expanded="true" aria-controls="palette-list" aria-haspopup="listbox"
      placeholder="Search runs, tasks, pairings, groups, models and views">
    <ul id="palette-list" class="palette-list" role="listbox" aria-label="Results"></ul>
    <p id="palette-status" class="sr-only" role="status"></p>
  </form>`;
}

const optionHtml = (e, i, recent) => `<li role="option" id="pal-o${i}" data-kind="${e.kind}" data-i="${i}"${recent ? ' data-recent="true"' : ""} aria-selected="false">
  <a href="${esc(e.href)}" tabindex="-1"><svg class="i" aria-hidden="true" focusable="false"><use href="#i-${KIND[e.kind].icon}"/></svg>
  <span class="pal-main"><span class="pal-label">${esc(e.label)}</span>${e.sub ? `<span class="pal-sub">${esc(e.sub)}</span>` : ""}</span>
  <span class="pal-kind">${KIND[e.kind].label}</span></a></li>`;

function setActive(dlg, i) {
  const opts = [...dlg.querySelectorAll("[role=option]")];
  const input = dlg.querySelector("#palette-q");
  const n = opts.length ? (i + opts.length) % opts.length : -1;
  opts.forEach((o, k) => o.setAttribute("aria-selected", String(k === n)));
  if (n >= 0) { input.setAttribute("aria-activedescendant", opts[n].id); opts[n].scrollIntoView({ block: "nearest" }); }
  else input.removeAttribute("aria-activedescendant");
}

function render(dlg) {
  const q = dlg.querySelector("#palette-q").value.trim();
  const list = dlg.querySelector("#palette-list");
  const status = dlg.querySelector("#palette-status");
  let html = "";
  if (!q) {
    const recent = readRecent();
    const base = staticEntries();
    const seen = new Set(recent.map(r => r.href));
    const rest = base.filter(e => e.kind === "command" || !seen.has(e.href));
    shown = [...recent, ...rest];
    let i = 0;
    if (recent.length) html += `<li role="presentation" class="palette-group">Recent</li>` + recent.map(e => optionHtml(e, i++, true)).join("");
    html += `<li role="presentation" class="palette-group">Views and commands</li>` + rest.map(e => optionHtml(e, i++, false)).join("");
  } else {
    const pool = [...staticEntries(), ...(index ? index.entries : [])];
    shown = rank(pool, q);
    if (shown.length) html = shown.map((e, i) => optionHtml(e, i, false)).join("");
    else if (!index) html = `<li role="presentation" class="palette-wait">Searching…</li>`;
    else html = `<li role="presentation" class="palette-none">Nothing matches. <a href="#/runs?q=${encodeURIComponent(q)}">Search Runs</a></li>`;
  }
  list.innerHTML = html;
  setActive(dlg, shown.length ? 0 : -1);
  status.textContent = !q ? "" : shown.length ? `${shown.length} result${shown.length === 1 ? "" : "s"}` : index ? "No results" : "Searching";
}

function choose(dlg, i) {
  const e = shown[i];
  if (!e) return;
  remember(e);
  closeDialog(dlg);
  if (e.kind === "command") COMMANDS.find(c => c.id === e.command)?.run();
  else location.hash = e.href;
}

export function initPalette() {
  const dlg = document.getElementById("palette");
  dlg.innerHTML = paletteBody();
  initDialog(dlg);
  const input = dlg.querySelector("#palette-q");
  input.addEventListener("input", () => render(dlg));
  input.addEventListener("keydown", e => {
    const cur = [...dlg.querySelectorAll("[role=option]")].findIndex(o => o.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive(dlg, cur + (e.key === "ArrowDown" ? 1 : -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(dlg, Math.max(cur, 0));
    }
  });
  dlg.querySelector("#palette-list").addEventListener("click", e => {
    const li = e.target.closest("[role=option]");
    if (!li) return;
    e.preventDefault(); // choose() navigates, so the anchor's own jump never double-fires
    choose(dlg, Number(li.dataset.i));
  });
  dlg.addEventListener("close", () => { input.value = ""; ctl?.abort(); });
}

export function openPalette(opener) {
  const dlg = document.getElementById("palette");
  closeDialog(document.getElementById("more-sheet"));
  dlg.querySelector("#palette-q").value = ""; // the close event that clears it is queued, so a quick reopen would see the old text
  render(dlg);
  openDialog(dlg, opener);
  dlg.querySelector("#palette-q").focus();
  ensureIndex().then(() => { if (dlg.open) render(dlg); });
}
