import * as F from "./format.js";
import { flagWidget } from "./flags.js";
import { basisNote, billedOf, esc, fmtMoney, fmtMs, fmtScore, fmtTok, fmtWhen, slug } from "./util.js";

export function statusChip(r) {
  if (r.status === "running") return `<span class="chip chip-warn"><span class="dot dot-run pulse"></span>Running</span>`;
  if (r.status === "failed") return `<span class="chip chip-fail">Failed</span>`;
  if (r.status === "cancelled") return `<span class="chip chip-dim">Cancelled</span>`;
  if (r.passes === true || r.passes === 1) return `<span class="chip chip-pass">Pass</span>`;
  if (r.passes === false || r.passes === 0) return `<span class="chip chip-fail">Fail</span>`;
  return `<span class="chip chip-dim">${esc(r.status)}</span>`;
}

export function judgeChip(r) {
  // judge_score is the semantic axis — `score` is mechanical (don't mislabel)
  if (r.judge_score != null)
    return `<span class="chip chip-info" title="Semantic quality score from the judge model">Judge ${fmtScore(r.judge_score)}</span>`;
  const st = r.judge_state || "not_judged";
  const why = esc(r.judge_reason || "");
  if (st === "inconclusive")
    return `<span class="chip chip-warn" title="${why}">Judge inconclusive</span>`;
  if (st === "unreadable")
    return `<span class="chip chip-warn" title="${why || "report.json could not be read, so whether the judge ran is unknown"}">Judge unknown</span>`;
  if (st === "not_judgeable")
    return `<span class="chip chip-dim" title="${why}">Not judgeable</span>`;
  return `<span class="chip chip-dim" title="${why || "The judge was not run for this run"}">Not judged</span>`;
}

export function runRow(r) {
  return `<tr>
    <td>${statusChip(r)} ${flagWidget("run", r.run_id)}</td>
    <td><a href="#/run/${esc(r.run_id)}">${esc(r.task_title || r.task_id)}</a>${r.task_title ? `<div class="dim sm">${esc(r.task_id)}</div>` : ""}</td>
    <td class="mono">${esc(slug(r.orchestrator))} <span class="dim">→</span> ${esc(slug(r.worker))}</td>
    <td>${judgeChip(r)}</td>
    <td class="t-num" title="${esc(basisNote(r))}">${fmtMoney(billedOf(r))}${r.cost_basis && r.cost_basis !== "billed" ? ' <span class="dim sm">est.</span>' : ""}</td>
    <td class="t-num">${fmtTok((r.total_input_tokens || 0) + (r.total_output_tokens || 0))}</td>
    <td class="t-num">${fmtMs(r.latency_ms)}</td>
    <td class="dim">${esc(r.run_group || F.NULL_GLYPH)}</td>
    <td class="dim">${fmtWhen(r.started_at)}</td>
  </tr>`;
}

export const RUN_HEAD = `<tr>
  <th>Verdict</th><th>Task</th><th>Orchestrator → Worker</th><th>Judge</th>
  <th class="t-num">Cost</th><th class="t-num">Tokens</th><th class="t-num">Duration</th>
  <th>Group</th><th>Started</th>
</tr>`;
