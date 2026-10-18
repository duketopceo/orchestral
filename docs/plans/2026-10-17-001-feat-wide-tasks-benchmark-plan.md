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

- `v4-wide-mi-filters` — more-itertools: N new utility functions across N
  modules, each with an independent hidden test file.
- `v4-wide-boltons-iterutils` — boltons: N independent buglets across
  distinct modules (each member = one fix + one test file).
- `v4-wide-attrs-validators` — attrs (or another fetched fixture): N
  validators to implement, shared import surface (forces cross-member
  consistency).
- `v4-wide-jsonlines-io` — N file-format codecs with a shared base
  (members can't be written in total isolation — plan must sequence or
  share the base).

Fixture tarballs via `fixtures fetch` as today; `expected_paths` declares
the N member paths; `verify.command` runs the staged pytest tree;
`fail_to_pass` names `test_member_*` files so per-member score is
recoverable.

### U2 — per-member score surface in report/cards

`execution` already stores `tests_run`/`failures`/`ok`. Add a
`member_results` map `{member: bool}` parsed from the verify output when
`fail_to_pass` names follow `test_member_<name>`, surfaced on the pairing
card as "members: k/N". No schema break — additive key, absent on
non-wide runs.

### U3 — rerun argon vs glm on the wide family

Same arm design as argon-v3 (same worker, replicates=3) so the two suites
are directly comparable. Prediction to falsify: argon's margin shows up
here (plan coverage + member consistency) where it couldn't on v3 — if
glm-orchestrator still ties, the "orchestrator quality" claim is dead at
this difficulty too, which is itself the finding.

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
