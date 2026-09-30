# orchestral Roadmap

**What this is:** an eval harness for orchestrator→worker model pairs over
OpenRouter — does a cheap planner plus cheap workers produce frontier-quality
output at a fraction of the cost?

Last updated: 2026-09-29.

## Shipped

- Core harness: YAML specs, plan → delegate → assemble, per-pair scoring and cost metering.
- Anti-gaming stack (DUK-24): spec anchors, holdout set, resolved audit gate, tautology detection.
- Observatory SPA: universal story cards, decisions-engine judge, adversarial SQL suite.
- Isolated execution: CubeSandbox (E2B-compatible) adapter — same test, twice, isolated.
- Fail-closed publication: scrubbed payloads both directions; the benchmark answer key never publishes.
- Spec self-check: a spec's references must pass their own grading.

## Next (priority order)

1. **Jev A/B lane** — the headline experiment: same spec, same seed, two arms
   (baseline orchestrator vs Jev in the decision loop), 5–10 runs per arm per
   spec, scored by spec self-check. Question: does Jev lift quality 20% or 5%,
   and what does it do to quality-per-dollar? Expect Jev to help routing but
   not rescue weak workers — a negative result here is still publishable.
2. Write up the baseline findings (100+ commits of harness, zero published
   conclusions — the writeup is the product).
3. Publish the harness + findings as the artifact.

## Cut

Anything that is not the A/B experiment or the writeup. The harness is
feature-complete for the question it was built to answer.
