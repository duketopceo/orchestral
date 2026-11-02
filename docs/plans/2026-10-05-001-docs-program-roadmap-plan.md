---
title: "Program roadmap — Now/Next/Later index of all orchestral plans"
type: docs
date: 2026-10-05
---

# Program roadmap — Now/Next/Later index of all orchestral plans

## Summary

One program-level plan that indexes every child plan in `docs/plans/` into
Now / Next / Later units with stable U-IDs, per-unit status, and evidence
pointers. `ROADMAP.md` stays the prose orientation; this file is the
trackable unit ledger `ce-work` and sitrep-style readers consume.

## Problem Frame

`ROADMAP.md` describes intent but carries no unit IDs, no per-item status,
and no links into the 24 plan files that actually govern the work.
A returning agent (or a second orchestrator) cannot tell which plan is
live, which shipped, or what evidence proves it without reading git
archaeology. This plan is the index that closes that gap — one read gives
the program state and every unit's child-plan source of truth.

---

## Requirements

- R1. Every unit below carries a stable U-ID that is never renumbered;
  new units take the next unused number.
- R2. Every unit names its child plan(s) by repo-relative path, or states
  that none exists.
- R3. Every unit records status (`done`, `in progress`, `pending`,
  `blocked`) plus the evidence that proves it — merged PR numbers,
  verified run/output artifacts, or branch names.
- R4. Units are grouped Now / Next / Later by program priority, not by
  plan-file chronology.
- R5. `ROADMAP.md` links this file as its first reference.

## Key Technical Decisions

- **Status lives in unit bodies, not frontmatter.** The plan-section
  contract forbids a `status` lifecycle field; per-unit status is content,
  not metadata, so it goes in the unit record alongside evidence.
- **Units are program workstreams, not code units.** A unit here groups
  child plans and residual work toward one outcome; each still consumes
  `ce-work` normally when its remaining work is code-shaped.
- **This file is the index, not the spec.** Child plans keep their own
  requirements, KTDs, and unit IDs; this file links and tracks them.
  Conflicts resolve in the child plan, then this index is updated.

---

## Implementation Units

### Now

Current priorities are provider-stable comparisons (U10), focused model
studies (U11), judge uncertainty (U13), and scrubbed publication (U4).
Older units remain below for continuity; their historical ordering does not
override `ROADMAP.md`.

#### U10. Provider-stable comparisons

- **Difficulty:** medium - route configuration and provenance must work
  without breaking other OpenAI-compatible providers.
- **Feasibility:** OpenRouter routing and response metadata must be verified
  against the active API before implementation.
- **Simpler alternative:** Record unresolved provider provenance and label
  unpinned comparisons; do not claim that this makes them reproducible.
