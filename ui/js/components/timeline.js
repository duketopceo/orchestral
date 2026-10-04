/* Lane timeline (DESIGN 6.9 #7): lanes are orchestrator, each worker, assemble,
   validate and judge. A bar is one call: its left edge is when it started, its
   length is its latency, its 2px top tick is the verdict (pass or fail). Cost and
   time show on hover and focus. All bars share one time axis with labeled ticks.
   Each bar links to its event in the Events tab. */
import { duration, money } from "../format.js";
import { esc } from "../util.js";
import { liveGlyph, stateHtml } from "./states.js";

const TICKS = [0, 0.25, 0.5, 0.75, 1];

function bar(runId, b, span) {
  const left = Math.min(99.6, 100 * b.start_ms / span);
  const width = Math.max(0.5, Math.min(100 - left, 100 * b.dur_ms / span));
  const cost = b.cost_usd == null ? "" : `, ${money(b.cost_usd)}`;
  const label = `${b.type} ${duration(b.dur_ms)}${cost}${b.verdict === "fail" ? ", error" : ""}`;
  return `<a class="ln-bar ${b.verdict === "fail" ? "ln-fail" : "ln-ok"}" href="#/run/${esc(runId)}?tab=events&event=${b.event}"
    style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%" title="${esc(label)}" aria-label="${esc(label)}"></a>`;
}

export function timelineHtml(runId, lanes, { running = false } = {}) {
  if (!lanes || !lanes.lanes.length) {
    return running
      ? stateHtml("starting", { title: "Run is starting", body: "The first call has not finished yet." })
      : stateHtml("empty", { title: "No timeline", body: "No timed calls were recorded for this run." });
  }
  const span = lanes.span_ms || 1;
  return `<div class="lanes" role="group" aria-label="Run timeline, one lane per stage">
    <div class="ln-axis" aria-hidden="true"><span class="ln-label"></span><div class="ln-track">${TICKS.map(t =>
      `<span class="ln-tick" style="left:${t * 100}%">${duration(span * t)}</span>`).join("")}</div></div>
    ${lanes.lanes.map(l => `<div class="ln-row" data-lane="${esc(l.id)}"${lanes.live === l.id ? ' data-live="1"' : ""}>
      <span class="ln-label">${esc(l.label)}${lanes.live === l.id ? ` ${liveGlyph()}` : ""}</span>
      <div class="ln-track">${l.bars.map(b => bar(runId, b, span)).join("")}</div></div>`).join("")}
  </div>`;
}
