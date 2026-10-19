---
status: plan
date: 2026-10-17
type: feat
---

# Feat: wide-decomposition task family (WANDR shape, mechanical grading)

## Diagnosis (measured, not suspected)

The argon-v3 benchmark (9 v3 tasks × 3 reps × 2 orchestrators, same worker)
produced a **dead heat at the ceiling**: 12/12 vs 11/11 at score 1.0 on every
cell that executed. The suite cannot discriminate orchestrators because:

- **Single-file subtasks need no real orchestration.** "Write one module" /
  "fix one file" is a 1-subtask plan; decomposition skill — the thing a
  frontier orchestrator is for — is never exercised.
- **The worker is the bottleneck, not the planner.** The artifact quality
  belongs to glm-5.3-flash either way; plan quality only changes the
  artifact when the plan is wrong, and trivial plans aren't wrong.
- The boltons arm additionally showed a structural hole (worker asked to
  modify files it never saw — fixed by PR #193), meaning part of the suite
  measured *file hallucination*, not editing.

Perplexity's WANDR benchmark (Wide ANd Deep Research: 500 tasks, dozens to
thousands of independently verifiable records each) is where a frontier
reasoning orchestrator shows its edge — wide fan-out, non-overlapping
coverage, per-member verification. Argon set a record there. That shape is
what we need; **not** WANDR's grading (LLM-verified citations imports judge
noise into a harness whose strength is mechanical truth).

## The tweak

Keep WANDR's task shape — many independently verifiable members per task —
inside our mechanical grading: a **fixture code task that requires N member
units**, each graded by hidden pytest in the sandboxed repo checkout.

- **Soft F1 analog** = `score` (fraction of tests passed) — already recorded.
- **Hard F1 analog** = `passes` (all checks green) — already recorded.
- Per-member granularity falls out of `fail_to_pass` naming convention
  (`test_member_<name>*`) — per-member pass/fail is recoverable from the
  suite output without new machinery.

The discriminating pressure moves to where it belongs: does the plan fan
out to cover all N members without merging or missing any? Does the plan's
file targeting land? Do members stay consistent (shared types, imports)
across independent worker calls?

## Non-goals

- No new task `type`. `code` + fixture already covers the grading contract;
  a new type forks `_REQUIRED_META`, `GRADING_CONTRACT`, artifact handling.
- No LLM-verified-member grading. Members pass via pytest or not.
- No WANDR corpus import. We author fixture tasks in our own domain
  (code), because the harness is an orchestration-eval, not a research-eval.
- No changes to `score_from_report`, `passes`, or the judge layer — the
  metrics already carry the signal; the task shape is what's new.

## Units

### U1 — author the `v4-wide-*` fixture family

4 fixture specs, `type: code`, each asking for **N ≥ 8 member units** in a
real repo checkout, per-member `fail_to_pass` test files:

- `v4-wide-mi-filters` — more-itertools (`more-itertools-10.8` exists):
  N new utility functions across N modules, each with an independent
  hidden test file.
- `v4-wide-boltons-iterutils` — boltons (`boltons-25.1` exists): N
  independent buglets across distinct modules (each member = one fix +
  one test file).
- `v4-wide-boltons-validate` — boltons reuse: N small validators with a
  shared import surface (forces cross-member consistency).
- `v4-wide-mi-codecs` — more-itertools reuse: N file-format codecs with a
  shared base (members can't be written in total isolation — the plan
  must sequence or share the base).

**Fixture grounding is per-spec acceptance, not assumed**: only repos
whose tarballs already exist under `fixtures/` may be used on first pass —
`attrs`, `jsonlines`, etc. require a fetch + lock + member-inventory step
*before* the spec is authored, not during. Each spec also carries an
author-side inventory check: the N member units must actually exist in
the repo at the pinned commit, and each member's hidden test file must be
authored at spec time (generated-oracle specs are a separate pattern).

