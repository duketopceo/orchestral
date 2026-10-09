---
title: "feat: Observatory honest outcomes — one verdict axis, evidence-first cells"
type: feat
date: 2026-10-08
---

# feat: Observatory honest outcomes — one verdict axis, evidence-first cells

## Summary

Give every run a single honest **outcome** — one closed enum answering "what
happened?" — and make every observatory surface consume it. The result: pass
rates, cells, chips, and attention items all agree on what counts as evidence
and what was never a fair attempt. This is a thin observability-layer change:
derivation and presentation only, no new orchestration.

## Problem Frame

The request was "the UI should answer: which orchestrator, which worker, on
which task, scored what — and there are so many inconclusive and failed
runs." Measurement of the real store (881 runs) corrects that premise:

- **"So many inconclusive" is a mirage.** Of 825 finished runs, only 15 lack
  a verdict — all are dry-runs, unjudged *by design*. `judge_state` already
  renders them "Not judged (dry run)". The perception comes from genuinely
  different things sharing visual weight.
- **"So many failed" is mostly real signal.** 385/810 scored runs are honest
  judge fails (~47%) — that is the finding, not noise to soften.
- **Three things are wearing the same "failed" red chip.** `status=failed`
  mixes `transport`/auth (infra — never a fair attempt), `malformed_output`
  (20 runs — no parseable artifact), and `exception:validation` (19 — run
  died at contract-check). Meanwhile bare `validation` failures are
  `status=finished` *with* a deterministic score. Same word, opposite
  semantics depending on the `exception:` prefix.
- **13 rows say "running" forever.** Killed by the budget stop on Oct 6;
  `abandon_run` exists but nothing auto-classifies them. They pollute Live
  and counts.
- **Cell counts lie by inclusion.** `task_matrix_payload` sets `n =
  len(cell)` — every run — while `pass_rate` uses only finished. A cell
  reading "13 runs, 3 passed" implies 23% when 13 of those were dry-runs,
  ghosts, or invalid: the honest rate may be 100% of 3 real attempts.

So the deliverable is not "score the unscored" (there is almost nothing
unscored) and not a wholesale IA rebuild (a task×pairing matrix already
exists on Now and Leaderboard). It is: **make outcomes honest, then make the
existing cells answer-first.**

Decisions already made with the user: `malformed_output` (no parseable
artifact) goes to a separate **invalid** bucket — excluded from rates,
shown explicitly, not auto-failed and not judge-fed. Re-scoring stays
harness-side (`backfill_judgments`); the observatory links, never executes.

---

## Requirements

- R1. One derived `outcome` per run, from one function, consumed by every
  surface — no view re-derives status semantics.
- R2. Rates and leaderboard numbers count **evidence runs only**
  (pass + fail). Invalid, infra, dry, lost, cancelled, inconclusive are
  excluded — and their excluded count is shown, not hidden.
- R3. Every non-evidence outcome names *why* in plain language
  ("the output never became a judgeable artifact", not
  `exception:malformed_output`).
- R4. Honest-invalid runs stay distinguishable from infra outages — a
  broken task type must be visible as such (24 of 43 exception-failures
  are on `multi-file-site`).
- R5. Thin layer: no writes to run state, no re-scoring, no new harness
  semantics. Outcome is derived at read time and remains reversible.
- R6. The judge-axis taxonomy (`judged|inconclusive|not_judged|not_
  judgeable|unreadable`) is preserved — outcome composes it, doesn't
  replace it.

## Key Technical Decisions

- **Outcome is a sibling to `judge_state`, not a merge.** `judge_state`
  answers "did the judge produce a verdict?"; `outcome_state` answers
  "what kind of result is this run?" A run can be `invalid` and
  `not_judgeable`, or `fail` and `judged`. Chips can show both axes where
  both matter.
- **Closed enum, derived at read:**

  | outcome | condition | evidence |
  |---------|-----------|----------|
  | `pass` | finished, `passes` truthy | yes |
  | `fail` | finished, scored fail (incl. bare `validation`) | yes |
  | `inconclusive` | finished, verdict attempted, unusable | no — shown |
  | `invalid` | failed: `malformed_output`, `empty_output`, `exception:validation`, `unknown` | no — shown |
  | `infra` | failed, `_is_infra(reason)` | no — shown |
  | `dry` | `dry_run` | no — hidden by default |
  | `lost` | `running` + heartbeat stale beyond a longer `LOST_AFTER` threshold, or annotated aborted | no — shown in Live only |
  | `cancelled` | status | no |
  | `running` | actually live | n/a |

