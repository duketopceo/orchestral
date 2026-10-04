/* Interval mark: a point plus its 95% whisker on a hairline track, no filled
   background track (DESIGN.md 6.10). Low n hatches the whisker. */
import { px } from "./scale.js";
import { hatchRect } from "./frame.js";

// x: scale for rates 0..1. cls: "pass" (mechanical hue) or "base" (hollow ink).
export function intervalMark({ x, y, rate, ci, lowN = false, cls = "pass", r = 4 }) {
  if (rate == null) return "";
  const lo = ci ? ci[0] : rate, hi = ci ? ci[1] : rate;
  const x0 = x(lo), x1 = x(hi);
  return `${lowN ? hatchRect(px(x0), px(y - 4), px(Math.max(0, x1 - x0)), 8) : ""}
    <line class="ch-whisker ch-${cls}" x1="${px(x0)}" y1="${px(y)}" x2="${px(x1)}" y2="${px(y)}"/>
    <line class="ch-cap ch-${cls}" x1="${px(x0)}" y1="${px(y - 3)}" x2="${px(x0)}" y2="${px(y + 3)}"/>
    <line class="ch-cap ch-${cls}" x1="${px(x1)}" y1="${px(y - 3)}" x2="${px(x1)}" y2="${px(y + 3)}"/>
    <circle class="ch-pt ch-pt-${cls}" cx="${px(x(rate))}" cy="${px(y)}" r="${r}"/>`;
}
