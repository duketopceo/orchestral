import { $view } from "../dom.js";

export async function viewAbout() {
  $view.innerHTML = `
    <h1>What am I looking at?</h1>
    <p class="page-sub">orchestral runs the same task through two models: an
    <b>orchestrator</b> that plans and delegates, and a <b>worker</b> that
    executes. It then grades the result twice, on two independent axes.</p>

    <div class="panel panel-pad">
      <h2>The Two Axes</h2>
      <table class="data">
        <tr><th>Axis</th><th>What it means</th><th>How it's graded</th></tr>
        <tr><td><b>Mechanical</b></td>
            <td>Did it work? Binary, deterministic, no opinions.</td>
            <td>Code runs its own tests · SQL output is diffed against a
            reference · pages are checked for required elements.</td></tr>
        <tr><td><b>Judge</b></td>
            <td>Is it good? A separate model scores quality from 0 to 1.</td>
            <td>A judge model reads the actual artifact (code, HTML, SQL)
            and scores it. <span class="dim">Score ≥ the configured bar
            counts as judge-approved.</span></td></tr>
      </table>
      <p class="dim">They can disagree: a run can pass every check and still
      be mediocre work. Divergence between the axes is the interesting part,
      not noise.</p>
    </div>

    <div class="panel panel-pad">
      <h2>Judge States</h2>
      <table class="data">
        <tr><th>State</th><th>Meaning</th></tr>
        <tr><td><span class="chip chip-info">Judge 0.83</span></td>
            <td>Scored. The number is the judge's verdict.</td></tr>
        <tr><td><span class="chip chip-dim">Not judged</span></td>
            <td>No judge was run for this run (older batches predate the
            judge axis, or it wasn't configured).</td></tr>
        <tr><td><span class="chip chip-warn">Judge inconclusive</span></td>
            <td>A verdict was attempted but couldn't be parsed. Retryable,
            never counts as a rejection.</td></tr>
        <tr><td><span class="chip chip-dim">Not judgeable</span></td>
            <td>No artifact survives to score, so there is nothing to show the judge.</td></tr>
        <tr><td><span class="chip chip-warn">Judge unknown</span></td>
            <td>report.json could not be read, so whether the judge ran is
            unknown. The verdict may have been lost, not never produced.</td></tr>
      </table>
    </div>

    <div class="panel panel-pad">
      <h2>Naming</h2>
      <table class="data">
        <tr><th>You see</th><th>What it is</th></tr>
        <tr><td class="mono">code-expr-parser</td>
            <td>A task: <code>&lt;type&gt;-&lt;slug&gt;</code>. The type says
            what's being graded (code, sql, html…), the slug names the
            exercise. Titles like "Expression parser" are the same task.</td></tr>
        <tr><td class="mono">grok47-eval</td>
            <td>A run group: one experiment batch. Runs launched together
            share it so they can be compared as a set.</td></tr>
        <tr><td class="mono">0013278fbaa0</td>
            <td>A run id: a hash prefix identifying one single attempt.</td></tr>
        <tr><td class="mono">deepseek-v4-pro → gemma-4-31b-it</td>
            <td>A pairing: orchestrator plans → worker executes. The
            leaderboard ranks pairings, not individual models.</td></tr>
      </table>
    </div>

    <div class="panel panel-pad">
      <h2>Reading the Numbers</h2>
      <p><b>Pass %</b> is the mechanical pass rate. <b>Judge</b> is the mean
      judge score or the count approved. <b>CI</b> is the Wilson 95%
      interval, wide on small samples by design. Rows under the minimum
      sample size are dimmed and sorted below full-evidence rows.
      <b>Cost</b> is metered provider spend for that cell.</p>
    </div>`;
}
