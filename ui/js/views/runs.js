/* Runs: a faceted, URL-synced, paged list. Facets, search and sort live in the
   URL (and are sent to the server locally, or applied to the snapshot hosted);
   the page is client-side at PAGE_SIZE rows so the table DOM stays small. Rows
   that arrive while reading wait behind a banner. */
import { $view } from "../dom.js";
import { STALE, can, data, filterRuns, isHosted, latest } from "../data.js";
import { stateHtml, icon } from "../components/states.js";
import { appliedNames, chipsHtml, debounce, liveFacets, selectHtml, RUN_FACETS } from "../components/facets.js";
import { isAbort } from "../api.js";
import { judgeChip, statusChip } from "../chips.js";
import { bindFlags, flagWidget, loadFlags } from "../flags.js";
import { start } from "../poller.js";
import { announce } from "../shell.js";
import { NIL, basisNote, billedOf, esc, fmtMoney, fmtMs, fmtTok, fmtWhen, normPairing, slug } from "../util.js";

export const PAGE_SIZE = 200;
const POLL_MS = 20000;
const BUFFER = 6;          // rows kept above and below the visible ones
const MAX_WINDOW = 48;     // never more rows in the DOM than this
const INITIAL_WINDOW = 24;
const SORTS = [
  ["status", "Verdict"], ["task", "Task"], ["cost", "Cost", "t-num"], ["tokens", "Tokens", "t-num", 2],
  ["duration", "Duration", "t-num", 2], ["started", "Started", "", 3],
];

function readState(params) {
  const filters = { q: params.get("q") || "" };
  for (const f of RUN_FACETS) filters[f.key] = params.get(f.key) || "";
  filters.pairing = normPairing(filters.pairing);
  return {
    filters, sort: params.get("sort") || "started", dir: params.get("dir") || "",
    page: Math.max(1, parseInt(params.get("page") || "1", 10) || 1),
  };
}

function hashFor(st) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(st.filters)) if (v) q.set(k, v);
  if (st.sort !== "started" || st.dir) q.set("sort", st.sort);
  if (st.dir) q.set("dir", st.dir);
  if (st.page > 1) q.set("page", String(st.page));
  const s = q.toString();
  return `#/runs${s ? `?${s}` : ""}`;
}

const applied = f => Object.values(f).some(Boolean);

function statusCell(r) {
  const chip = r.stalled
    ? `<span class="chip chip-warn" title="No event for 10 minutes">${icon("stalled")}Stalled</span>`
    : statusChip(r);
  return `${chip} ${flagWidget("run", r.run_id)}`;
}

function groupCell(r) {
  if (!r.run_group) return NIL;
  const text = r.group_label || r.run_group;
  return `<span class="grp"><span class="grp-key" title="${esc(r.run_group)}">${esc(text)}</span>
    <button type="button" class="copy-key" data-copy="${esc(r.run_group)}" aria-label="Copy group key" title="Copy the full group key">${icon("copy")}</button></span>`;
}

function rowHtml(r, i) {
  return `<tr class="run-row" data-i="${i}" data-run="${esc(r.run_id)}">
    <td>${statusCell(r)}</td>
    <td><a href="#/run/${esc(r.run_id)}">${esc(r.task_title || r.task_id)}</a>${r.task_title ? `<div class="dim sm">${esc(r.task_id)}</div>` : ""}</td>
    <td class="mono">${esc(slug(r.orchestrator))} <span class="dim">→</span> ${esc(slug(r.worker))}</td>
    <td>${judgeChip(r)}</td>
    <td class="t-num" title="${esc(basisNote(r))}">${fmtMoney(billedOf(r))}${r.cost_basis && r.cost_basis !== "billed" ? ' <span class="dim sm">est.</span>' : ""}</td>
    <td class="t-num">${fmtTok(r.tokens ?? ((r.total_input_tokens || 0) + (r.total_output_tokens || 0)))}</td>
    <td class="t-num">${fmtMs(r.latency_ms)}</td>
    <td>${groupCell(r)}</td>
    <td class="dim">${fmtWhen(r.started_at)}</td>
  </tr>`;
}

