---
title: Jev A/B experiment driver + coverage ledger + BI (batched, cost-scaled reps)
created: 2026-09-30
status: draft (review-revised)
plan_kind: feature
---

# Jev A/B experiment driver + coverage ledger + BI

## Problem frame

The ROADMAP headline — "does Jev in the decision loop lift quality, and at
what quality-per-dollar" — currently has zero protocol and zero coverage
tracking: 100 stored runs are all n=1 anecdotes, the Jev arm has one run,
and nothing knows which cells are done, which are pending, or which
findings have been published.

The user-set budget policy: **rep count scales inversely with cell cost** —
cheap cells get up to ~100 reps, expensive cells fewer — executed in batches
with gates, never a blind burst. A cell is "done" when the arm-comparison
evidence meets a stated standard, not when a fixed count fires.

## Requirements

- **R1 — paired arms, honestly paired.** `harness.py experiment` runs each
  `(task, orchestrator, worker)` cell on two arms per replicate index:
  `baseline` (as today) and `jev` (`--jev-assist`). `Provider.chat` takes no
  seed — seeds do not reach chat providers — so the real pairing is same
  spec + same replicate index + **arms interleaved in time** (baseline then
  jev within each replicate, alternating order across replicates) to bound
  temporal/provider-drift confounds. The recorded `seed` is bookkeeping
  only; the plan claims nothing more.
- **R2 — dynamic rep targets.** Per-cell target =
  `clamp(ceil(cell_budget / est_pair_cost), FLOOR=5, CAP=100)` where
  `est_pair_cost` is the mean billed `runs.total_cost_usd` for the
  **baseline** arm × `JEV_LOAD_FACTOR` (decide calls + possible
  replan + one rework worker call are priced as ~1.3× baseline until
  measured), summed as the pair. Cost estimate is task-scoped — a new
  `RunStore.mean_cell_cost(task, orch, worker)` query; `mean_run_cost`
  exists but is not task-scoped and is reused for fallback only. Cells with
  no history run 1 calibration replicate, then price from actual billed
  cost. `est=0` (unmetered models, `pricing_source=unmetered`) clamps to
  CAP — zero cost divides, it doesn't shrink reps. Estimates recompute
  after every batch (one-directional: a hotter-than-history cell shrinks
  its own target; cheaper-than-history does not grow it).
- **R3 — batched, gated, resumable.** Reps execute in batches of `B`
  (`--batch-size`, default 5; a batch = B replicate indexes × 2 arms,
  interleaved). Between batches: (a) **live spend check** — sum
  `calls.cost_usd` joined to runs (billed view uses `runs.total_cost_usd`,
  in-flight view uses `calls` which meters per call during the run —
  `spend_today` alone is blind to in-flight spend); abort when experiment
  spend ≥ `--budget` or today ≥ `--daily-cap`; (b) error abort — >50% of a
  batch's runs ending in infra error (exception/timeout, not judged-fail)
  → cell `aborted`; (c) early-stop when the **arm-difference** 95% CI
  half-width ≤ `--diff-eps` (default 0.15). The driver catches budget
  exhaustion as a return/exception — it must NOT call `sys.exit` (that is
  `_spend_recheck`'s contract, wrong for composed use).
- **R4 — coverage ledger.** `harness.py coverage` reports the expected
  matrix vs stored runs: cell key `(task, orchestrator, worker)` with arm
  split read from `runs.config` JSON `jev_assist` (not a column — `seed`
  and `jev_assist` live in the config blob; `run_group`/`replicate` are
  indexed columns) → reps/target/pass-rate per arm/difference CI/state
  (`pending|partial|done|aborted`)/published flag. State is derived at
  query time; `aborted` is persisted via `annotations` so a killed process
  is distinguishable from a stopped cell. Publication: widen
  `set_annotation`'s kind whitelist with `"post"` (PK `(kind,target)` —
  latest post wins, no history) and `flag="posted"`; `harness.py
  publish-mark --target <cell|run_id> --url …` writes it; coverage renders
  `posted ✓/·`. The driver's resume path skips cells already `done`.