- **Status:** pending - [#179](https://github.com/duketopceo/orchestral/issues/179).
- **Child plans:** none.
- **Remaining work:** support optional pinned routes, record the serving
  provider when reported, and qualify earlier unpinned results.

#### U11. Focused model studies

- **Difficulty:** medium - candidate routes, task fit, costs, and failure
  classifications need verification before expanding a matrix.
- **Feasibility:** depends on live provider access, suitable model serving
  paths, sandbox fixtures, and an explicit spend cap.
- **Simpler alternative:** one smoke cell per viable candidate before a
  full comparison; stop on provider or runtime failures.
- **Status:** pending - Particle-1 [#202](https://github.com/duketopceo/orchestral/issues/202),
  Liquid decision models [#199](https://github.com/duketopceo/orchestral/issues/199),
  Liquid text worker [#203](https://github.com/duketopceo/orchestral/issues/203),
  Qwen route verification [#205](https://github.com/duketopceo/orchestral/issues/205),
  and Carbon access [#204](https://github.com/duketopceo/orchestral/issues/204).
- **Child plans:** `docs/plans/2026-10-17-001-feat-wide-tasks-benchmark-plan.md`
  for the wide-task family; individual study issues define their own controls.
- **Remaining work:** verify routes and prices, use task-appropriate baselines,
  hold comparison controls fixed, and report uncertainty and cost per pass.

#### U13. Judge resampling and uncertainty

- **Difficulty:** medium - independent samples, cache behavior, aggregation,
  and per-sample cost need a consistent contract.
- **Feasibility:** draft plan exists; the design and current implementation
  need review before work begins.
- **Simpler alternative:** report existing judge disagreements and uncertainty
  without claiming repeated verdicts establish truth.
- **Status:** pending - the plan was added in #189; no `--judge-samples`
  implementation was found in the harness at this checkpoint.
- **Child plans:** `docs/plans/2026-10-12-001-feat-judge-resampling-plan.md`.
- **Remaining work:** validate the draft's sampling and aggregation rules,
  cache separation, and cost controls before implementation.

#### U1. Jev A/B experiment + writeup

- **Difficulty:** medium - the driver exists; what is left is running paired arms to budget and reading CIs honestly.
- **Feasibility:** Current spend and coverage need rechecking; v3 code cells
  require the isolated runtime and fixtures, and CIs may stay inconclusive.
- **Simpler alternative:** Report only the non-code cells first (they already run); skip the v3 code cells until the runtime is stable. Nothing to delete.
- **Category:** existing experiment; not a prerequisite for publishing
  other completed studies.
- **Status:** pending reconciliation — last recorded as relaunched 2026-10-09 with the cube-env
  contract sourced after the first run stopped with 38 cells pending
  (all v3 code cells had refused on missing `ORCHESTRAL_CODE_RUNTIME`;
  the aborted annotations were cleared so those cells resume). Spend to
  that checkpoint was ~$3.28 of $12; completed non-code cells were
  `inconclusive`. Current coverage and spend have not been revalidated.
- **Child plans:**
  `docs/plans/2026-09-30-001-feat-ab-experiment-coverage-plan.md`
- **Supporting artifacts:** `experiments/jev-ab.yaml`,
  `docs/runbooks/tonight-ab.md`, `harness.py coverage|report|publish-mark`.
- **Remaining work:** inspect the current ledger before deciding whether to
  resume within a fresh approved cap; report pass rates, diff CIs,
  quality-per-dollar, and caveats without treating an inconclusive result
  as a win.

#### U2. The Score redesign — residual units

- **Difficulty:** easy - only the video compose/render is left.
- **Feasibility:** Remotion hosting, music licence and spend approval are operator decisions, not code.
- **Simpler alternative:** Ship the storyboard as a static SVG/PNG sequence and cut the render; the Score UI already carries the story.
- **Status:** nearly done — 22 of 23 units merged. U14 (#148), U15
  (#149), and U19–U21 (#154) all landed after the plan's 2026-10-04
  status block was written. Only U22 (launch video compose + render)
  remains, blocked on Open Questions (Remotion, music license, spend).
- **Child plans:** `docs/plans/2026-10-02-2322-feat-the-score-redesign-plan.md`
  (its dated status block predates the #148/#149/#154 merges; PR
  history is the fresher record).
- **Remaining work:** U22 only — see U7.

#### U3. Post-merge consolidation — residual 4f tier

- **Difficulty:** hard - remaining analysis surfaces need credible evidence.
- **Feasibility:** Needs enough real runs to be meaningful; METR horizon fit and perturbation sweeps add model spend; inspect-log schema is upstream-owned.
- **Simpler alternative:** Defer all of it until the writeup exists. an Inspect export already exists as `export --format inspect`; do not build a second path.
- **Status:** in progress — Units 1–3 and 4a–4e merged (#159–#164).
  Inspect export (#166), catalog provider telemetry (#167), Pareto marker
  (#168), and METR fit (#169) have also shipped.
- **Child plans:**
  `docs/plans/2026-10-04-001-feat-post-merge-consolidation-plan.md`
- **Category:** declared post-writeup — maintain-mode improvements, not
  build blockers.
- **Remaining work (post-writeup tier):** assess the remaining composite
  index and prompt-perturbation work against the child plan; do not rebuild
  the shipped surfaces.

---

#### U9. Safe code reduction (negative-diff program)

- **Difficulty:** medium - mechanical, but each deletion needs triangulated evidence.
- **Feasibility:** Dynamic dispatch and entry-point registry gaps cause false positives; every slice must pass the CI gate.
- **Simpler alternative:** Start from `scripts/dead_code_report.py` output and delete only tier-1 candidates; skip anything the entry-point registry cannot explain.
- **Category:** optional hygiene — negative-diff debt paydown that makes
  maintain mode cheaper; agent-dispatchable, no model spend. Not a writeup
  blocker.
- **Status:** in progress — census (#173), entry-point registry (#177),
  and candidate report (#185) shipped; further removal remains optional.
- **Child plans:**
  `docs/plans/2026-10-06-001-refactor-safe-code-reduction-plan.md`,
  `docs/plans/2026-10-08-001-feat-dead-code-report-plan.md`.
- **Scope:** evidence-triangulated dead-code removal — codebase-memory
  graph + lint + coverage against an entry-point registry, sliced into
  pure-deletion CI-gated PRs. Agent-dispatchable by design.

### Next

#### U4. Baseline findings writeup + published artifact

- **Difficulty:** medium - the writing is the work; the hosted surface is live.
- **Feasibility:** Completed studies can be written up independently of U1.
  Cloudflare Access gates `obs.shippedit.dev`; scrub must keep answer keys out.
- **Simpler alternative:** Publish one README-linked report page from `reports/` rather than a bespoke site; reuse the static snapshot (`scripts/build-static-snapshot.py`).
- **Category:** critical path — the product.
- **Status:** pending — completed study records #200 and #201 can seed a
  writeup. Public scrubbed examples remain open in #5; the hosted
  observatory is live but Access-gated.
- **Child plans:** none dedicated yet; draws on the U1 report output and
  the hosted observatory (`docs/plans/2026-10-02-001-feat-cloudflare-hosted-observatory-plan.md`,
  live at `obs.shippedit.dev`, runbook `docs/hosted-observatory.md`).
- **Scope:** publish scrubbed evidence with provider, uncertainty, and
  infrastructure-failure caveats.

#### U5. v3 task curation + calibration

- **Difficulty:** medium - curation is manual and calibration costs model runs.
- **Feasibility:** Pinned upstream repos and licences (boltons), no guest network so wheelhouse staging must stay current, spend for calibration pairings.
- **Simpler alternative:** Skip band reassignment; ship the nine tasks with bands marked uncalibrated, as the plan already allows.
- **Category:** optional for the writeup — same-cell A/B needs no
  difficulty bands to be valid; uncalibrated bands stay a caveat.
- **Status:** in progress — nine real-repo tasks landed (four
  feature-gap, five bugfix pinned at parents of real boltons commits);
  calibration runs and band reassignment remain.
- **Child plans:**
  `docs/plans/2026-09-30-002-feat-v3-realrepo-task-family-plan.md`;
  provenance in `docs/v3-curation.md`.
- **Remaining work:** calibration on reference pairings, difficulty-band
  reassignment from evidence, 1–3 additional tasks.

#### U6. Gemini 4 Argon eval slot

- **Difficulty:** easy - initial access and evaluation are complete.
- **Feasibility:** verify current route and price before any new run.
- **Simpler alternative:** reuse the recorded studies rather than rerun
  without a new question.
- **Category:** completed initial evaluation; future studies belong in U11.
- **Status:** done - Argon is in `models/default.yaml`; wide comparison
  [#200](https://github.com/duketopceo/orchestral/issues/200) and bakeoff
  [#201](https://github.com/duketopceo/orchestral/issues/201) are closed.
- **Child plans:** `docs/plans/2026-10-17-001-feat-wide-tasks-benchmark-plan.md`.
- **Follow-up:** the original v3 comparison is a separate question; check
  existing coverage before planning any additional paid cells.

#### U12. Hosted publication operations

- **Difficulty:** easy - the full sync path and completion hook shipped.
- **Feasibility:** each publishing runner needs the updated harness, opt-in
  setting, credential, and access to the mirror.
- **Simpler alternative:** run the existing manual `sync --push` path.
- **Status:** in progress - #207 merged; an automatic-sync smoke and
  count/cost parity were verified. Four missing-directory entries remained
  dirty at that checkpoint.
- **Child plans:** `docs/plans/2026-10-02-001-feat-cloudflare-hosted-observatory-plan.md`.
- **Remaining work:** configure intended runners, reconcile missing run
  directories if their artifacts are needed, and decide separately whether
  paid CI evals should publish. Sync remains best-effort and synchronous.

---

### Later

#### U7. Launch video (Score U22)

- **Difficulty:** hard - video production tooling plus assets.
- **Feasibility:** Operator decisions on Remotion, music licence and spend.
- **Simpler alternative:** Replace with a recorded `capture-ui-matrix.py` walkthrough (GIF/WebM); no new toolchain.
- **Category:** external + decision-gated — blocked on operator answers,
  not code.
- **Status:** blocked — awaiting Open Question answers (Remotion home,
  music license, spend approval) recorded in the Score plan.
- **Child plans:** `docs/plans/2026-10-02-2322-feat-the-score-redesign-plan.md`
  (U21 storyboard feeds it).

#### U8. Program-level caveats to keep honest

- **Difficulty:** easy - wording only.
- **Feasibility:** None; these are standing caveats.
- **Simpler alternative:** Keep as one caveats block that the writeup includes verbatim; do not grow into features.
- **Category:** standing — caveats by design, never a deliverable.
- **Status:** pending — standing items, not a deliverable unit: judge
  self-preference in Jev-judged cells, seeds as bookkeeping only,
  multiplicity across cells, self-executing orchestrator edge cases.
  These stay caveats in every writeup rather than becoming features.
- **Child plans:** none.

---

## Child-plan index (all plans, by governing unit)

| Plan | Unit | Status |
|------|------|--------|
| `docs/plans/2026-09-11-001-feat-v02-media-gallery-plan.md` | — | shipped (media task types + gallery) |
| `docs/plans/2026-09-11-002-feat-v03-judgment-plan.md` | — | shipped (judge + refinement loop) |
| `docs/plans/2026-09-11-1927-feat-v10-shared-tool-plan.md` | — | shipped (open-source harness shape) |
| `docs/plans/2026-09-12-2136-feat-video-task-type-plan.md` | — | shipped (video task type) |
| `docs/plans/2026-09-13-0046-feat-multi-file-task-type-plan.md` | — | shipped (multi-file zip tasks) |
| `docs/plans/2026-09-13-1800-observability-v2-plan.md` | — | shipped (events/report observability) |
| `docs/plans/2026-09-14-0104-replicates-variance-plan.md` | — | shipped (N-run replicates, CIs) |
| `docs/plans/2026-09-14-0115-tui-plan.md` | — | shipped (`harness.py tui`) |
| `docs/plans/2026-09-16-0214-serve-web-gui-plan.md` | — | superseded by 2026-09-16-1137 |
| `docs/plans/2026-09-16-1137-feat-serve-web-gui-plan.md` | — | shipped (`harness.py serve`) |
| `docs/plans/2026-09-17-001-feat-evidence-v2-plan.md` | — | shipped (evidence v2) |
| `docs/plans/2026-09-18-001-feat-agentic-workers-judge-axis-plan.md` | — | shipped (agentic workers, judge axis, badges) |
| `docs/plans/2026-09-22-001-feat-observatory-explainability-plan.md` | — | shipped (explainability, data viz) |
| `docs/plans/2026-09-23-001-feat-universal-observatory-cards-plan.md` | — | shipped (story cards) |
| `docs/plans/2026-09-26-001-feat-e2b-sandbox-backend-plan.md` | — | shipped (E2B-compatible backend) |
| `docs/plans/2026-09-27-001-fix-benchmark-spec-integrity-plan.md` | — | shipped (spec anchors, reference-verified grading) |
| `docs/plans/2026-09-29-001-fix-e2b-sdk-v1-compat-plan.md` | — | shipped (SDK v1/v2 compat) |
| `docs/plans/2026-09-29-002-feat-cubesandbox-durable-integration-plan.md` | — | shipped (durable CubeSandbox runtime) |
| `docs/plans/2026-09-30-001-feat-ab-experiment-coverage-plan.md` | U1 | shipped driver; current experiment status needs reconciliation |
| `docs/plans/2026-09-30-002-feat-v3-realrepo-task-family-plan.md` | U5 | partially shipped (9 tasks; calibration pending) |
| `docs/plans/2026-10-02-001-feat-cloudflare-hosted-observatory-plan.md` | U4, U12 | shipped; runner opt-in and missing artifacts remain |
| `docs/plans/2026-10-02-2322-feat-the-score-redesign-plan.md` | U2, U7 | partially shipped (22/23 units; U22 blocked) |
| `docs/plans/2026-10-03-001-feat-perplexity-eval-patterns-plan.md` | — | shipped (runner hardening, judge contract, Harbor export) |
| `docs/plans/2026-10-04-001-feat-post-merge-consolidation-plan.md` | U3 | partially shipped; assess remaining 4f work |
| `docs/plans/2026-10-06-001-refactor-safe-code-reduction-plan.md` | U9 | in progress (census, registry, report shipped) |
| `docs/plans/2026-10-07-001-feat-multi-model-eval-tests-plan.md` | — | shipped (#176) |
| `docs/plans/2026-10-08-001-feat-dead-code-report-plan.md` | U9 | shipped (#185) |
| `docs/plans/2026-10-08-002-feat-observatory-honest-outcomes-plan.md` | — | implemented (#183) — outcome taxonomy + evidence denominators |
| `docs/plans/2026-10-08-003-feat-agent-quickstart-plan.md` | — | implemented (#182) — agent onboarding doc |
| `docs/plans/2026-10-12-001-feat-judge-resampling-plan.md` | U13 | draft; implementation pending review |
| `docs/plans/2026-10-13-001-fix-v3-validate-contract-plan.md` | — | shipped (#191) |
| `docs/plans/2026-10-17-001-feat-wide-tasks-benchmark-plan.md` | U6, U11 | initial wide studies completed; future studies tracked separately |

---

## Scope Boundaries

- This file indexes and tracks; it does not restate child-plan
  requirements or KTDs.
- Child plans marked `—` in the index are shipped foundations with no
  owning open unit; they stay linked for archaeology.
- New child plans register under the unit that governs them at creation
  time, or a new unit is minted (next unused U-ID) if none fits.