function headHtml(st) {
  const desc = (st.dir || ({ task: "asc", status: "asc" }[st.sort] || "desc")) === "desc";
  const cells = SORTS.map(([key, label, cls = "", pri]) => {
    const on = st.sort === key;
    return `<th${cls ? ` class="${cls}"` : ""}${pri ? ` data-pri="${pri}"` : ""}${on ? ` aria-sort="${desc ? "descending" : "ascending"}"` : ""}>
      <button type="button" class="th-sort" data-sort="${key}">${label}</button></th>`;
  });
  // columns that are not sortable keep their place
  cells.splice(2, 0, "<th>Orchestrator → Worker</th>");
  cells.splice(3, 0, "<th>Judge</th>");
  cells.splice(7, 0, '<th data-pri="3">Group</th>');
  return `<tr>${cells.join("")}</tr>`;
}

export async function viewRuns(params) {
  await loadFlags();
  const st = readState(params);
  let all = await data.runs({});          // the unfiltered set: facet options and the "new rows" baseline
  let rows = all;
  let known = new Set(all.map(r => r.run_id));
  let facets = liveFacets(all);

  $view.innerHTML = `
    <h1>Runs</h1>
    <div class="facet-bar" role="search">
      <label class="facet facet-search"><span class="facet-label">Search</span>
        <input type="search" id="f-q" placeholder="Task, model, group or reason" value="${esc(st.filters.q)}"></label>
      <span id="facet-selects" class="facet-selects"></span>
    </div>
    <div id="facet-chips" class="facet-chips"></div>
    <div id="runs-new-slot"></div>
    <div class="runs-meta"><span id="runs-count" role="status"></span>
      <span class="runs-pager"><button type="button" id="runs-prev">Previous</button><button type="button" id="runs-next">Next</button></span></div>
    <div class="panel"><table class="data"><thead id="runs-head"></thead><tbody id="runs-body"></tbody></table></div>`;

  const $ = id => document.getElementById(id);
  let selected = -1;
  let rowH = 48;
  const syncUrl = () => history.replaceState(null, "", hashFor(st));

  function drawFacets() {
    $("facet-selects").innerHTML = facets.map(f => selectHtml(f, st.filters[f.key])).join("");
    $("facet-chips").innerHTML = chipsHtml(st.filters, facets);
  }

  function none() {
    if (!applied(st.filters)) {
      return stateHtml("empty", { title: "No runs yet", body: "Runs appear here as soon as one is recorded.",
        action: can("launch") ? { label: "Start a run", href: "#/new" } : { label: "Read the guide", href: "#/about" } });
    }
    const names = appliedNames(st.filters);
    const list = names.length > 1 ? `${names.slice(0, -1).join(", ")} or ${names.at(-1)}` : names[0];
    return stateHtml("nomatch", { title: "No runs match these filters",
      body: `Remove the ${list} filter to see more runs, or clear them all.`,
      action: { label: "Clear filters", href: "#/runs" } });
  }

  /* Windowed rows: the page holds up to PAGE_SIZE rows of data but only the rows
     near the viewport are in the DOM (DESIGN 9: table DOM <= 1,500 nodes), with
     spacer rows standing in for the rest. */
  let pageRows = [];
  let win = { first: 0, last: 0 };
  const spacer = px => px > 0 ? `<tr class="spacer" aria-hidden="true"><td colspan="9" class="spacer-cell"><div style="height:${Math.round(px)}px"></div></td></tr>` : "";

  function paintWindow(first, last) {
    win = { first, last };
    const rowsHtml = pageRows.slice(first, last).map((r, k) => rowHtml(r, first + k)).join("");
    $("runs-body").innerHTML = spacer(first * rowH) + rowsHtml + spacer((pageRows.length - last) * rowH);
    if (selected >= first && selected < last) markSelected();
    bindFlags($("runs-body"));
  }

  function visibleRange() {
    const body = $("runs-body");
    const wrap = body.closest(".tbl-wrap");
    const top0 = body.getBoundingClientRect().top;
    const clip = wrap ? wrap.getBoundingClientRect() : { top: 0, bottom: innerHeight };
    const headH = wrap?.querySelector("thead")?.offsetHeight || 0;
    const lo = Math.max(0, clip.top + headH), hi = Math.min(innerHeight, clip.bottom);
    const vf = Math.max(0, Math.floor((lo - top0) / rowH));
    const vl = Math.min(pageRows.length, Math.max(vf + 1, Math.ceil((hi - top0) / rowH)));
    return [vf, vl];
  }

  function syncWindow(force = false) {
    if (!$("runs-body") || !pageRows.length) return;
    const [vf, vl] = visibleRange();
    if (!force && vf >= win.first && vl <= win.last) return;
    const span = Math.min(MAX_WINDOW, vl - vf + 2 * BUFFER);
    const first = Math.max(0, Math.min(vf - BUFFER, pageRows.length - span));
    paintWindow(first, Math.min(pageRows.length, first + span));
    measure();
  }

  /* The row height is measured from what rendered; spacers use it. */
  function measure() {
    const trs = [...document.querySelectorAll("#runs-body tr.run-row")];
    if (!trs.length) return;
    const h = trs.reduce((n, tr) => n + tr.offsetHeight, 0) / trs.length;
    if (h > 0 && Math.abs(h - rowH) > 1) { rowH = h; paintWindow(win.first, win.last); }
  }

  function markSelected() {
    document.querySelectorAll("#runs-body tr[aria-selected]").forEach(tr => tr.removeAttribute("aria-selected"));
    const tr = document.querySelector(`#runs-body tr.run-row[data-i="${selected}"]`);
    tr?.setAttribute("aria-selected", "true");
    return tr;
  }

  function select(i) {
    selected = Math.min(pageRows.length - 1, Math.max(0, i));
    if (selected < win.first || selected >= win.last) {
      const span = Math.min(MAX_WINDOW, win.last - win.first || MAX_WINDOW);
      const first = Math.max(0, Math.min(selected - BUFFER, pageRows.length - span));
      paintWindow(first, Math.min(pageRows.length, first + span));
    }
    markSelected()?.scrollIntoView({ block: "nearest" });
  }

  function drawRows() {
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    if (st.page > pages) st.page = pages;
    const from = (st.page - 1) * PAGE_SIZE;
    pageRows = rows.slice(from, from + PAGE_SIZE);
    $("runs-head").innerHTML = headHtml(st);
    selected = -1;
    if (!pageRows.length) {
      $("runs-body").innerHTML = `<tr><td colspan="9">${none()}</td></tr>`;
    } else {
      paintWindow(0, Math.min(pageRows.length, INITIAL_WINDOW));
      syncWindow(true);
    }
    const count = $("runs-count");
    count.dataset.total = String(rows.length);
    count.dataset.pageRows = String(pageRows.length);
    count.textContent = rows.length
      ? `${(from + 1).toLocaleString()} to ${(from + pageRows.length).toLocaleString()} of ${rows.length.toLocaleString()} runs`
      : "0 runs";
    $("runs-prev").disabled = st.page <= 1;
    $("runs-next").disabled = st.page >= pages;
    $("runs-prev").hidden = $("runs-next").hidden = pages <= 1;
  }

  const newest = latest();
  async function load({ initial = false } = {}) {
    let got;
    try {
      got = applied(st.filters) || st.sort !== "started" || st.dir
        ? await newest(signal => data.runs({ ...st.filters, sort: st.sort, dir: st.dir }, { signal }))
        : all;
    } catch (e) {
      if (initial || isAbort(e)) throw e;
      $("runs-body").innerHTML = `<tr><td colspan="9" class="empty" role="alert">${esc(e.message)}</td></tr>`;
      return;
    }
    if (got === STALE) return;
    rows = got;
    drawRows();
  }

  function change(patch, { keepPage = false } = {}) {
    Object.assign(st, patch);
    if (!keepPage) st.page = 1;
    syncUrl();
    drawFacets();
    return load();
  }

  const typed = debounce(v => change({ filters: { ...st.filters, q: v } }));
  $("f-q").addEventListener("input", e => typed(e.target.value));
  $("facet-selects").addEventListener("change", e => {
    const key = e.target.id?.replace(/^f-/, "");
    if (RUN_FACETS.some(f => f.key === key)) change({ filters: { ...st.filters, [key]: e.target.value } });
  });
  $("facet-chips").addEventListener("click", e => {
    const b = e.target.closest("[data-remove]");
    if (!b) return;
    const key = b.dataset.remove;
    if (key === "q") $("f-q").value = "";
    change({ filters: { ...st.filters, [key]: "" } });
  });
  $("runs-head").addEventListener("click", e => {
    const b = e.target.closest("[data-sort]");
    if (!b) return;
    const key = b.dataset.sort;
    const dflt = { task: "asc", status: "asc" }[key] || "desc";
    const cur = st.sort === key ? (st.dir || dflt) : null;
    const dir = cur ? (cur === "desc" ? "asc" : "desc") : dflt;
    change({ sort: key, dir: dir === dflt ? "" : dir });
  });
  const turn = delta => () => { st.page += delta; syncUrl(); drawRows(); $("runs-count").scrollIntoView({ block: "nearest" }); };
  $("runs-prev").addEventListener("click", turn(-1));
  $("runs-next").addEventListener("click", turn(1));
  $view.addEventListener("click", async e => {
    const b = e.target.closest(".copy-key");
    if (!b) return;
    try { await navigator.clipboard?.writeText(b.dataset.copy); announce("Group key copied"); } catch { announce("Could not copy"); }
  });

  /* j and k walk the rows; Enter opens the selected one. The handler is bound
     once and goes quiet when the Runs table is not on screen. */
  bindRowKeys(() => ({ selected, count: pageRows.length, select,
    open: () => document.querySelector(`#runs-body tr.run-row[data-i="${selected}"] a`)?.click() }));
  // scrolling (the page, or the table's own box) moves the window
  let queued = false;
  addScroll(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; syncWindow(); });
  });

  /* New runs: poll the unfiltered list; show a banner, never insert above the reader. */
  const banner = n => {
    $("runs-new-slot").innerHTML = n
      ? `<div id="runs-new" class="runs-new" role="status"><span>${n} new run${n === 1 ? "" : "s"}.</span> <button type="button">Show</button></div>` : "";
  };
  $("runs-new-slot").addEventListener("click", async e => {
    if (!e.target.closest("button")) return;
    all = await data.runs({});
    known = new Set(all.map(r => r.run_id));
    facets = liveFacets(all);
    st.page = 1;
    banner(0);
    drawFacets();
    await load();
  });
  if (!isHosted()) {
    start("runs-new", async signal => {
      const fresh = await data.runs({}, { signal });
      const mine = filterRuns(fresh.filter(r => !known.has(r.run_id)), st.filters);
      banner(mine.length);
    }, { ms: POLL_MS });
  }

  drawFacets();
  if (applied(st.filters) || st.sort !== "started" || st.dir) await load({ initial: true });
  else drawRows();
}

let keysBound = false;
let rowState = null;
let scrollFn = null;
function addScroll(fn) { scrollFn = fn; }
function bindRowKeys(get) {
  rowState = get;
  if (keysBound) return;
  keysBound = true;
  document.addEventListener("scroll", () => scrollFn && document.getElementById("runs-body") && scrollFn(), true);
  window.addEventListener("resize", () => scrollFn && document.getElementById("runs-body") && scrollFn());
  document.addEventListener("keydown", e => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || !rowState) return;
    if (!document.getElementById("runs-body")) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName))) return;
    if (document.querySelector("dialog[open]")) return;
    const s = rowState();
    if (e.key === "Enter" && s.selected >= 0) { s.open(); e.preventDefault(); return; }
    const step = e.key === "j" ? 1 : e.key === "k" ? -1 : 0;
    if (!step || !s.count) return;
    s.select(s.selected < 0 ? 0 : s.selected + step);
    e.preventDefault();
  });
}
