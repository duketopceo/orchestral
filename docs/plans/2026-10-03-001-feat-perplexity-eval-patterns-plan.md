---
title: Perplexity eval patterns — runner hardening, decomposed judge contract, Harbor task export
created: 2026-10-03
deepened: 2026-10-03
status: draft
plan_kind: feature
origin: ad-hoc research (deep-read of perplexityai/search_evals, perplexityai/wandr, pplx-garden/lily on 2026-10-03)
---

# Adopt proven eval patterns from Perplexity's open source

## Problem frame

Three weaknesses in orchestral, each with a proven public fix:

1. **Runs are all-or-nothing — and the ledger lies about it.**
   `Runner.run()` (`runner.py:271`) is one atomic pipeline (plan →
   delegate → assemble → validate → judge → accounting). A crash re-pays
   the whole run on retry. Worse, the experiment ledger treats a crashed
   run's `status="running"` row as *done*: `_missing_work`
   (`experiment.py:249–259`) fills a replicate+arm slot on any row with
   `replicate` set, no status filter — orphaned reps are never rerun
   *and* never counted missing. A cell of orphans yields
   `cell_state=pending` + empty `_missing_work` → the experiment loop
   launches nothing and spins until spend gates fire (infinite at
   `budget=0`). The 8 orphaned rows from the driver's 03:33 death are
   silently under-delivering reps today — a live bug. Orphaned rows also
   keep `total_cost_usd=0` forever, undercounting `coverage_rows`,
   `spend_today`, and `mean_cell_cost`.
2. **The judge verdict is opaque.** `judge.py` returns one
   `{score, passed, reasoning}` blob per artifact. When a run fails you
   can't tell which requirement failed or whether the judge saw evidence —
   the writeup needs per-criterion provenance, not a scalar.
3. **No runnable public task format.** Specs in `tasks/*.yaml` are
   already committed and citable, but only inside orchestral — they
   aren't portable to other harnesses. A self-contained standard package
   (instruction + environment + verifier) makes benchmark evidence
   runnable and comparable outside this repo.

`perplexityai/search_evals` and `perplexityai/wandr` solve all three in
production. This plan adopts their patterns, phased by dependency-free
value.

`pplx-garden/lily` was evaluated and rejected: requires Apple GPU family
10+ (M5) and macOS 26 — cannot run on this machine (M1 Max, Asahi Linux).
Only its measurement-contract doc style is borrowed (deterministic
synthetic inputs, paired fresh processes, digest-match proof that both
arms ran identical workloads) for the writeup's methodology section.

## Key references (external)

- `/tmp/search_evals` — `runner.py` (attempt dirs, manifest guard,
  `hydrate_costs`), `suites/graders.py` (strict json_schema judge),
  `costs.py` (Decimal pricing, `missing_cost_count`)
- `/tmp/wandr` — `adapters/wandr/src/wandr/` (satisfied/supported
  criterion schema, per-stage replay caches under `debug/`),
  `datasets/wandr/<task>/task.toml` (Harbor package format, difficulty
  labels), `agents/relay/` (Harbor bridge). NOTE: wandr's task corpus
  declares `network_mode = "public"` plus Perplexity API keys — it is
  NOT importable under an offline isolation contract (see C2 deferral).

## Stage map (verified against runner.py)

| Stage | Boundaries | Existing durable artifact |
|---|---|---|
| init | providers → `store.new_run` (313) → EventLogger (322) → `write_manifest` (344) | `manifest.json` |
| plan | `plan_ce`/`plan_raw` (417–444); jev plan_gate replan (449–483) | `plan.json` (438) |
| delegate | per-subtask loop (565–862); jev output_gate rework (867–951) | `worker-{i}.json` (854), `worker-{i}.{ext}`, `worker-{i}-rework.json`, `worker-self.json` (535) |
| assemble+validate | fused per type (953–1100) | `artifact.*`, `raw/` evidence |
| judge | 1108–1173 | `report["judge"]` inside `report.json` (1182) |
| accounting | 1207–1253 (`_flush_ledger` → `cost.json`, `update_meta`, `finalize_manifest`, `metrics.json`) | `cost.json`, `metrics.json`, terminal `runs.status` |

