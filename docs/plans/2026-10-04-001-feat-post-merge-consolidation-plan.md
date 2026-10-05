---
title: Post-merge consolidation — branch cleanup, dead weight, fixes, pull-in roadmap
created: 2026-10-04
status: draft
plan_kind: feature
origin: ad-hoc (post-#152 board-empty sweep + three parallel research agents)
---

# Consolidate after The Score: clear branches, cut dead weight, fix gaps, pull in proven eval features

## Problem frame

The Score redesign + eval-patterns + hosted-observatory merge wave (PRs
#127–#152) landed in ~36 hours. `main` is green and deployed, but the
accumulation is un-swept:

1. **22 unmerged local branches.** Mechanical triage (git cherry +
   residual diffs) shows ~18 are fully superseded — their work landed
   via other routes under different patch IDs — and 2 carry real
   unpicked work. Keeping them costs every future `git branch` a
   cognitive tax and risks a stale branch being re-based onto main.
2. **Merge-drift dead weight.** The monolithic `ui/app.js` era left
   superseded render paths, stale docs, and possibly dead
   endpoints/selectors. (Audit findings inline below.)
3. **Fix list from the merge seam.** Post-merge integration gaps:
   markers, skipped tests, parity gaps across local/hosted/TUI
   surfaces. (Audit findings inline below.)
4. **Publishable-evidence gaps.** External research against
   inspect_ai, HELM, lm-eval, Braintrust, LMArena, Artificial
   Analysis, METR shows a small set of high-fit features orchestral
   lacks — nearly all are *evidence-strength* features, not new
   orchestration, which fits the "thin observatory, publishable
   evidence" goals.

Constraints honored throughout:

- Observatory stays a **thin live-observability layer** — no second
  orchestration engine.
- ROADMAP "Cut" guidance: harness is feature-complete except the A/B
  run + writeup; pull-ins are writeup force-multipliers first.
- No new dependencies without justification (AGENTS.md).
- Never commit secrets, runs/, reports/, or scrubbed data.

## Branch triage (verified 2026-10-04)

Triage method: `git rev-list --count main..b`, `git cherry` (patch-id
equivalence), then `git diff main...b --stat` + spot-reads of the
residual for every branch with `unpicked > 0`. "Superseded" below means
the branch's unique content exists on main under different patch IDs —
verified per branch, not inferred from cherry alone.

### Delete — fully superseded (verified content on main)

| Branch | Why it can go |
| --- | --- |
| `restack-u7` | my restack scratch; u7-shell landed via #145 |
| `feat/score-u4-rstarting-ictus`, `feat/score-u4-icons` | U4 ictus landed via #145 rollup (2-line change) |
| `fix/calibrate-yaml-numeric-id`, `fix/flaky-calibrate-assertion` | landed (quoted run-ids + edge-id tests on main) |
| `docs/redesign-spec-plan` | plan doc only, landed |
| `feat/cube-durable-integration` | content landed |
| `m51`, `m56` | Sep-26 integration carriers, patches equivalent |
| `m65` | `tests/test_publishing_doc.py` on main |
| `m77` | `tests/test_ci_gate_independence.py` + decoupled CI jobs on main |
| `m54` | scrub fail-closed on withheld is on main (`harness.py` scrub path) |
| `feat/observatory-views` | Sep-14 TUI views, superseded |
| `feat/observatory-substrate` | `manifest.py`, `run.cancelled`, pairing leaderboard all on main |
| `fix/e2b-sdk-v1-compat` | signature-dispatch + self-hosted contract on main (`cubeexec.py`) |
| `feat/benchmark-spec-integrity` | `selfcheck`, `metadata.difficulty` (40 specs), `reference:` blocks, `test_selfcheck.py` all on main |
| `feat/card-artifact-render` | 2 commits ⊂ b-s-i; HTML card render landed via U15 |
| `fix/quick-wins-spend-guards` | spend gate + favicon + `test_web_spend_gate.py` on main via U14/U23; residual is dead `ui/app.js` edits — verify contrast CSS bits first |
| `feat/review-terminal-tasks` | `agentexec.py`, `dataset.py`, `terminal.py`, `shots.py`, comparables, spend guard all on main; residual is old-monolith drift — verify `opencode.json` delta (19 lines) first |
| `feat/observatory-explainability` | `dataset.py`, v2 tasks, `test_observatory_cards.py`, title/blurb tags all on main — cherry-pick the docs first (see below) |

### Salvage — real unpicked work

| Branch | Content | Action |
| --- | --- | --- |
| `feat/score-u19-u21` | U19 README lockup + paper Pairings screenshot + social image; U20 motion polish (`ui/js/motion.js` +71, phase fill, number tick, row insert, route crossfade, skeleton); U21 demo storyboard + capture kit (+3241 lines, **built against the current ui/js architecture**) | Fresh work in the right architecture — update from main, open PR |
| `m70` | DUK-90 unrounded-max SQL regression tests (`tests/test_sql_task.py` +230 with standalone `UNROUNDED_MAX_SQL` query), small `orchestral/audit.py` addition, task-spec rounding/tie-break doc rules | Cherry-pick the test + audit additions into a fix PR; `labels-media*.yaml` and `reports/` already on main |

### Cherry-pick-then-delete

| Branch | Worth taking | Then |
| --- | --- | --- |
| `feat/observatory-explainability` | `docs/spec-critique-gpt-5.6-sol.md` (917-line spec critique), `docs/model-roster.yaml` (260-line roster), `docs/plans/*e2b-sandbox*`, `scripts/wave-a*.sh` | Copy wanted docs/scripts onto main or a docs PR; delete branch |

## Implementation units

### Unit 1 — Branch consolidation (merge plan core)

**Goal:** local branch list reflects only live work; nothing unique lost.

**Steps (ordered):**
1. `feat/score-u19-u21`: `git merge origin/main`, resolve conflicts
   (ui/app.css tail append, ui/js/views/card.js/cards.js — U15 card
   work landed in the same files), run gates, push, open PR. This is
   the only salvage branch worth a full PR — it's current-architecture
   UI work with demo capture that feeds the publishable-evidence goal.
2. `m70`: extract `tests/test_sql_task.py` additions + `audit.py` diff
   onto a fresh `fix/sql-regression-tests` branch off main; verify the
   sql specs on main already carry the tie-break/rounding fixes (they
   do — verified), so only test+audit code travels.
3. `feat/observatory-explainability`: copy `docs/spec-critique-gpt-5.6-sol.md`,
   `docs/model-roster.yaml`, `scripts/wave-a*.sh` (if still referenced
   by any docs — else skip) onto a small docs PR; skip if the model
   roster duplicates `models/default.yaml` now.
4. `feat/review-terminal-tasks`: read the `opencode.json` delta — if
   its permission allowlist differs from main's, evaluate; else skip.
5. Delete the 18 superseded branches (local + remote where present).
   Remote refs: most track `origin/` — use `git push origin --delete`
   per branch; `restack-*`, `m5*`, `m6*`, `m7*` are local carriers.
6. Sweep worktrees: `restack-u7`/`restack-u16` worktrees at /tmp can go.

**Verification:** for each branch slated for deletion, record the
one-line evidence (e.g. "selfcheck on main") in the PR description of
the cleanup commit or in this plan — deletions are recoverable from
reflog for 90 days anyway.

### Unit 2 — Dead-weight removal (from audit agent)

The audit confirms the merge boundaries are clean — `web/state.py` is a
genuine shared derivation layer for server/snapshot/CLI; no
second-orchestration-engine drift. What follows is the residue.

**2a. Safe removals — verified zero consumers**

- Root `json-viewer.js` (88 lines): byte-equivalent stale copy of
  `ui/js/components/json-viewer.js`, zero importers — delete.
- `orchestral/web/state.py` `history_rows()` (767) and `run_sections()`
  (323): only callers are their own tests; remove function + test.
- Dead CSS in `ui/app.css` (~30 selectors, zero emitters, zero test
  references): the old `.ph-*` evidence-chain ribbon set (436-459 +
  media rules 707-708 — keep `.ph-strip`, still emitted at run.js:128),
  `.dot*`, `.live-strip`/`.live-card`/`.lc-label`, `.group-grid`/`.gc-*`/
  `.b-*`, `.verdict-strip`, `.cardlist`/`.cl-*`, `.rest-state`, and the
  utility classes `.muted`, `.num`, `.filters`, `.split` (live
  equivalents are `.dim`, `.t-num`, `.facet-bar`).
- Unused exports: `data.mode` (data.js:21), `poller.active`
  (poller.js:68).

**2b. Probably dead — verify contract before removing**

- `timeline` field + `timeline_payload()` (state.py:1153-1178, emitted
  at 1445): zero SPA consumers (run.js renders `d.lanes`); the dead
  `.ph-*` CSS was its renderer — remove **together**. Blocker: it's in
  the hosted snapshot contract (`run/<id>.json`); check for external
  consumers before deleting the field, else deprecate-not-delete.
- `/api/leaderboard` + `leaderboard.json` + `data.leaderboard` (see 3c —
  `leaderboard_rows` itself stays: `harness.py` cards export uses it).
- `overview_payload` fields `leaderboard` and `recent` (state.py:636):
  computed every 5s poll, no readers. It's a published key — check
  whether Now used them pre-redesign; likely deprecate.
- `POST /run` form-post and `POST /run/<id>/cancel` redirect variant
  (server.py:583): deliberate compat surface pinned by tests — removal
  is a product decision, not drift.
- Orphaned icon symbols (`i-latency`, `i-tokens`, `i-flag-dismiss`,
  `i-transcript`, `i-plan`, `i-external`, `i-download`, `i-filter`,
  `i-sort`): pinned by `tests/test_icons.py` exact-set equality —
  deliberate vocabulary over-provisioning for TUI/CLI parity, **do not
  delete** without a DESIGN.md decision.
- `scripts/pull_swebench.py` (orphan), `scripts/wave-a*.sh` (referenced
  only as a cautionary note in a runbook), `scripts/build-static-snapshot.py`
  (working but undocumented dev tool): classify as keep-documented or
  delete in the same pass.

**2c. Bugs masquerading as cleanup targets (fix, don't delete)**

- `leaderboard.js:140` emits `row-thin` for low-sample rows — **no CSS
  rule exists**; the Guide promises dimming that never renders. Add the
  rule or drop the class.
- `leaderboard.js:148` emits `td.e` on high-failure cells — only a dead
  `.ph-name .e` rule exists; style or drop.
- `starting → "empty"` mapping with shipped `#r-starting` (see 3e).

**2d. Useful but buried — docs/discoverability, not code**

- `harness.py sync` (hosted publish path) and `harness.py cards`
  (publishable PNG export) are **absent from README** — README's
  Publishing section stops at local `runs-pub/`. For the
  "stranger publishes scrubbed results" objective this is the missing
  link.
- README Commands table misses 11 registered commands (`recover`,
  `dataset`, `holdout`, `cards`, `judge`, `specaudit`, `claimsaudit`,
  `harbor`, `models`, `doctor`, `sync`) and duplicates `scrub`/
  `calibrate` rows.
- README "Web GUI" §237-247 describes the pre-redesign UI; current
  routes are Now/Runs/Pairings/Compare/Experiments/Publish/Models/
  New/Guide — plus cancel/abandon/flag/thread capabilities it never
  mentions, and the `Ctrl K` palette + `/about` Guide nobody can find.
- `ui/og/default.html`, `capture-ui-matrix.py`, `e2e-hosted.py` are
  wired but undocumented in CONTRIBUTING.md's gate list.
- Stale docstrings: `render.py:3`, `tui/state.py:119`; `DESIGN.md` §2
  current-state audit is pre-redesign — label historical or update.

### Unit 3 — Post-merge fix list (from audit agent)

Severity-ranked findings from a static read-only audit (all cited with
file:line evidence; verify each in execution):

**3a. Broken today — hosted Pairings dead button**
`ui/js/views/leaderboard.js:102` emits a "Download view" anchor to
`/api/shot.png` unconditionally; on the hosted mirror that endpoint is
a 501 by design, so the click downloads a JSON error as `shot.png`.
Fix: wrap in `can("png_capture")` — the identical gate
`ui/js/views/card.js:110` already uses. One-line fix, verified live-site
bug.

**3b. Latent — parity coverage sits outside the required gate**
Every browser test is `skipUnless(HAS_PLAYWRIGHT)`; playwright only
installs in the non-required `browser` job, so the required `test`
green proves nothing about snapshot↔worker parity, the
`HOSTED_KEYS`↔`snapshot.py` key contract, or scrub/browser assertions
(tests/test_serve_browser.py:524-574). Two options, pick one:
  - require the `browser` job (KTD13 documents the open decision), or
  - extract the key-contract assertion into a non-Playwright test so
    the required gate covers it (cheap: `keys.test.mjs`-style Node test
    + a Python test asserting `payloadName`-compatible names from
    `snapshot.py`).

**3c. Latent — dead `leaderboard` data path kept alive across the sync
contract.** `data.leaderboard()` exists in both adapters
(`ui/js/data.js:84,195`), the local endpoint serves it
(`server.py:372`→`state.py:349`), and `snapshot.py:130` emits
`api/leaderboard.json` — but **no view calls it** (`#/leaderboard`
renders `viewLeaderboard` over `data.pairings()`). It's computed on
every sync, served, never read, and pinned by the contract test.
Remove: snapshot key, server route, both adapters, and the
`test_serve_browser.py:568` key assertion (adjust contract test).

**3d. Latent — legacy-bookmark redirects don't cover new routes.**
Local (`server.py:272-281`) and worker (`worker.js:372-377`) redirect
only `/runs`, `/leaderboard`, `/compare`, `/new`, `/run/*` — the
redesign's `/experiment`, `/cards`, `/card`, `/models`, `/about`
paths 404 when hit directly. Extend both redirect tables to all router
paths; keep parity between the two implementations.

**3e. Paper cuts**
- `ui/js/components/states.js:23-26` `starting → "empty"` TODO says the
  `#r-starting` glyph is pending — it shipped (`ui/icons.svg:64`). Map
  it; used by run.js:34, timeline.js:24.
- Root `json-viewer.js` (88 lines) is a stale duplicate of
  `ui/js/components/json-viewer.js` — delete (nothing imports it).
- Docstrings name deleted `ui/app.js`: `render.py:3`,
  `tui/state.py:119-122`.
- `DESIGN.md` §2 current-state audit describes the pre-redesign SPA
  (app.js:1318, viewOverview et al.) — label as historical or update.
- Data-dependent skip: `test_shell_browser.py:332-338` skips the
  baton-ring animation when the corpus lacks a live row — the
  heartbeat fixture (#152) already makes a live row deterministic;
  consider tightening the skip to a hard assertion on the `full` shape.

**3f. Explicit non-findings (don't touch)**
Worker 501s are deliberate read-only posture with every SPA caller
capability-gated; KTD/DUK markers are the tracking convention; the
`NotImplementedError` in openrouter.py is a pinned honest gate; code
exec fail-closed is documented posture — stubs in judge/swe tests are
intentional but would silently hide the switch-on when an isolated
runtime lands (watch item only).

### Unit 4 — Publishable-evidence pull-ins (research verified against repo)

External survey (inspect_ai, HELM, lm-eval, Braintrust, Langfuse,
promptfoo, LMArena, Artificial Analysis, METR) filtered against
ROADMAP + shipped features. What already exists and must not be
re-built: recover/relaunch, decomposed judge contract (v2),
multi-judge + judge cache, calibration, revalidate, holdout arm,
contamination_gap, Wilson/Newcombe CIs on pass rates, Harbor export,
experiment diffing, p50/p95 latency, difficulty labels, in-place
stage-resume (planned, deferred-gated).

Ranked pull-in candidates — each is evidence-strength, none is new
orchestration:

**4a. Pairwise judging + Bradley–Terry ratings** (top pick)
- What: head-to-head verdicts between paired arms' artifacts on the
  same task+seed (position-swapped, tie allowed); BT fit produces
  pairing ratings with CIs — the publishable headline the scalar
  leaderboard can't produce.
- Where: new `judge_pair()` in `orchestral/judge.py` (reuse
  `_extract_json`, `_judge_input`, judge cache w/ schema bump); BT fit
  in `orchestral/stats.py` (pure-Python, ~100 lines, no deps);
  `harness.py judge --pairwise` + backfill over stored paired-arm
  artifacts; leaderboard column in `ui/js/views/leaderboard.js`.
- Cost: medium. Fit: excellent — the experiment driver already
  produces the battle pairs.

**4b. Hierarchical bootstrap CIs on scalar metrics** (METR/HELM)
- What: resample task-families → tasks → attempts; gives CIs for
  `score_mean`, `cost_per_pass`, `successes_per_dollar`, which today
  ship as bare means.
- Where: `orchestral/stats.py` `bootstrap_ci`; consumers
  `CellAggregate`, `PairingAggregate`, `compare_payload`, `diff_verdict`.
- Cost: low. Fit: excellent — defensible intervals on every headline.

**4c. Macro-averaged leaderboard** (HELM groups)
- What: mean of per-task pass rates — each task equal weight. Today's
  pooled ranking is silently weighted by where spend happened.
- Where: `pairing_leaderboard` `macro_pass_rate` + leaderboard column.
- Cost: low (~30 lines). Fit: arguably a correctness fix for
  publishable rankings.

**4d. Contamination canary strings** (Harbor convention)
- What: unique canary GUID per holdout spec; flag runs whose outputs
  echo another spec's canary; export into Harbor `task.toml` free.
- Where: `metadata.canary` in tasks/*.yaml; `audit.py` check;
  runner/report flag; `harbor_export.py` emit.
- Cost: low (~100 lines). Fit: one-line credibility claim.

**4e. CI eval gate** (Braintrust exit-code pattern)
- What: `harness.py gate --baseline <g> --candidate <g>` exit 1 on any
  `regressed` verdict or threshold breach — reuses `compare_payload`
  verdicts verbatim.
- Cost: low. Fit: closes the dogfood loop; zero new computation.

**4f. Post-writeup tier:** Inspect-log interop export
(`orchestral/export.py` → inspect view consumable), measured-vs-
advertised provider telemetry panel (calls table → tok/s p50/p90 +
billed-vs-ratecard ratio on Models), Pareto frontier + labeled
composite index, METR `human_minutes` horizon fit, prompt-perturbation
sweeps via `ablate`.

## Ordering

1. Unit 1 (branch consolidation) — same-day, unblocks nothing but
   removes confusion; u19-u21 PR is the only reviewable artifact.
2. Units 2+3 (dead weight + fixes) — after audit findings land; the
   removals are independent small PRs to keep diffs reviewable.
3. Unit 4 pull-ins in order 4a → 4e — all before/at writeup time; 4f
   items strictly after.

## Verification

- Every deletion decision carries a recorded evidence line (this
  document + cleanup PR body).
- All gates per AGENTS.md: `/tmp/gate-venv` unittest + ruff + mypy;
  `audit --strict` and `selfcheck --execute` where specs/grading move.
- Salvaged branches' PRs get the standard CI matrix; no admin merges.

## Open questions

- u19-u21 may conflict with main in card views (U15 landed same
  files) — resolution risk is low but nonzero.
- Whether `labels-media*.yaml`/`reports/` evidence files should be
  repo-tracked long-term (AGENTS.md says don't commit reports/;
  calibration labels sit at repo root today — decide policy, don't
  drift).
- The `browser` job question (3b): require it, or move the key-contract
  assertion into the required gate. KTD13 owns the decision.
- Hosted snapshot keys are a published contract — whether dead payload
  fields (`timeline`, `overview.leaderboard`/`recent`) may be deleted
  or must be deprecate-only.

## Suggested PR shape

- PR-1 `chore/branch-consolidation`: u19-u21 rebase+PR is separate;
  this PR carries only m70's sql tests+audit and (optionally) the
  explainability docs cherry-pick — plus records branch deletions.
- PR-2 `fix/hosted-shot-gate`: 3a one-liner + 2c unstyled classes +
  3e glyph map — the visible-bug bundle.
- PR-3 `chore/dead-weight`: 2a + 3c (leaderboard path) + 3d redirects
  + doc sweeps — mechanical, reviewable.
- PR-4 `docs/readme-commands`: 2d README/CONTRIBUTING refresh.
- PR-5 `test/parity-in-required-gate`: 3b whichever option is chosen.
- Pull-ins (4a-4e) are each their own plan-sized unit; 4c+4e are the
  cheapest honest wins if only two ship.
