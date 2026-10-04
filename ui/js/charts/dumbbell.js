/* Dumbbell for Compare: baseline interval and candidate interval on one
   0-100% axis, joined by a link from baseline point to candidate point. */
import * as F from "../format.js";
import { linear, px } from "./scale.js";
import { svgFrame } from "./frame.js";
import { intervalMark } from "./interval.js";
import { ofText } from "./text.js";

const PAD = 8, H = 30;

export function dumbbell({ id, a, b, width = 220 }) {
  const x = linear(0, 1, PAD, width - PAD);
  const ya = 9, yb = 21;
  const ra = a.finished ? a.passed / a.finished : null;
  const rb = b.finished ? b.passed / b.finished : null;
  const link = ra != null && rb != null
    ? `<line class="ch-link" x1="${px(x(ra))}" y1="${ya}" x2="${px(x(rb))}" y2="${yb}"/>` : "";
  const body = `<line class="ch-axis" x1="${PAD}" y1="${H - 1}" x2="${width - PAD}" y2="${H - 1}"/>
    ${link}
    ${intervalMark({ x, y: ya, rate: ra, ci: a.ci, lowN: F.lowNCell(a.finished), cls: "base" })}
    ${intervalMark({ x, y: yb, rate: rb, ci: b.ci, lowN: F.lowNCell(b.finished), cls: "pass" })}`;
  const range = c => (c ? `, 95% interval ${F.rangePct(c[0], c[1])}` : "");
  return svgFrame({
    id, width, height: H, cls: "ch-svg ch-dumbbell",
    title: "Pass rate, baseline to candidate",
    desc: `Baseline ${ofText(a.passed, a.finished, ra)}${range(a.ci)}. Candidate ${ofText(b.passed, b.finished, rb)}${range(b.ci)}.`,
    body,
  });
}