**Member size bound (hard constraint, learned from the boltons run):**
the worker output contract is whole-file JSON, and one 57.5KB file
already hit glm-5.3-flash's ~32k-token output cap (`finish_reason:
length`, unparseable). Member units must be **small**: target ≤ ~5KB of
emitted source per member and **≤ ~50KB total across all members** in one
subtask's output — so a spec that wants N large files must decompose to
multiple subtasks (which is *also* the orchestration signal, but a
too-large single response just dies). Specs state the bound in
`metadata.max_member_bytes` so the spec-audit can flag oversize members.

### U2 — per-member score surface in report/cards

`execution` already stores `tests_run`/`failures`/`ok`. Add
`execution.member_results: {member: bool}` — the per-member truth map.

**Concrete seam (found while reading `cubeexec.py`):** today's
`run_repo_suite` is *command-granularity* — the comment at
cubeexec.py:744 says it plainly: "Command granularity can't attribute
which declared tests failed," so a nonzero exit scores the whole batch 0.
Per-member truth needs a structured report, not stdout scraping
(`output_tail` is capped at 4,000 chars — 8+ members × `-v` lines can
overflow it). Implementation: when `fail_to_pass` entries match
`test_member_*`, the verify step runs pytest twice — the scored pass
as today, plus `--junitxml=/tmp/members.xml` (or `-v --tb=no` if junitxml
isn't guaranteed), reads the file back through the sandbox file API, and
parses `classname`/`name` → member map. Member pass iff its entry has
zero failures/errors; **absent member file in the worker artifact counts
as fail** (never inconclusive). `member_results` lands under
`report["execution"]["member_results"]`; absent on non-wide runs — no
schema break.

**Orchestrator-vs-worker attribution (the point of the benchmark):**
each member also records `targeted: bool` — whether the plan's declared
file list covered the member's path. Member absent from plan
`files`/`expected_paths` → **orchestrator miss**; targeted but tests
fail → **worker miss**. Surface `members_orchestrator_miss` /
`members_worker_miss` per run on the pairing card alongside "members:
k/N" — that split is what lets a better planner show its edge instead of
drowning in worker capability.

### U3 — rerun argon vs glm on the wide family

Same arm design as argon-v3 (same worker, replicates=3) so the two suites
are directly comparable. Prediction to falsify: argon's margin shows up
here (plan coverage + member consistency) where it couldn't on v3 — if
glm-orchestrator still ties, the "orchestrator quality" claim is dead at
this difficulty too, which is itself the finding.

**Price a cell before any matrix**: a wide run costs ≈ 1 plan call +
N×worker calls + N-file output tokens — the priciest shape we run.
First live run of each spec doubles as the cost probe (measured
`total_cost_usd` × reps × orchestrators ⇒ the real matrix estimate);
the `$25` capped key stays the spend ceiling.

### U4 — document the discrimination boundary

Append to `docs/` the measured lesson: single-file fixture tasks saturate;
orchestrator discrimination requires N-member wide tasks (or harder
per-member difficulty). Update ROADMAP/agents notes so future benchmark
authors don't re-learn it by spending runs.

## Risks / open questions

- **Subtask fan-out cost**: N members × worker calls per run — bounded by
  `max_subtasks` (default 20) and the budget gate; still the costliest
  per-run shape we run. Acceptable at glm-5.3-flash worker prices; argon
  planning cost is the variable being measured.
- **Shared-base members** (v4-wide-jsonlines-io) risk coupling: if a base
  file fails, dependent members cascade — that coupling is *intentional*
  (it's where plan sequencing matters) but must be distinguishable in the
  member_results map, not hidden in one score.
- **Fixture fetch cost**: 4 new pinned tarballs; standard `fixtures fetch`
  flow, locked as usual.

## Verification

- `harness.py validate` + `audit --strict` clean on the new specs.
- `selfcheck --execute` reports the named fixture skip (not silent).
- Dry-run each spec: plan → worker fakes → mechanical path checks.
- One live argon run + one glm-orchestrator run per spec before any matrix:
  confirm the verify path executes per-member tests and `member_results`
  parses.
- Unit test: `member_results` parser on recorded verify output.