Two gaps in what files alone can reconstruct:

- **`file_sets`/`results`/`agent_diffs` are in-memory only.**
  `worker-{i}.json` stores `out` = `{subtask_id, notes,
  summarize_fileset(files)}` — paths/sizes/hashes, no contents
  (fileset.py:304–310, deliberately content-free). The `files` dict
  lives in memory until `merge_filesets` → `artifact.zip`
  (runner.py:1035–1043). A crash mid-delegate on multi/code/bugfix
  leaves zero durable file contents — skipping "done" subtasks on
  resume would rebuild an empty or different artifact.
- **`report.json` precedes the accounting stage.** A crash between
  1182 and 1207 leaves a finished-looking report under
  `status="running"` with `total_cost_usd=0` — "done" detection must
  key on terminal state, not report presence.

Stage writes are bare `write_text` (non-atomic) — resume must treat
unparseable stage files as "replay this stage," not trust presence.

## Ordering invariants (verified)

- `EventLogger.log` → `_index_call` → `store.record_call` commits
  synchronously per call (logger.py:69–114, _index_call 145–177; storage.py:362–397).
- **`_index_call` swallows all store failures** into `debug.jsonl`
  (logger.py:175–177), and `events.jsonl` flushes before the sqlite
  insert — a billed call can lack a ledger row. `backfill_calls`
  (storage.py:523–591) exists precisely because **events.jsonl is
  authoritative** — resume rehydration replays events, never trusts
  `calls` alone.
- Stage-marker trust = **verified ledger row**, not write order.
  `calls` has no `event_id`; sequence numbers are the join key.

## Resume hazards (verified)

- **No run-dir lock** — and the lease must be *owner-side*: every
  `run()` acquires `run_dir/lock` (flock) at init and heartbeats
  during long calls, or "resume racing a live original" is never
  detected. flock liveness is the live-owner signal; last-event-mtime
  is only the fallback for pre-lease runs — a single call >10 min
  produces no event writes and would falsely read as stale.
- **`EventLogger._seq` restarts at 0** (logger.py:67) — every resume
  would duplicate `sequence` values (keys live ordering in
  tui/screens.py:300, dataset.py:133, D1 via cf.py:69). Seed `_seq`
  from the event tail.
- **`calls.attempt` is taken** — per-call retry (logger.py:165). The
  run-level counter is `run_attempt`: written into each `llm_call`
  event's `metadata` on resumed attempts, propagated to `calls`
  through `record_call` and `backfill_calls` (its INSERT list is
  hardcoded, storage.py:576–583), and added to `cf.py` `CALL_COLUMNS`
  if the mirror should distinguish attempts.
- **Rework precedence**: jev output_gate writes
  `worker-{w}-rework.json` and replaces `results[w]` in memory while
  `worker-{w}.json` keeps the pre-rework output (runner.py:930–931).
  Rebuild must prefer `-rework` per index; `worker-self.json` (self-
  executed plans, runner.py:535) has a different shape.
- **Resume must not re-run init side-effects**: no `store.new_run`,
  no `build_manifest`/`write_manifest` — read-only revalidation +
  `finalize_manifest` only.
- **`dataset.py` mislabels across attempts**: one record per `calls`
  row joined to the final run outcome. Export must exclude
  prior-`run_attempt` calls **only within replayed (phase, step)
  ranges** — calls from kept stages genuinely produced the artifact
  and must stay.
- **`meta.latency_ms` is process-t0-relative** (runner.py:391);
  accumulate prior-attempt latency in `resume_state.json`.
