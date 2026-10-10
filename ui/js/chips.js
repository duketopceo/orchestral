import * as F from "./format.js";
import { icon, liveGlyph } from "./components/states.js";
import { flagWidget } from "./flags.js";
import { NIL, basisNote, billedOf, esc, fmtMoney, fmtMs, fmtScore, fmtTok, fmtWhen, slug } from "./util.js";

/* The outcome axis (what happened to the run) is separate from the judge
   axis (whether a verdict exists): a malformed output is Invalid, not a
   Fail, and an infra death never reached the model-quality question. */
const OUTCOMES = {
  pass: ["chip-pass", "pass", "Pass", "The run met the task's pass criteria"],
  fail: ["chip-fail", "fail", "Fail", "Judged and failed, or failed the mechanical checks"],
  invalid: ["chip-warn", "inconclusive", "Invalid",
    "The output never became a judgeable artifact; this is not a model-quality verdict"],
  infra: ["chip-warn", "cost", "Infra",
    "Infrastructure failure: the run never got a fair attempt"],
  inconclusive: ["chip-dim", "inconclusive", "Inconclusive",
    "Finished without a usable verdict"],
  dry: ["chip-dim", "not-judged", "Dry run", "Sample data; never counted as evidence"],
  lost: ["chip-warn", "stalled", "Lost",
    "Still marked running but silent for over an hour; the process is presumed dead"],
  cancelled: ["chip-dim", "cancelled", "Cancelled", "Stopped before a terminal state"],
  running: ["chip-warn", "live", "Running", "Still in progress"],
  unknown: ["chip-dim", null, "Unknown", "Unrecognized status"],
};

export function outcomeChip(r) {
  if (r.holdout && r.status === "finished")
    return `<span class="chip chip-dim" title="Holdout arm: the outcome is not published">Withheld</span>`;
  const st = r.outcome || (r.status === "running" ? "running" : r.status);
  const [cls, ic, label, dflt] = OUTCOMES[st] || OUTCOMES.unknown;
  const why = esc(r.outcome_reason || dflt);
  const glyph = st === "running" ? liveGlyph() : ic ? icon(ic) : "";
  return `<span class="chip ${cls}" title="${why}">${glyph}${label}</span>`;
}

export function judgeChip(r) {
  // judge_score is the semantic axis — `score` is mechanical (don't mislabel)
  if (r.judge_score != null)
    return `<span class="chip chip-info" title="Semantic quality score from the judge model">${icon("judge")}Judge ${fmtScore(r.judge_score)}</span>`;
  const st = r.judge_state || "not_judged";
  const why = esc(r.judge_reason || "");
  if (st === "inconclusive")
    return `<span class="chip chip-warn" title="${why}">${icon("inconclusive")}Judge inconclusive</span>`;
  if (st === "unreadable")
    return `<span class="chip chip-warn" title="${why || "report.json could not be read, so whether the judge ran is unknown"}">Judge unknown</span>`;
  if (st === "not_judgeable")
    return `<span class="chip chip-dim" title="${why}">Not judgeable</span>`;
  return `<span class="chip chip-dim" title="${why || "The judge was not run for this run"}">${icon("not-judged")}Not judged</span>`;
}

export function runRow(r) {
  return `<tr>
    <td>${outcomeChip(r)} ${flagWidget("run", r.run_id)}</td>
    <td><a href="#/run/${esc(r.run_id)}">${esc(r.task_title || r.task_id)}</a>${r.task_title ? `<div class="dim sm">${esc(r.task_id)}</div>` : ""}</td>
    <td class="mono">${esc(slug(r.orchestrator))} <span class="dim">→</span> ${esc(slug(r.worker))}</td>
    <td>${judgeChip(r)}</td>
    <td class="t-num" title="${esc(basisNote(r))}">${fmtMoney(billedOf(r))}${r.cost_basis && r.cost_basis !== "billed" ? ' <span class="dim sm">est.</span>' : ""}</td>
    <td class="t-num">${fmtTok((r.total_input_tokens || 0) + (r.total_output_tokens || 0))}</td>
    <td class="t-num">${fmtMs(r.latency_ms)}</td>
    <td class="dim">${r.run_group ? esc(r.run_group) : NIL}</td>
    <td class="dim">${fmtWhen(r.started_at)}</td>
  </tr>`;
}

export const RUN_HEAD = `<tr>
  <th>Outcome</th><th>Task</th><th>Orchestrator → Worker</th><th>Judge</th>
  <th class="t-num">Cost</th><th class="t-num" data-pri="2">Tokens</th><th class="t-num" data-pri="2">Duration</th>
  <th data-pri="3">Group</th><th data-pri="3">Started</th>
</tr>`;
