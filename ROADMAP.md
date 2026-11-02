# orchestral Roadmap

**Program index:** [`docs/plans/2026-10-05-001-docs-program-roadmap-plan.md`](docs/plans/2026-10-05-001-docs-program-roadmap-plan.md)
— Now/Next/Later units (U-IDs) with per-unit status, evidence, and links to
every child plan. This file stays the prose orientation; that file is the
unit ledger.

**What this is:** an eval harness for orchestrator→worker model pairs over
OpenRouter — does a cheap planner plus cheap workers produce frontier-quality
output at a fraction of the cost?

Last updated: 2026-11-02. The program index preserves the older workstreams
and records current priorities and open questions.

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
- Since 2026-09-30: Inspect AI eval-log export (`export --format inspect`, #166),
  measured provider telemetry in the model catalog (#167), pairing-leaderboard
  macro-averaged pass rate (#164), Pareto frontier marker (#168) and
  METR-style time-horizon fit (#169), multi-model judge + embedding
  comparison (`cluster`, #176), honest outcome taxonomy in the observatory
  (#183), agent quickstart (`docs/agent-quickstart.md`, #182), and the
  negative-diff code-reduction program (#172, #173, #177, #185 —
  `docs/analysis/`). Per-unit status lives in the program index above.
- Wide-task member attribution (#198) and completed study records:
  Argon vs GLM ([#200](https://github.com/duketopceo/orchestral/issues/200))
  and the frontier bakeoff ([#201](https://github.com/duketopceo/orchestral/issues/201)).
- The model-study issue form (#206) and opt-in full hosted sync after
  terminal runs (#207). Sync remains best-effort; failures stay dirty.

## Next (priority order)

1. **Make comparisons reproducible:** pin and record serving providers
   ([#179](https://github.com/duketopceo/orchestral/issues/179)). Label
   earlier unpinned comparisons accordingly rather than attributing every
   difference to the model. Assess the draft judge-resampling plan to
   measure disagreement; repeated verdicts are not proof of truth.
2. **Run focused, capped model studies:** verify current routes and prices,
   then smoke Particle-1 ([#202](https://github.com/duketopceo/orchestral/issues/202))
   and Liquid decision models ([#199](https://github.com/duketopceo/orchestral/issues/199)).
   Scope the small Liquid text worker to suitable tasks
   ([#203](https://github.com/duketopceo/orchestral/issues/203)); verify
   Qwen routes ([#205](https://github.com/duketopceo/orchestral/issues/205)).
   Carbon remains gated on verified access and model ID
   ([#204](https://github.com/duketopceo/orchestral/issues/204)).
3. **Publish evidence:** write up completed studies with failure, provider,
   and uncertainty caveats; publish scrubbed examples for v1.0
   ([#5](https://github.com/duketopceo/orchestral/issues/5)).
   The hosted observatory is Access-gated and is not itself a public example.
4. **Operate the hosted mirror:** install the merged harness on publishing
   runners and configure opt-in sync as described in
   `docs/hosted-observatory.md`. Investigate missing run directories before
   attempting artifact recovery. The paid GitHub eval workflow does not
   publish to the mirror.
5. **Reconcile older experiments and task calibration:** check the current
   Jev A/B ledger before resuming; calibrate v3 task difficulty and repair
   missing fixtures as needed. Run paid evaluations only with a defined cap.

## Later

Keep the observatory a thin mirror over the harness. The Score launch video,
optional code reduction, and RL/eval research
([#196](https://github.com/duketopceo/orchestral/issues/196)) are not
prerequisites for publishing benchmark evidence.