- **`lost` is read-time derivation, not a store migration.** A run past
  `LOST_AFTER` (e.g. 1h without a heartbeat — the ghosts have no finished_at
  at all) renders `lost` without touching `runs.status`. The 13 existing
  ghosts classify correctly on the next read; a separate optional sweep
  command can persist the annotation later. This keeps R5.
- **Cell payloads carry the histogram, not just rates.** `task_matrix`
  cells gain `outcomes: {pass, fail, invalid, infra, inconclusive, dry,
  lost}` alongside `pass_rate` (now computed over evidence runs only) —
  viewers see both "what the cell claims" and "what was excluded".
- **Copy is part of the feature.** Each outcome ships with a one-line
  plain-words definition used in tooltips and the About/legend surface;
  names like "invalid" are load-bearing, not decoration.

## High-Level Technical Design

```mermaid
flowchart LR
    M[RunMeta + report.json + heartbeat] --> OS[outcome_state<br/>closed enum + reason]
    OS --> API1[/api/runs, /api/run/]
    OS --> API2[/api/matrix cells:<br/>evidence rate + outcome histogram]
    OS --> API3[/api/overview, needs_look]
    OS --> API4[/api/leaderboard, /api/experiment]
    API1 --> UI1[chips: one outcome chip,<br/>plain-words tooltip]
    API2 --> UI2[cell: pass-rate + n evidence,<br/>muted excluded count]
    API2 --> UI3[cell click -> runs view<br/>pre-filtered to that cell]
    API3 --> UI4[needs_look + task-health item]
```

## Implementation Units

### U1. `outcome_state()` — the enum and its derivation

- **Goal:** One function, one enum, every terminal run classified.
- **Requirements:** R1, R3, R6
- **Files:** `orchestral/web/state.py` (`outcome_state(meta, now=None) ->
  (outcome, reason)` next to `judge_state`), `_public_run` gains
  `outcome`/`outcome_reason`; `tests/test_web_outcomes.py` (new — or
  extend `tests/test_runs_payload.py`, whichever fixture style fits).
- **Approach:** Pure function over `RunMeta` + `_judge_block` +
  `last_heartbeat`, mirroring `judge_state`'s closed-taxonomy shape
  (docstring explains why each state exists, same as its sibling).
  Ordering matters: `dry` and `lost` are checked before terminal buckets;
  `exception:` prefix distinguishes died-in-harness from scored.
- **Patterns to follow:** `judge_state` (`orchestral/web/state.py:1016`).
- **Test scenarios:**
  - finished+passes => `pass`; finished+scored-fail => `fail`;
    finished+bare-`validation` => `fail` (it was scored).
  - failed+`exception:malformed_output` => `invalid`;
    failed+`transport` => `infra`; the `exception:` prefix on a scored
    word does not silently re-bucket.
  - dry_run finished => `dry`; running+stale-past-LOST_AFTER => `lost`;
    running+fresh heartbeat => `running`.
  - Every outcome's `reason` string is non-empty for non-evidence states.
