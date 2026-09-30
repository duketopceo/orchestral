# orchestral Roadmap

**What this is:** an eval harness for orchestrator→worker model pairs over
OpenRouter — does a cheap planner plus cheap workers produce frontier-quality
output at a fraction of the cost?

Last updated: 2026-09-30.

## Shipped

- Core harness: YAML specs, plan → delegate → assemble, per-pair scoring and cost metering.
- Anti-gaming stack (DUK-24): spec anchors, holdout set, resolved audit gate, tautology detection.
- Observatory SPA: universal story cards, decisions-engine judge, adversarial SQL suite.
- Isolated execution: CubeSandbox (E2B-compatible) adapter — same test, twice, isolated.
- Fail-closed publication: scrubbed payloads both directions; the benchmark answer key never publishes.
- Spec self-check: a spec's references must pass their own grading.
- **v3 task family machinery** (`docs/v3-task-family.md`): pinned real-repo
  fixtures (`fixtures/registry.yaml` → fetch/check/lock), staged in-sandbox,
  graded by a declared `verify.command` with a verifier-owned `test_files`
  oracle overlay. No guest network — deps ride a staged `wheelhouse/`.

## Next (priority order)

1. **Jev A/B lane** — the headline experiment: same spec, same seed, two arms
   (baseline orchestrator vs Jev in the decision loop), 5–10 runs per arm per
   spec, scored by spec self-check. Question: does Jev lift quality 20% or 5%,
   and what does it do to quality-per-dollar? Expect Jev to help routing but
   not rescue weak workers — a negative result here is still publishable.
   Repetition count is dynamic on measured per-cell cost — see
   `docs/plans/2026-09-30-001-feat-ab-experiment-coverage-plan.md`.
   Driver shipped: `harness.py experiment` (batched paired arms, live-spend +
   error + diff-CI gates), `coverage` (done/pending/aborted/posted ledger),
   `publish-mark`, the observatory Experiment section, and
   `docs/runbooks/tonight-ab.md`. What remains is executing the run.
2. **v3 task curation + calibration** — 10–12 real-repo tasks across
   bugfix/feature-gap/revert/refactor, calibrated ~30/45/25 difficulty split.
   Machinery and three feature-gap tasks (`v3-mi-run-length`,
   `v3-mi-chunked-by`, `v3-boltons-deep-merge`) are in; the set isn't.
3. Write up the baseline findings (100+ commits of harness, zero published
   conclusions — the writeup is the product).
4. Publish the harness + findings as the artifact.

## Cut

Anything that is not the A/B experiment or the writeup. The harness is
feature-complete for the question it was built to answer.
