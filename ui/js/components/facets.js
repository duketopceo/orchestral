/* Facets for list views: the definitions, the options a data set offers, the
   chips that show what is applied, and a quiet-period debounce.

   A facet value that the current data does not contain (a stale deep link) is
   kept and shown as an error-styled chip, so a typo never looks like "no runs". */
import { icon } from "./states.js";
import { esc } from "../util.js";
import { shortSlug } from "../format.js";

export const DEBOUNCE_MS = 200;

/* debounce(fn): calls fn with the last arguments once DEBOUNCE_MS pass with no
   further call. AbortSignal.timeout is the timer, so views hold no timer calls. */
export function debounce(fn, ms = DEBOUNCE_MS) {
  let ctl = null;
  return (...args) => {
    ctl?.abort();
    const mine = ctl = new AbortController();
    const quiet = AbortSignal.timeout(ms);
    quiet.addEventListener("abort", () => { if (!mine.signal.aborted) fn(...args); }, { once: true });
  };
}

const STATUS = [
  ["running", "Running"], ["stalled", "Stalled"], ["finished", "Finished"], ["passed", "Passed"],
  ["failed", "Failed"], ["cancelled", "Cancelled"],
];
const JUDGE = [
  ["judged", "Judged"], ["not_judged", "Not judged"], ["inconclusive", "Inconclusive"],
  ["unreadable", "Unreadable"], ["not_judgeable", "Not judgeable"],
];

/* key: URL parameter. options(rows) -> [[value, label], ...] from the unfiltered rows. */
export const RUN_FACETS = [
  { key: "group", label: "Group", all: "All groups", options: rows => uniq(rows, r => r.run_group,
      r => (r.group_label ? `${r.group_label}` : r.run_group)) },
  { key: "task", label: "Task", all: "All tasks", options: rows => uniq(rows, r => r.task_id, r => r.task_title || r.task_id) },
  { key: "pairing", label: "Pairing", all: "All pairings", options: rows => uniq(rows,
      r => (r.orchestrator && r.worker ? `${r.orchestrator}|${r.worker}` : ""),
      r => `${shortSlug(r.orchestrator)} / ${shortSlug(r.worker)}`) },
  { key: "status", label: "Verdict", all: "Any verdict", options: () => STATUS },
  { key: "judge", label: "Judge", all: "Any judge state", options: rows => JUDGE.filter(([v]) => rows.some(r => r.judge_state === v)) },
  { key: "type", label: "Type", all: "Any type", options: rows => uniq(rows, r => r.type, r => r.type) },
  { key: "difficulty", label: "Difficulty", all: "Any difficulty", options: rows => uniq(rows, r => r.difficulty, r => r.difficulty) },
];

function uniq(rows, valueOf, labelOf) {
  const seen = new Map();
  for (const r of rows) {
    const v = valueOf(r);
    if (v && !seen.has(v)) seen.set(v, labelOf(r));
  }
  return [...seen].sort((a, b) => String(a[1]).localeCompare(String(b[1])));
}

/* Facets that have at least one option; the rest are not drawn. */
export function liveFacets(rows) {
  return RUN_FACETS.map(f => ({ ...f, opts: f.options(rows) })).filter(f => f.opts.length);
}

export function selectHtml(f, value) {
  return `<label class="facet"><span class="facet-label">${esc(f.label)}</span>
    <select id="f-${f.key}"><option value="">${esc(f.all)}</option>${f.opts.map(([v, l]) =>
      `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
}

/* One chip per applied facet (and the search). `facets` is liveFacets(rows). */
export function chipsHtml(filters, facets) {
  const chips = [];
  if (filters.q) chips.push({ key: "q", label: "Search", text: filters.q, bad: false });
  for (const f of facets) {
    const v = filters[f.key];
    if (!v) continue;
    chips.push({ key: f.key, label: f.label, text: f.opts.find(([o]) => o === v)?.[1] ?? v,
      bad: !f.opts.some(([o]) => o === v) });
  }
  // a facet with no options at all is also "not found"
  for (const f of RUN_FACETS) {
    if (filters[f.key] && !facets.some(x => x.key === f.key))
      chips.push({ key: f.key, label: f.label, text: filters[f.key], bad: true });
  }
  return chips.map(c => `<span class="facet-chip${c.bad ? " bad" : ""}" data-facet="${esc(c.key)}"${c.bad ? ' role="alert"' : ""}>
      <span class="facet-chip-text" title="${esc(c.text)}"><b>${esc(c.label)}</b> ${esc(c.text)}${c.bad ? ' <span class="facet-bad">Not found in current data</span>' : ""}</span>
      <button type="button" class="facet-x" data-remove="${esc(c.key)}" aria-label="Remove ${esc(c.label)} filter">${icon("close")}</button></span>`).join("");
}

/* The labels of the applied facets, for "Remove the search or the group" copy. */
export function appliedNames(filters) {
  const names = [];
  if (filters.q) names.push("search");
  for (const f of RUN_FACETS) if (filters[f.key]) names.push(f.label.toLowerCase());
  return names;
}