- **R5 — BI, self-preference declared.** Mechanical `passes` (validator)
  is the **primary axis** — `~typesafe/jev-latest` is both the assist
  engine in the jev arm and the scorer, so judge-score deltas are
  self-referential and are labeled as such wherever shown. Observatory
  experiment view (extend `compare_payload`, which already computes
  group deltas): per-arm pass-rate + Wilson CI, **difference CI**
  (Newcombe score method on the two-proportion difference — unpaired is
  correct since seeds don't reach chat providers), cost/pass per arm,
  per-arm error rates (the jev arm has more call surface — asymmetric
  exclusion can hide regressions), and jev intervention rates
  (`jev_replan`/`jev_rework` counted by walking `events.jsonl` — these
  are not in the index). Cell-level verdict:
  `lift|harm|no-diff|inconclusive` from the difference CI — not CI
  overlap. Static export for posting (`reports/`-style card; publish
  `runs-pub/`-scrubbed artifacts only, never raw `reports/` HTML).
- **R6 — isolated execution.** Code-bearing task types run under
  `ORCHESTRAL_CODE_RUNTIME=isolated` (codeexec already fails closed
  without it — no bypass). The driver refuses code-type cells unless the
  cube env contract is present (`E2B_DOMAIN`+`E2B_API_KEY`+`SSL_CERT_FILE`
  or `harness.py doctor` pass), so a misconfigured cell can't silently
  degrade to "no runtime" verdicts. Coverage records
  `execution.runtime` per run so runtime failure and model failure stay
  separable.
- **R7 — cheap judge stays constant.** `~typesafe/jev-latest` judges all
  cells in both arms (decisions endpoint ~$0.00001/decide, artifact-hash
  cached). The judge must be identical across arms; `--judge` overrides
  are out of scope for the experiment. **Holdout tasks are excluded from
  the matrix** — the judge ships `task.prompt[:4000]` to a third-party
  endpoint and would contaminate the holdout. Dry-run rows
  (`dry_run=1`) are excluded from every cost/statistical query.

## Design

### Cost-scaled reps (worked numbers, verified)

`cell_budget = --budget / n_cells` (cells, not cell×arm). At
`--budget 12.00`, 40 cells → $0.30/cell → a $0.003/pair cell targets 100
reps (the user's example); at `--budget 2.00` → $0.05/cell → 17 reps.
FLOOR=5 exists for evidence, not cost — a cell whose pair costs more than
its budget share still runs 5 pairs, which is why `--budget` alone is not
a spend bound (R3's cumulative abort is).

### Stopping standard — difference CI, not per-arm CI

Cell done when `HW(difference CI) ≤ diff-eps` **or** reps ≥ target.
Difference CI at equal n/arm: mid-rate cells need ~15–25 reps/arm to hit
ε=0.15; extreme deltas (p≈1.0 vs p≈0.5) resolve inside ~2 batches;
genuinely contested cells correctly burn their cap. (Reference: per-arm
Wilson HW at n=5, p=1.0 is ≈0.217 — per-arm stopping at ε=0.10 would need
n≈16 even for perfect cells, which is why the difference CI is the right
quantity.) Where the cell has no jev-arm data yet, coverage shows
`pending`.

### Execution shape

`harness.py experiment --matrix experiments/jev-ab.yaml --budget 12.00
--daily-cap 15.00 --seed 7 [--batch-size 5] [--jobs 2]`

- Matrix YAML: `orchestrators`, `workers`, `tasks` lists (mirrors
  grid/batch vocabulary); `experiments/` is committable spec-only.
- Cell naming: `run_group = "{matrix}:{task}:{orch}:{worker}:{arm}"` —
  `run_group` and `replicate` are indexed; `~` slugs sanitize for display
  only (groups are a TEXT column, not paths).
- Per replicate index i (1-based, matching `_rep_kwargs` convention):
  two sequential runs, arm order alternating (i odd → baseline first).
- `--jobs` parallelizes **cells**, not reps — keeps pair interleave and
  spend accounting serial within a cell. `--jobs` is added to the
  experiment parser only (it is per-subcommand, not in `_add_run_flags`).
- The driver composes `Runner(**_rep_kwargs(args, i))` per launch —
  never `--replicates` (that flag fires all N contiguously, no gates) —
  and bypasses `_run_preamble`'s replicate-flag validation by setting
  args fields directly. `--dry-run` prints the priced plan table and
  touches nothing (no Runner launch — dry-run index rows would pollute
  coverage).
- Between batches the driver also checks the error-rate and difference-CI
  gates; on `--budget`/cap exhaustion it returns with a summary, not
  `sys.exit`.

### Coverage + publication

`orchestral/coverage.py`: matrix join + state machine + annotation join.
`storage.py`: widen `set_annotation` kind tuple to include `"post"` (and
`"cell-state"` for persisted aborts) — schema-compatible, PK already
`(kind,target)`. Coverage reads `runs.config` JSON for `jev_assist`/
`seed`; the name `coverage` does collide conceptually with existing
spec-coverage — acceptable, documented.

### BI surface

`orchestral/web/state.py::experiment_payload(store, matrix_path)` —
reuses `compare_payload` internals; adds difference CI and intervention
counts (per-run `events.jsonl` walk, acceptable at this scale).
`harness.py dashboard`/`report --compare` get the same table. One chart:
per-arm pass-rate bars with CI whiskers, honestly labeled incl. the
jev-self-judge caveat.

## Non-goals

- No scheduler/daemon — cadence stays "on demand + tonight's batch run".
- No Runner/judge/cubeexec internals changes; no seed plumbing into
  `Provider.chat`.
- No new task specs; matrix reuses committed v2-*/code specs, holdouts
  excluded.
- Not extending jev-assist to media/executor lanes (documented gap).

## Implementation units

### U1 — stats + driver

- `orchestral/stats.py`: lift `web/state.py::_wilson` (lines ~884–893)
  here as `wilson_interval(passes, n)`, add `diff_ci(p1,n1,p2,n2)`
  (Newcombe). `state.py` imports from stats — no duplicate implementation.
- `orchestral/experiment.py`: matrix load, `mean_cell_cost` pricing,
  rep-target clamp, batch loop with R3 gates (live `calls`-spend check,
  error abort, difference-CI stop), alternating arm order, resume-skip
  `done` cells, persisted `aborted` state.
- `storage.py::mean_cell_cost(task, orch, worker)` — AVG over
  `runs.total_cost_usd` filtered `dry_run=0`.
- `harness.py`: `experiment` subparser (own `--jobs`, `--budget`,
  `--batch-size`, `--diff-eps`, `--matrix`) → `cmd_experiment`.
- `experiments/jev-ab.yaml` + `experiments/README.md` (specs only, no
  run output lands here).

Tests — `tests/test_experiment.py`:
- clamp math at floor/cap/in-range; est=0 → CAP; hotter-than-history
  shrinks target; no-history → calibration pair then re-price
- pair pricing is per-replicate (2 runs), jev arm priced with load factor
- batch gates: live `calls` spend abort; >50% infra-error batch →
  `aborted` persisted; difference-CI early-stop fires and skips remaining
  reps; arms interleave correctly; resume skips `done` cells
- code-type cell without cube env → refused, no spend
- `--dry-run` → plan table only, zero index writes

### U2 — coverage + publication

- `storage.py`: widen `set_annotation` whitelist (`post`, `cell-state`,
  `flag="posted"`).
- `orchestral/coverage.py`: matrix-vs-store join, per-arm aggregation,
  state machine, published join.
- `harness.py`: `coverage [--matrix F] [--json]`, `publish-mark --target
  X --url U [--note N]`.

Tests — `tests/test_coverage.py`: pending/partial/done/aborted states;
aborted persisted vs crash-partial distinguished; publish-mark → posted
flag; arm split read from `config.jev_assist`; `dry_run=1` rows excluded;
empty matrix → honest empty output.

### U3 — observatory + reports

- `web/state.py::experiment_payload` reusing `compare_payload`; SPA
  section on overview; `dashboard`/`report --compare` arm table.

Tests — `tests/test_web_experiment.py`: payload shape, difference-CI
fields, intervention counts from fixtures, self-judge label present.

### U4 — docs + runbook

- `README.md` experiment section; `docs/model-config.md` "judge constant
  across arms" note; `experiments/README.md` matrix reference.
- `docs/runbooks/tonight-ab.md`: env contract — **assert
  `OPENROUTER_API_KEY` resolves to `omaseal get openrouter orchestral`
  before launch; never source `scripts/wave-a.sh`'s `keys.json` line**
  (it bills the default key, not the eval key) — plus `scripts/cube-env.sh`,
  command line, where to watch (observatory + CubeOps :12088).

## Dependencies and sequencing

- Verify `--jev-assist` + `doctor` are on `main` (PRs #112/#111) before
  the run — both exist in-tree; the gate is the merge, not the code.
- U1 → U2 → U3 → U4. Linear, acyclic.
- Host env proven: cube stack live, `cube-env.sh` contract, e2b 1.11.1.

## Verification

- Throwaway-venv gates (tests+ruff+mypy), `audit --strict`,
  `selfcheck --execute` — unchanged surfaces, must stay green.
- `experiment --dry-run --matrix experiments/jev-ab.yaml` → priced plan
  table, zero index writes.
- Tonight: env assertions per runbook →
  `experiment --matrix experiments/jev-ab.yaml --budget 12.00
  --daily-cap 15.00 --seed 7 --jobs 2` → `coverage --matrix …` shows all
  cells `done|aborted` with reasons → `report --compare` arm table.
- Brake stack, honestly stated: one independent brake (omaseal $50/mo
  key cap) + one index-metered brake now closed on in-flight spend
  (`calls`-table check) + the `--budget` cumulative abort. Worst-case
  blind spend ≈ one batch × `--jobs` × pair cost.

## Risks / honest caveats

- **Self-preference confound is real:** jev scores jev-assisted
  artifacts. Primary axis = mechanical pass; judge deltas labeled
  self-referential everywhere.
- **Seeds don't reach chat providers** — "same seed" is bookkeeping;
  pairing rests on spec + interleave, so the difference CI is unpaired.
- **Multiplicity:** ~40–80 cells at 95% → expect ~2–4 intervals to miss.
  The export card states this.
- **Serial worst case:** 100 reps ≈ 200 runs/cell ≈ 40+ min at ~12s/run;
  early-stop + `--jobs` bound it, budget bounds it harder.
- **Self-executing orchestrators** produce no delegation → output gate
  inert → lift concentrates in plan gate; coverage surfaces
  `delegated: false` per arm so this reads as data, not a bug.
- **Judge-cache dedup:** byte-identical artifacts across reps share one
  judge call — correct for spend, noted for variance reads.
- **Cubelet flood:** non-issue — `--jobs` bounds concurrent microVMs
  (2 vs ~500 warm TAPs).
- **`keys.json` key mixup** (wave-a.sh precedent) — runbook asserts key
  provenance before launch.
- **Persisted `aborted` vs crash-partial** distinguishes kill-mid-batch
  from honest stops.
