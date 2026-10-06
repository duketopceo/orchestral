import { $view } from "../dom.js";
import { icon, rest } from "../components/states.js";
import { termHelp } from "../components/term.js";

/* Guide sections, linked as #/about?s=<key>. Every key a `?` popover points at
   has an element with id s-<key>, so a popover link always lands. */
const TOC = [
  ["axes", "The two axes"], ["judge", "Judge states"], ["naming", "Names"],
  ["numbers", "Reading the numbers"], ["legend", "Glyphs and Rests"],
];

const GLYPHS = [
  ["pass", "Pass", "The mechanical check passed."],
  ["fail", "Fail", "The mechanical check failed."],
  ["cancelled", "Cancelled", "The run was stopped before it finished."],
  ["live", "Live", "The run is in progress."],
  ["stalled", "Stalled", "No event for ten minutes and no live owner."],
  ["inconclusive", "Inconclusive", "The evidence does not decide. Retryable, never a rejection."],
  ["not-judged", "Not judged", "No judge score exists for this run."],
  ["judge", "Judge", "A judge score: the gauge, then the number."],
  ["low-n", "Low n", "Too few samples. Shown hatched."],
  ["frontier", "Frontier", "Pareto frontier: best quality at its price."],
  ["flag-interesting", "Flagged", "Marked to hold here (a fermata)."],
];

const RESTS = [
  ["empty", "Empty", "Nothing here yet."],
  ["nomatch", "No match", "A search or filter found nothing."],
  ["missing", "Missing", "The item is outside this data set."],
  ["error", "Error", "The view could not load. Nothing is lost."],
  ["starting", "Starting", "The run is starting."],
];

function scrollToSection(key) {
  const el = key && document.getElementById(`s-${key}`);
  if (!el) return;
  el.scrollIntoView({ block: "start" });
  el.focus({ preventScroll: true });
}

export async function viewAbout(params = new URLSearchParams()) {
  $view.innerHTML = `
    <h1>Guide</h1>
    <p class="guide-lede body-l">orchestral runs the same task through two models: an
    <b>orchestrator</b> that plans and delegates, and a <b>worker</b> that
    executes. It then grades the result twice, on two independent axes.</p>
    <nav class="guide-toc" aria-label="In this guide">${TOC.map(([k, t]) => `<a href="#/about?s=${k}">${t}</a>`).join("")}</nav>

    <article class="guide body-l">
      <section>
        <h2 id="s-axes" tabindex="-1">The two axes</h2>
        <h3 id="s-mechanical" tabindex="-1">Mechanical ${termHelp("mechanical")}</h3>
        <p>Did it work? Binary, deterministic, no opinions. Code runs its own tests,
        SQL output is diffed against a reference, and pages are checked for required elements.</p>
        <h3>Judge ${termHelp("judge")}</h3>
        <p>Is it good? A separate model reads the actual artifact (code, HTML, SQL)
        and scores its quality from 0 to 1. A score at or above the configured bar
        counts as judge-approved.</p>
        <p class="dim">The axes can disagree: a run can pass every check and still be
        mediocre work. Divergence between them is the interesting part, not noise.</p>
      </section>

      <section>
        <h2 id="s-judge" tabindex="-1">Judge states</h2>
        <p>A run's judge axis is always one of five states, and the state is always named.</p>
        <table class="data">
          <tr><th>State</th><th>Meaning</th></tr>
          <tr><td><span class="chip chip-info">Judge 0.83</span></td><td>Scored. The number is the judge's verdict.</td></tr>
          <tr><td><span class="chip chip-dim">Not judged</span></td>
              <td>No judge was run for this run. Older batches predate the judge axis, or it was not configured.</td></tr>
          <tr><td><span class="chip chip-warn">Judge inconclusive</span></td>
              <td>A verdict was attempted but could not be parsed. Retryable, never counts as a rejection.</td></tr>
          <tr><td><span class="chip chip-dim">Not judgeable</span></td>
              <td>No artifact survives to score, so there is nothing to show the judge.</td></tr>
          <tr><td><span class="chip chip-warn">Judge unknown</span></td>
              <td>report.json could not be read, so whether the judge ran is unknown. The verdict may have been lost, not never produced.</td></tr>
        </table>
      </section>

      <section>
        <h2 id="s-naming" tabindex="-1">Names</h2>
        <dl class="guide-names">
          <dt class="mono">code-expr-parser</dt>
          <dd>A task: <code>&lt;type&gt;-&lt;slug&gt;</code>. The type says what is graded (code, sql, html), the slug names the exercise.</dd>
          <dt class="mono">grok47-eval</dt>
          <dd>A run group: one experiment batch. Runs launched together share it so they can be compared as a set.</dd>
          <dt class="mono">0013278fbaa0</dt>
          <dd>A run id: a hash prefix identifying one single attempt.</dd>
          <dt class="mono" id="s-pairing" tabindex="-1">deepseek-v4-pro → gemma-4-31b-it ${termHelp("pairing")}</dt>
          <dd>A pairing: orchestrator plans, worker executes. The leaderboard ranks pairings, not individual models.</dd>
          <dt id="s-replicates" tabindex="-1">Replicates ${termHelp("replicates")}</dt>
          <dd>The same task and pairing run more than once. Replicates above 1 create a run group.</dd>
        </dl>
      </section>

      <section>
        <h2 id="s-numbers" tabindex="-1">Reading the numbers</h2>
        <p><b>Pass %</b> is the mechanical pass rate. <b>Judge</b> is the mean judge score or the count approved.
        <b>Cost</b> is metered provider spend for that cell.</p>
        <h3 id="s-ci" tabindex="-1">CI ${termHelp("ci")}</h3>
        <p>The Wilson 95% interval around a pass rate. It is wide on small samples by design.</p>
        <h3 id="s-low-n" tabindex="-1">Low n ${termHelp("low-n")}</h3>
        <p>Rows under the minimum sample size are dimmed and sorted below full-evidence rows. Marks drawn from too few runs are hatched.</p>
        <h3 id="s-frontier" tabindex="-1">Frontier ${termHelp("frontier")}</h3>
        <p>The Pareto frontier on quality versus cost. A pairing is marked when no
        credible peer is both better on macro pass rate <i>and</i> cheaper per pass.
        Every frontier point is a defensible pick; anything off it is dominated by
        at least one frontier row. Thin and unmetered rows cannot nominate a point.</p>
      </section>

      <section>
        <h2 id="s-legend" tabindex="-1">Glyphs and Rests</h2>
        <p>Every verdict has a shape as well as a word, so nothing depends on colour alone.</p>
        <ul class="legend">${GLYPHS.map(([k, w, m]) =>
          `<li>${icon(k)}<b>${w}</b><span>${m}</span></li>`).join("")}
          <li><svg class="swatch" width="24" height="16" aria-hidden="true"><rect width="24" height="16" fill="url(#hatch)" stroke="currentColor"/></svg><b>Hatch</b><span>Evidence is thin here.</span></li>
        </ul>
        <p>Empty and error states draw a Rest: a bar of music with nothing sounding.</p>
        <ul class="legend legend-rests">${RESTS.map(([k, w, m]) =>
          `<li>${rest(k)}<b>${w}</b><span>${m}</span></li>`).join("")}</ul>
      </section>
    </article>`;
  scrollToSection(params.get("s"));
}