- **`record_call` never marks `sync_dirty`** — a resumed run that
  re-crashes strands newly billed calls from the D1 ledger (terminal
  `update_meta` marks dirty, but only if reached).
- **Append-only bias**: never delete or overwrite `events.jsonl`/
  `calls` — replay is additive only. In-place replay *does* overwrite
  per-stage files (worker-*.json) — acceptable because per-call
  input/output in the ledger is the evidence of record; if attempt-
  level file provenance is wanted later, search_evals' `attempts/
  NNNNNN/` layout is the fallback.

## Phase A — Runner hardening (search_evals patterns)

### A0. Fix `_missing_work` orphan accounting (pre-existing bug, ships alone)

Status-aware slot fill: only `finished`/`failed`/`cancelled` rows fill
a replicate+arm slot; stale `status="running"` rows return as
*recoverable* run_ids (staleness = flock liveness where a lease exists,
last-event mtime > 10 min otherwise). `arm_stats` still counts orphans
as `infra_errors` but surfaces the per-status breakdown (visibility
only — A4 owns the rate columns). Open question below: whether
graceful `status="failed"` rows should also be resume-eligible —
initial answer: no, failed is terminal by policy, orphans are
process-death evidence, not judged outcomes.

Fixes the silent rep loss, the no-budget infinite loop, AND enables
fresh-relaunch recovery: with status-aware slots, a *new* run filling
a recoverable slot counts exactly once — no double-count.

### A1. Recovery v1: relaunch recoverable runs (small)

`CellLauncher` (`experiment.py:58`) learns to launch a fresh run for a
recoverable slot instead of skipping it. `harness.py run --resume
<run_id>` marks the orphan with `flag="aborted"` and a
`"superseded by <new_id>"` note — the annotation flag enum is closed
(`{interesting, not, posted, aborted}` per storage.py:688–691); reuse
`aborted` rather than extend the vocabulary — and launches a new run
with the same replicate/arm/seed. Recoverable-slot bookkeeping must
exclude already-superseded orphans so they don't resurface across
processes. This delivers rep-completion + honest spend for ~none of
A2's cost.

### A2. Stage-resume under one run_id (deferred-gated)

Full in-place resume — gated on measured re-spend justifying it (see
KTD "stage-resume vs. relaunch"). Design held for implementation:

- Acquire lease (flock + heartbeat); refuse on live owner.
- Manifest revalidation (strict): `task_hash` + strict `config_hash`
  + `*_prompt_hash` + pipeline-behavior hash all match → hard-fail
  (`--force` overrides). Pipeline-behavior hash = sha over the
  runner/planner/judge/storage module set or a manually bumped
  `PIPELINE_VERSION` — raw `git_commit` is rejected: it fires on
  docs/test commits and would stall CellLauncher auto-resume
  mid-campaign (ritual `--force` erodes the guard).
- Stage position from durable files: `plan.json` parseable → skip
  plan; per-index `worker-{i}(-rework)?.json` → skip subtask
  (rework preferred); **`artifact.*` + `report.json` + terminal state
  (`runs.status` terminal OR `cost.json` + `run.completed` event)
  → done**. Anything else replays the accounting tail — flush ledger
  + `update_meta` + `finalize_manifest` are idempotent.
- File-producing types (multi/code/bugfix) need `file_sets` persisted
  per-subtask — `worker-{i}-files.json` (tmp+replace) written after
  each worker; contents are model-produced, same publish class as
  `artifact.zip` (the `worker-` prefix is already allowlisted, so
  they ship in the scrubbed mirror — intended, not a footgun).
  Executor path already has durable `raw/worker-{i}-attempt-{n}`
  evidence dirs.
- `resume_state.json` (tmp+replace, `schema_version`): `run_attempt`,
  accumulated latency, stage markers, pointers for in-memory state.
  Added to `_scrub_dir`'s named-omission list alongside the `lock`
  file — non-allowlisted entries currently skip *silently*, violating
  the publish-manifest invariant ("nothing is dropped silently");
  every new run-dir entry gets an explicit omission record.
- Ledger hydration replays `events.jsonl` `llm_call` entries (filter
  `worker_error` types — `calls` rows include them but the live
  ledger never did); `backfill_calls` repairs missing rows first.
  `calls` lacks `usage` — hydrated `cost.json` emits `usage: null`
  uniformly (document, don't fake). Hydrate XOR re-add, never both.
- `sync_dirty`: mark the run dirty on resume-path call commits — a
  re-crashed resume must not strand billed calls from D1.
- `run_attempt` in event `metadata` → `calls.run_attempt` →
  `CALL_COLUMNS`; dataset export excludes prior-attempt calls in
  replayed (phase, step) ranges only.
- `run_attempt` count lands in `manifest.json`/`report.json` so
  provenance publishes (mirror sees allowlisted top-level only).

### A3. Ledger hydration + orphan-cost repair

- Repair recompute: `runs.total_cost_usd` from `calls` rows for stale
  `status="running"` rows regardless of resume — killed runs
  currently report $0 forever to `coverage_rows`, `spend_today`,
  `mean_cell_cost`.
- `missing_cost_count` in `report.json` + experiment summary — keyed
  on `api_cost_usd IS NULL` against the full pricing vocabulary:
  `flat_estimate` counts unconditionally (planners.py:1595 — an
  estimate by definition); `cli_reported` counts when no provider
  `usage.usd` was reported (planners.py:1587–1593); `api` on
  decisions-engine calls counts when `usage.cost` was absent
  (judge.py:162, jevassist.py:133); plus `configured_estimate`/
  `unmetered`/`none` (costs.py:124–131; `unmetered_workers`,
  storage.py:617–629). Per-source semantics stated explicitly —
  the jev/decisions and executor arms are the experiment's measured
  cells, so their unknown cost must not silently read as $0.

### A4. Dual failed scoring (completes A0's status split)

Orphan crashes currently distort `arm_stats` denominators
(invisible inside `infra_errors`) — A4 finishes the status-aware
accounting A0 starts. Split per-status counts; report both
`failed_excluded` and `failed_as_zero` rates in `CoverageRow`/
`to_dict`, `coverage_summary`, per-cell experiment summaries.
`failed_as_zero` stays display-only — mixing failure modes into
`diff_ci` conflates task failure with infra failure.

## Phase B — Decomposed judge contract (wandr pattern)

### B1. Per-criterion verdicts with provenance

wandr's `JudgmentResult` checks every requirement twice: `satisfied`
(did the artifact meet it) and `supported` (do the submitted excerpts
convey the satisfaction to a reader). Map to orchestral:

- Criteria live under **`metadata.criteria`** — NOT a new `TaskSpec`
  field (a dataclass field changes `asdict(task)` for every spec →
  invalidates every stored `task_hash`; `holdout.materialize`'s fixed
  top-level key set would also silently drop it — it already passes
  `metadata` wholesale, holdout.py:611, so `metadata.criteria` flows
  through materialized specs with zero code change).
- When `criteria:` is absent, derive from `validation:` entries +
  spec anchors — every existing spec stays judgeable. Derived-
  criterion quality varies (see Open Questions).
- Chat judge: structured output returning per criterion
  `{satisfied: bool, supported: bool, evidence: str}` + overall
  score/passed. `supported` = judge cites the artifact content
  proving `satisfied`; an unsupported verdict is flagged, not
  trusted. Implementation: prompt-engineered JSON through the
  existing `_extract_json` parse path — `Provider.chat` has no
  `response_format` kwarg today; extending it is a named option,
  not assumed (search_evals uses strict json_schema, but the
  prompt-level path keeps the protocol surface unchanged).
- **Secret-bearing criteria are graded mechanically, never by LLM** —
  and the trigger is *value membership*, not key names: a criterion
  is secret-bearing if its text intersects `spec_secrets(spec)` =
  `holdout_secrets(spec)` ∪ values under `GRADED_KEYS` ∪
  `GRADED_METADATA_KEYS` ∪ per-type answer keys (`required`,
  `forbidden`, `calls`, `reference_sql`, `expected_answer` — note
  `required`/`forbidden`/`document`/`calls` are NOT in `GRADED_KEYS`,
  privacy.py:150–171). This runs on *derived* criterion text too —
  `validation:` entries can embed expected values verbatim. The
  primary rationale is verdict validity — a judge fed the answer
  rubber-stamps every criterion — publication safety is second:
  `withhold_graded_keys` strips dict *keys*, never substrings inside
  an `evidence` string, so secret values quoted back into evidence
  publish intact.
- Decisions judge (`_judge_via_decisions`): per-criterion `noul`
  questions yield `satisfied` probabilities; `evidence` and
  `supported` are **`null`** — the noul engine cannot cite artifact
  text, and deriving `supported` from self-confidence would fabricate
  a provenance axis (wandr's semantic is evidence-sufficiency, not
  engine certainty). The `engine` field already distinguishes the
  shapes.
- `report["judge"].criteria[]` alongside unchanged headline
  `score`/`passed` — all downstream readers verified additive-safe
  (calibrate.py:136–154, state.py:619–628/755, dataset.py:37–69).
- **Judge cache**: bump `JUDGE_CACHE_SCHEMA` 2→3 (storage.py:34–37
  prescribes this for contract changes) and include
  `canonical(criteria)` in the cache key — a criteria-only spec edit
  must not serve a stale verdict. Result carries
  `judge_contract: "v2"`; backfilled judgments stay `v1`-labeled.

### B2. Rollup + surface

- Hard verdict per-engine: chat requires every required criterion
  `satisfied` AND `supported`; decisions requires `satisfied` above
  calibrated threshold. Soft score = fraction satisfied.
- `judge.criteria` flows to the hosted mirror verbatim via
  `run_detail_payload` (state.py:755) — plumbing verified free.
  Named display deliverable: per-criterion table in the SPA run-
  detail view (ui/app.js renders scalar judge fields only today) —
  small, but it is what makes B1's provenance actually readable.
- `calibrate.py` gains per-criterion agreement stats.
- **Explicit deliverable**: the 10-run old-vs-new calibration
  comparison named in Risks is a B2 gate task — run it and record the
  outcome before the contract default flips; not an implied step.

## Phase C — Harbor task export (import deferred)

### C1. Export: `harness.py harbor export <task_id>`

`orchestral/harbor_export.py` → `task.toml` + `instruction.md` +
`environment/Dockerfile` + `tests/test.sh` (+ oracle files):

- **Export publishes the answer key — by design.** A runnable Harbor
  package is self-contained: `tests/test.sh` + oracle files
  necessarily embed expected outputs (`reference_sql`,
  `expected_answer`, `required_content`, oracle `test_files`). That
  is the format's contract, matching wandr's own packages. Therefore:
  - Hard-refuse `holdout.is_holdout(spec)` (holdout.py:554–556; NOT
    `privacy.run_is_holdout` — it reads run manifests and always
    returns False on a spec dir). Also refuse when `--tasks-dir`
    resolves inside a holdout-materialized dir — `find_task` rglobs
    whatever it's given.
  - Require an explicit ack flag (`--publish-keys`) naming that the
    spec's expected answers become public — no accidental key
    publication.
  - Defense-in-depth assertion: no string from
    `holdout_secrets(spec)` in any emitted file's final bytes
    (vacuous for non-holdout by construction — it catches guard-
    bypass bugs, not normal input). Emitted text passes
    `privacy.scrub_dict` (host paths in fixture/verify fields).
- Serialize from a **field allowlist**, never `spec.metadata`
  wholesale (it can carry `calls`, seeded haystacks, internal notes).
- `tests/test.sh` ← generated driver running `verify.command` inside
  the env → reward JSON. The v3 spec contract (`metadata.fixture`,
  `workdir: repo`, `setup_commands`, `verify.command`, `test_files`
  oracle, `timeout_seconds`) is spec-local;
  `audit.check_fixture_contract` already enforces the constraints
  export needs. Verdict = exit code.
- `environment/Dockerfile` ← fixture tarball + wheelhouse from
  `fixtures/registry.yaml` for code/bugfix/terminal types.
  Judge-evaluated types (html/image/video) export with mechanical
  checks only — LLM judges don't fit Harbor's verifier model;
  document the boundary.
- `task.toml` ← id/title/metadata + difficulty labels (wandr style).
- Packages emit under `dist/harbor/` (gitignored or publish-tracked —
  decision below); a package is "citable" by living in the repo or a
  published bundle.

### C2. Import — deferred until a concrete offline corpus is named

wandr's corpus — the motivating example — is `network_mode="public"`
throughout and needs `PERPLEXITY_API_KEY`/`WANDR_FETCH_*`; no concrete
offline Harbor corpus has been identified. Building a foreign-verifier
execution path for a hypothetical corpus is speculative machinery.
When a real offline corpus exists (e.g. terminal-bench-style
packages), the design constraints are already settled: sandboxed
`shlex.join` verifier path, `allow_internet_access: False`, env
allowlist, Dockerfile rejected-or-allowlist-mapped (never auto-built
— foreign code at build privilege), launch opt-in flag (same shape as
`--allow-agent-exec`, never a web form), `timeout_seconds` clamped at
import (foreign `task.toml` values are untrusted — a declared 86400s
pins a metered sandbox for a day), bounded+scrubbed verifier output
(`report.execution` carries foreign-controlled text into the
published record — note the instruction.md → worker-prompt direction
is equally foreign-controlled and belongs to the opt-in boundary).

### Deferred (named, not implied)

- C2 import (above — no named corpus).
- Publishing packages to the upstream Harbor registry.
- wandr's canon/dedup streaming pipeline (research-corpus value only).
- `lily` runtime — hardware/OS incompatible, permanently deferred.

## High-level design sketch

```
recovery (A1 relaunch path):
  _missing_work: slot free OR run.status in {finished,failed,cancelled}
                 stale "running" -> recoverable run_id
  CellLauncher(recoverable slot) -> fresh run (same arm/replicate/seed)
  orphan row -> annotated "cancelled/superseded by <new_id>"

