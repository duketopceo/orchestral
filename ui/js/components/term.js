/* Term help: a `?` button that opens a native popover with the plain-language
   definition and a link to the Guide section that holds the full text.
   termHelp(key) returns markup; the popover is placed next to its button when
   it opens (the top layer ignores the button's own box). */
import { esc } from "../util.js";

export const TERMS = {
  judge: ["Judge", "A separate model reads the finished artifact and scores its quality from 0 to 1. The score is advisory until the judge is calibrated against human labels."],
  mechanical: ["Mechanical", "Did it work? A deterministic check: code runs its tests, SQL output matches a reference, a page has its required elements."],
  "low-n": ["Low n", "Too few runs to trust the number. Hatched marks and dimmed rows mean the sample is under the minimum, so read the interval, not the point."],
  ci: ["CI", "The Wilson 95% interval for a pass rate. It is wide on small samples by design."],
  replicates: ["Replicates", "How many times the same task and pairing run. More than one creates a run group so the runs can be compared as a set."],
  pairing: ["Pairing", "An orchestrator that plans and delegates, plus a worker that executes. The leaderboard ranks pairings, not single models."],
};

let seq = 0;

export function termHelp(key) {
  const t = TERMS[key];
  if (!t) return "";
  const pid = `term-${key}-${++seq}`;
  return `<button type="button" class="term-help" popovertarget="${pid}" aria-label="What is ${esc(t[0])}?">
      <svg class="i" aria-hidden="true" focusable="false"><use href="#i-help"/></svg></button>
    <div id="${pid}" class="term-pop" popover><p>${esc(t[1])}</p><a href="#/about?s=${esc(key)}">Read more in the Guide</a></div>`;
}

if (typeof document !== "undefined" && !window.__termPopovers) {
  window.__termPopovers = true;
  document.addEventListener("toggle", e => {
    const pop = e.target;
    if (!pop.classList?.contains("term-pop") || e.newState !== "open") return;
    const btn = document.querySelector(`[popovertarget="${pop.id}"]`);
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const w = Math.min(280, window.innerWidth - 32);
    pop.style.width = `${w}px`;
    pop.style.left = `${Math.max(16, Math.min(r.left, window.innerWidth - w - 16))}px`;
    pop.style.top = `${r.bottom + 6}px`;
  }, true);
}