- **Verification:** unit tests cover every enum member; no existing test
  changes semantics (payload gains a field, doesn't alter one).

### U2. Evidence counting — cells, leaderboard, overview

- **Goal:** Rates mean "of real attempts"; excluded runs are visible as
  excluded.
- **Requirements:** R2, R4
- **Dependencies:** U1
- **Files:** `orchestral/web/state.py` (`task_matrix_payload`,
  `overview_payload`, `leaderboard_rows`/pairing aggregation where the
  same bug lives), `tests/test_web_now.py`, `tests/test_web_experiment.py`.
- **Approach:** Matrix/leaderboard pass-rates and `n` computed over
  evidence outcomes only; cell gains the outcome histogram; overview's
  run-count split by outcome. Dry runs default-excluded everywhere they
  aren't already.
- **Test scenarios:**
  - Fixture cell with 3 pass + 2 invalid + 1 dry => `pass_rate` 1.0 over
    n=3 evidence, histogram shows the excluded 3.
  - Leaderboard pass-rate denominator unchanged when ghost/invalid rows
    are added to the fixture set.
- **Verification:** `GET /api/matrix` on the live store: `multi-file-site`
  cells show a large invalid share instead of a deflated rate.

### U3. Rendering — outcome chip, filters, cell drill-down

- **Goal:** The enum becomes the thing a reader sees.
- **Requirements:** R1, R3
- **Dependencies:** U1, U2
- **Files:** `ui/js/chips.js` (`outcomeChip` alongside `statusChip`/
  `judgeChip`), `ui/js/views/runs.js` (outcome column/filter — default
  hides `dry` and shows non-evidence distinctly), `ui/js/views/run.js`
  (header shows outcome + reason), `ui/js/views/now.js` and
  `ui/js/views/leaderboard.js` (cells render evidence-n + excluded
  marker; cell links to `#/runs?task=…&pairing=…` filtered view),
  `ui/js/views/about.js` or a legend (plain-words outcome definitions).
- **Approach:** One chip component; color grammar reuses existing chip
  classes (`chip-pass/fail/warn/dim/info`) with `invalid`/`infra`/`lost`
  mapping to dim/info so honest fails stay the only red. Cell click is a
  link to the existing runs view with query params — no new route.
- **Test scenarios:**
  - `runs?outcome=invalid` (or equivalent filter param) lists only
    invalid runs.
  - Browser test: clicking a matrix cell lands on the filtered runs list
    (extend `test_web_*_browser` pattern if one exists for matrix).
  - Copy rules: outcome names/reasons pass the existing
    user-visible-string lint (ASCII-safe, no banned glyphs).
- **Verification:** visual pass on the live store — the 13 ghosts render
  "lost", dry-runs stop reading as inconclusive, `multi-file-site` shows
  its invalid cluster.

### U4. Task-health attention + ghost reconciliation

- **Goal:** Invalid-rate clustering becomes a named signal, and Live
  stops lying about dead runs.
- **Requirements:** R2, R4
- **Dependencies:** U1
- **Files:** `orchestral/web/state.py` (`needs_look_items`,
  `live_runs`), `tests/test_web_now.py`.
- **Approach:** `needs_look` gains a `task_health` item when one task's
  invalid share crosses a threshold (e.g. ≥3 invalid and ≥25% of its
  terminal runs — the multi-file-site cluster trips it immediately);
  Live rows render `lost` state with the abandon affordance still on
  offer for genuinely-stalled-but-alive runs.
- **Test scenarios:**
  - Fixture with N invalid on one task => one `task_health` item naming
    the task; spread-across-tasks invalids produce none.
  - A stale-forever `running` row renders `lost`, not `running`.
- **Verification:** `needs_look` on the real store names
  `multi-file-site` without prompting.

---

## Scope Boundaries

### Deferred to Follow-Up Work

- **Diagnosing `multi-file-site` itself** — U4 makes the cluster visible;
  whether the task spec or the workers are broken is a separate,
  content-level investigation.
- **Re-scoring/backfilling inconclusive runs** — harness-side
  (`backfill_judgments`); the observatory links to runs but never triggers
  judging (thin-layer rule).
- **Persisting `lost` to the store** — read-time derivation suffices; a
  reaper command is a separate chore if ops want it.
- **Full IA rebuild / new views** — the matrix and leaderboard already
  exist; this plan upgrades them rather than adding surfaces.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `exception:` prefix semantics are subtler than mapped (e.g. `exception:validation` vs bare `validation` both exist in real data) | U1 tests enumerate both forms from real rows; reason strings preserve the raw `failure_reason` |
| Excluding invalid from rates inflates scores on broken task types | Histogram keeps excluded counts visible; U4's task-health item surfaces clustering |
| `lost` threshold flags a legitimately-slow long run | `LOST_AFTER` >> `STALL_AFTER_S`; lost is a display state, never a write (R5) |
| Judge-axis chips and outcome chip confuse each other | U3 copy pass names the axes differently ("Result" vs "Judge") |

## Verification

New `tests/test_web_outcomes.py` green; existing web payload tests green;
live-store smoke: `/api/runs` rows all carry `outcome`, the 13 ghosts read
`lost`, matrix cells on `multi-file-site` expose the invalid cluster.
Standard gate: `unittest discover -s tests`, `ruff check .`, `mypy
orchestral harness.py`, browser tests.

## Open Questions

- Exact `LOST_AFTER` value — pick from heartbeat data at U1 (ghosts are
  days old; live runs beat within minutes, so the window is wide).
- Whether `inconclusive` counts toward anything beyond display — currently
  excluded from rates; if a judge-rerun cadence lands later, revisit.
- Does the Now landing keep its current band order once cells are honest?
  If the honest heatmap is the answer-first surface, it may deserve to
  lead — reviewer call, not a blocker.