stage-resume (A2, deferred-gated):
  every run: flock(run_dir/lock) at init, heartbeat during long calls
  --resume:  live owner -> refuse
             manifest: task_hash + strict config_hash
                       + *_prompt_hash + PIPELINE_VERSION
                       -> drift = hard-fail (--force overrides)
             backfill_calls            # events.jsonl authoritative
             ledger += replay(llm_call events, run_attempt filter)
             _seq = tail(events).sequence
             stage = first unparseable/absent stage file
                     (plan.json -> worker-{i}(-rework)?.json
                      -> artifact/report+terminal -> done)
             resume_state.json {run_attempt++, latency, markers}
             -> re-enter run() at stage; terminal path unchanged

judge:
  criteria = spec.metadata.criteria or derive(validation + anchors)
  for each criterion: secret-bearing(value-membership vs spec_secrets)
                      -> mechanical grade only
  chat path:      per-criterion {satisfied, supported, evidence}
  decisions path: per-criterion noul -> satisfied prob;
                  evidence=null, supported=null
  cache_key = sha256(prompt + artifact + canonical(criteria))
  report["judge"] += {criteria[], judge_contract:"v2"}
```

## Key technical decisions

- **Relaunch-first recovery, stage-resume deferred-gated** (A1/A2).
  Honest accounting: with A0's status-aware slots, a fresh run fills
  a recoverable slot exactly once — "double-counting" was never the
  real cost of relaunch. The real tradeoff is re-spend on completed
  stages vs. machinery: same-run_id stage-resume needs lease, `_seq`
  seeding, `run_attempt` plumbing, dataset-export filtering,
  per-subtask file-set persistence, and destroys attempt-1 stage
  files in place — for a harness whose measured spend is ~$2.44
  across 163 runs. Sequenced: A0+A1+A3 ship rep-completion and honest
  spend now; A2 lands when a measured cell shows meaningful re-spend
  (video/executor/multi-subtask types) or when the writeup needs
  attempt-level provenance. If A2 ships, prefer attempt-dir
  preservation over in-place overwrite for evidence-keeping.
- **`metadata.criteria`, not a TaskSpec field** (B1): protects every
  stored `task_hash`; survives `holdout.materialize` (fixed top-level
  keys + wholesale metadata passthrough).
- **`supported=null` on the decisions engine** (B1): wandr's semantic
  is "excerpts alone convey satisfaction" — a calibrated probability
  is not evidence sufficiency; null is more truthful than a derived
  proxy. B2's hard verdict is per-engine.
- **Cohort hash over a curated behavior view** (A2-scope):
  `manifest.config_hash` already exists over full `run_config`
  (includes `replicate`/`run_group`/`seed`/`jev_assist` — volatile
  within a cohort). `cohort_hash` = sha256(task_hash + behavior view
  of model configs + planner + prompt_variant), where the behavior
  view excludes pricing fields (`input/output_price_per_mtok` etc.) —
  a mid-campaign rate-card correction is accounting drift, not cohort
  drift. Arm-scoped comparison warns; `--strict` refuses.
- **Export = key publication, gated by ack flag** (C1): the format
  requires embedded oracles; the honest boundary is refusal for
  holdout + explicit opt-in for everything else, not secret-stripping
  that would break the verifier.

## Risks

- **A2 has no safe halfway state** — lease without hydration, or
  hydration without `run_attempt`+dataset filtering, is subtler
  corruption than no resume. It lands whole or not at all (another
  argument for the deferral gate).
- **File-producing-type `file_sets` durability** (A2): the new
  `worker-{i}-files.json` is the load-bearing fix; without it the
  skip rule silently produces different artifacts on the exact task
  family Phase C exports.
- **Judge contract changes score distributions** (B1): gated by the
  named B2 deliverable (10-run old-vs-new calibration comparison)
  before the default flips; `judge_contract:"v2"` labels mixed
  cohorts; `JUDGE_CACHE_SCHEMA` bump prevents stale-verdict
  poisoning.
- **New model-controlled text channels** (B1/C2-deferred): judge
  `evidence` and (if import ever lands) foreign verifier output +
  foreign `instruction.md` all feed published `report.json` — bounded
  + scrubbed, but the injection surface widens; criteria carrying
  adversarial spec text is a live consideration.
- **New run-dir entries publish-or-skip by name**: `lock`,
  `resume_state.json` get explicit `_scrub_dir` omission records
  (non-matching entries currently skip silently — violates the
  publish-manifest invariant); `worker-{i}-files.json` matches the
  `worker-` allowlist deliberately.
- **`run_is_holdout` fails open** on corrupt manifests (existing
  caveat): stage files must never precede `write_manifest` —
  verified, manifest writes at init (runner.py:344).
- **Rate-card churn**: `cohort_hash`'s pricing-field exclusion is the
  specific guard; a naive `to_dict()` hash false-positives on every
  cell whenever pricing YAMLs update.

## Verification

- A0: fixture cell whose only rows are `status="running"` orphans →
  slot returns as recoverable; experiment loop cannot spin empty;
  `infra_errors` breakdown visible.
- A1: relaunched run fills the recoverable slot exactly once
  (`cell_state` counts one outcome); orphan annotated superseded.
- A2 (when gated in): SIGKILL mid-delegate → `--resume` completes
  with zero new calls for finished stages; corrupt `worker-1.json`
  replays only that subtask; crash post-`report.json`/pre-`cost.json`
  replays the accounting tail (not "done"); rework precedence honored
  (`-rework` wins); multi-file type resumes with intact `file_sets`;
  manifest drift hard-fails (PIPELINE_VERSION mismatch; pricing-only
  model edit does NOT fail cohort compare); double `--resume`
  refuses on lease; `sequence` monotonic; resumed calls mark
  `sync_dirty` (assert row present after re-crash); dataset export
  excludes superseded-attempt calls in replayed ranges only.
- A3: kill fixture leaves `total_cost_usd=0` → repair recomputes;
  unpriced-model run emits `missing_cost_count`.
- A4: mixed finished/failed fixture → both rates, values correct.
- B1: 3-criterion fixture → per-criterion satisfied/supported/
  evidence; unsupported flagged; decisions path returns null
  supported/evidence; criteria-only spec edit misses judge cache;
  a criterion derived from a spec's `required`/`reference_sql` value
  is graded mechanically — assert the judge input never contains the
  secret value (canary pattern).
- B2: SPA run-detail shows per-criterion table; the 10-run comparison
  is executed and recorded before default swap.
- C1: code task exports → package lint-clean; `is_holdout` spec
  refuses; `--tasks-dir` inside holdout dir refuses; export without
  `--publish-keys` refuses; emitted bytes contain no
  `holdout_secrets` strings.
- Gates: unittest + ruff + mypy per AGENTS.md; spec-schema or grading
  changes also `audit --strict` + `selfcheck --execute`; any new CI
  gate updates `.github/required-checks.json` +
  `test_required_check_contract.py` together.

## Open Questions

- Should graceful `status="failed"` runs be resume-eligible (attempt
  N+1 on the same slot), or is failed deliberately terminal while
  only orphans recover? Working answer: failed is terminal — it is a
  judged outcome, orphans are process death.
- What do derived criteria look like for `validation:[]`/minimal
  specs — meaningful or trivial (non_empty)? If trivial, derived
  criteria may need a floor (skip judging rather than emit noise).
- Does backfill_judgments get the v2 contract, or do backfilled runs
  stay v1? Working answer: label them v1 — provenance honesty.
- Does `run_attempt` belong in `CALL_COLUMNS` (D1)? Working answer:
  yes when A2 lands — attempt provenance should be mirror-visible.
- Exported packages: gitignored `dist/` artifacts, tracked repo
  files, or a separate publish bundle? Decides what "citable" means
  for the writeup.
- When does A2 justify itself — what measured per-stage re-spend
  threshold? Candidate: when median cell re-spend on crash exceeds
  the fixed cost of a full relaunch by a stated margin.

## Effort shape

- A0: small, high-value — fixes a live correctness bug (rep loss +
  infinite-loop vector).
- A1: small — launcher path + annotation.
- A2: largest unit when gated in — no safe halfway state; all hazards
  in one landing.
- A3–A4: small.
- B1: **large** — ~9 mechanisms (criteria plumbing, derivation, chat
  structured output, decisions asymmetry, secret-membership guard,
  mechanical grading split, cache bump, report shape, spec docs).
- B2: small-medium (rollup + SPA table + calibrate + calibration-run
  gate).
- C1: medium — new module + CLI verb, reuses v3 verify machinery.

## Doc-review coverage

Five personas dispatched: coherence, feasibility (timed out — partial
coverage only), security-lens, adversarial, scope-guardian. Accepted
findings integrated above; notable: accounting-window "done" gap,
undurable `file_sets`, vacuous export canary → ack-flag boundary,
secret-membership guard replacing key-name lists, `supported=null`
for decisions, owner-side lease acquisition, rework precedence,
pricing-field exclusion in `cohort_hash`, C2 deferral, `sync_dirty`
ownership, `_scrub_dir` named omissions.
