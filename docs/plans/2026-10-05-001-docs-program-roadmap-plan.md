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

#### U1. Jev A/B experiment + writeup

- **Status:** in progress — experiment driver running against OpenRouter
  (`--budget 12.00 --daily-cap 15.00 --seed 7 --jobs 2`), ~90 runs and
  $0.52 recorded at time of writing.
- **Child plans:**
  `docs/plans/2026-09-30-001-feat-ab-experiment-coverage-plan.md`
- **Supporting artifacts:** `experiments/jev-ab.yaml`,
  `docs/runbooks/tonight-ab.md`, `harness.py coverage|report|publish-mark`.
- **Remaining work:** run to budget/coverage completion, then the
  baseline-vs-Jev writeup (pass rates, diff CIs, quality-per-dollar,
  caveats — self-preference, seed, multiplicity).

#### U2. The Score redesign — residual units

- **Status:** nearly done — 22 of 23 units merged. U14 (#148), U15
  (#149), and U19–U21 (#154) all landed after the plan's 2026-10-04
  status block was written. Only U22 (launch video compose + render)
  remains, blocked on Open Questions (Remotion, music license, spend).
- **Child plans:** `docs/plans/2026-10-02-2322-feat-the-score-redesign-plan.md`
  (its dated status block predates the #148/#149/#154 merges; PR
  history is the fresher record).
- **Remaining work:** U22 only — see U7.

#### U3. Post-merge consolidation — residual 4f tier

- **Status:** in progress — Units 1–3 and 4a–4e merged (PRs #159–#164:
  dead weight, pairwise judging + Bradley–Terry, hierarchical bootstrap
  CIs, macro pass rate, contamination canaries, CI eval gate).
- **Child plans:**
  `docs/plans/2026-10-04-001-feat-post-merge-consolidation-plan.md`
- **Remaining work (post-writeup tier):** inspect-log interop export,
  provider telemetry panel, Pareto frontier + composite index, METR
  horizon fit, prompt-perturbation sweeps.

---

#### U9. Safe code reduction (negative-diff program)

- **Status:** in progress — census (#173) and entry-point registry (#177)
  shipped; candidate-report unit planned
  (`docs/plans/2026-10-08-001-feat-dead-code-report-plan.md`), execution
  pending.
- **Child plans:**
  `docs/plans/2026-10-06-001-refactor-safe-code-reduction-plan.md`,
  `docs/plans/2026-10-08-001-feat-dead-code-report-plan.md`.
- **Scope:** evidence-triangulated dead-code removal — codebase-memory
  graph + lint + coverage against an entry-point registry, sliced into
  pure-deletion CI-gated PRs. Agent-dispatchable by design.

### Next

#### U4. Baseline findings writeup + published artifact

- **Status:** pending — gated on U1 completing; the hosted observatory
  surface is already live.
- **Child plans:** none dedicated yet; draws on the U1 report output and
  the hosted observatory (`docs/plans/2026-10-02-001-feat-cloudflare-hosted-observatory-plan.md`,
  live at `obs.shippedit.dev`, runbook `docs/hosted-observatory.md`).
- **Scope:** the writeup is the product — 100+ commits of harness with
  zero published conclusions. Publish scrubbed evidence only.

#### U5. v3 task curation + calibration

- **Status:** in progress — nine real-repo tasks landed (four
  feature-gap, five bugfix pinned at parents of real boltons commits);
  calibration runs and band reassignment remain.
- **Child plans:**
  `docs/plans/2026-09-30-002-feat-v3-realrepo-task-family-plan.md`;
  provenance in `docs/v3-curation.md`.
- **Remaining work:** calibration on reference pairings, difficulty-band
  reassignment from evidence, 1–3 additional tasks.

#### U6. Gemini 4 Argon eval slot

- **Status:** pending — provider-gated; announced Fairwind trusted-tester
  access, not on OpenRouter yet. Entry parked as `~google/gemini-4-argon`
  in `models/default.yaml`; `harness.py models sync` picks it up when
  listed.
- **Child plans:** none.
- **Candidate run:** Argon as orchestrator × best-known worker on the v3
  family; prefer `:batch` pricing if offered.

---

### Later

#### U7. Launch video (Score U22)

- **Status:** blocked — awaiting Open Question answers (Remotion home,
  music license, spend approval) recorded in the Score plan.
- **Child plans:** `docs/plans/2026-10-02-2322-feat-the-score-redesign-plan.md`
  (U21 storyboard feeds it).

#### U8. Program-level caveats to keep honest

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
| `docs/plans/2026-09-30-001-feat-ab-experiment-coverage-plan.md` | U1 | shipped driver; experiment in progress |
| `docs/plans/2026-09-30-002-feat-v3-realrepo-task-family-plan.md` | U5 | partially shipped (9 tasks; calibration pending) |
| `docs/plans/2026-10-02-001-feat-cloudflare-hosted-observatory-plan.md` | U4 | shipped (live at obs.shippedit.dev) |
| `docs/plans/2026-10-02-2322-feat-the-score-redesign-plan.md` | U2, U7 | partially shipped (22/23 units; U22 blocked) |
| `docs/plans/2026-10-03-001-feat-perplexity-eval-patterns-plan.md` | — | shipped (runner hardening, judge contract, Harbor export) |
| `docs/plans/2026-10-04-001-feat-post-merge-consolidation-plan.md` | U3 | partially shipped (Units 1–3, 4a–4e merged; 4f pending) |
| `docs/plans/2026-10-06-001-refactor-safe-code-reduction-plan.md` | U9 | in progress (U1+U2 shipped #173/#177; U3 planned) |
| `docs/plans/2026-10-08-001-feat-dead-code-report-plan.md` | U9 | pending (candidate report) |

---

## Scope Boundaries

- This file indexes and tracks; it does not restate child-plan
  requirements or KTDs.
- Child plans marked `—` in the index are shipped foundations with no
  owning open unit; they stay linked for archaeology.
- New child plans register under the unit that governs them at creation
  time, or a new unit is minted (next unused U-ID) if none fits.
