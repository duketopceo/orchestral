---
title: "fix: benchmark spec integrity — reference-verified grading + discrimination report"
type: fix
date: 2026-09-27
---

# Benchmark spec integrity: reference-verified grading + discrimination report

## Summary

Make the task suite trustworthy as a measuring instrument: every spec with
executable or answer-keyed grading must carry reference material that provably
passes its own checks, `harness.py selfcheck` enforces that property in CI,
and an empirical pass reports which checks discriminate models and which are
walls or rubber stamps.

## Problem Frame

Live evidence shows the suite is not currently a reliable instrument:

- `tasks/v2-fanout-records.yaml` shipped a hidden test expecting `skipped == 3`
  where the documented rules produce 4 — every spec-conforming model failed
  (0/19 live runs), so the task produced a wall, not a signal. (Fixed on
  `feat/card-artifact-render`; the class of bug remains unguarded.)
- `v2-crossfile-service` and `v2-injection-log` pass at ~92% — near-ceiling,
  so they barely discriminate between workers.
- Six `code` specs and two `bugfix` specs carry hidden `metadata.tests` with
  **no reference implementation** — there is no way to know whether their
  tests are self-consistent without a model attempt. Only `swe-patch`
  (`metadata.patch`) and `terminal` (`metadata.commands`/`expect`) carry
  replayable references today.
- `harness.py audit` reports 0 errors but flags 130 specs missing
  `metadata.difficulty` and a 100-spec `html-batch` near-duplicate family,
  so headline results cannot weight by difficulty and one problem is counted
  ~100 times.

The failures break both directions of trust: conforming work can fail
(fanout) and weak work can pass (ceilinged checks).

## Requirements

- Every spec's declared grading must be **self-verifiable**: reference
  material in the spec must pass the spec's own validators, proven by a
  command, enforced by CI.
- The spec text is the contract. Where a hidden test contradicts documented
  rules, the test is fixed to the spec's semantics; where the spec itself is
  ambiguous, the spec is tightened to one defensible reading and the test
  follows it. Each correction records which way it resolved.
- No model calls, no eval-run code paths. Selfcheck executes only
  repo-authored content (spec tests against spec references), never model
  artifacts, and never through `Runner`.
- The empirical pass is **report-only** — stored run data influences
  warnings, never spec verdicts.
- Model-visible surfaces stay clean: nothing in this work writes to prompts,
  and nothing in this work re-opens code execution in the eval path.

## Key Technical Decisions

- **KTD-1 — References are mandatory for executable grading.** Every spec
  with `metadata.tests` must carry a `metadata.reference` file map
  (`{path: content}`) that the tests pass against. `code` specs get a new
  `reference` key; `bugfix` specs get `reference` (the *fixed* fileset,
  distinct from the shipped-broken `files`); `swe-patch` already has
  `metadata.patch` — the reference is `files` + applied `patch`, no new key.
  A spec with hidden tests and no reference fails `selfcheck` and `audit`.
- **KTD-2 — `selfcheck` is a dev/CI command, not an eval surface.**
  `harness.py selfcheck` reuses the existing **dry-run candidate seam**:
  the planners already return spec-owned reference material as the dry-run
  artifact (`reference_text`, `expected_answer`, `reference_sql`,
  `expected`, `calls`, `commands`, `patch`/`files`), so the default layer
  is "produce each spec's dry-run candidate and run it through the runner's
  validators" — no Runner instance, no RunStore writes, no model calls.
  `code`/`bugfix` have no reference to replay (KTD-1 fixes that); every
  other keyed type is exercised this way.
  - default layer — no subprocess: dry-run candidate through the same
    `validation:` checks the runner applies, plus structural checks
    everywhere (`expected_paths` ⊆ declared/reference paths,
    `required_content` tokens present in reference members, `max_code_lines`
    vs reference size).
  - `--execute` layer — runs `metadata.tests` against the reference fileset
    via a host `python -m unittest` subprocess in a fresh tmpdir, with the
    same timeout/budget shape `codeexec` uses. This is a repo-authoring tool:
    it exists to prove *our* tests accept *our* reference; it never touches
    run artifacts, never runs inside `Runner`, and is documented as
    dev-side-only alongside the fail-closed eval posture.
- **KTD-3 — Empirical discrimination is a separate read path.**
  `harness.py selfcheck --runs` mines `runs/index.db` via `RunStore`: for
  every spec with stored finished runs, per-check and per-hidden-test pass
  distributions. A check at 0% across all models is flagged `suspect` (likely
  spec/test bug — the fanout signature); a check at ≥95% is flagged
  `rubber_stamp`; a task whose combined pass rate sits at 0% or ≥95% with
  n≥5 is flagged `floor`/`ceiling`. Output is a findings list, exit 0 —
  never an error, since it depends on whatever data happens to exist.
- **KTD-4 — Corrections land in the specs, with the decision recorded.**
  Each confirmed contradiction is fixed in the YAML; the commit message and
  plan handoff note which side (spec vs test) moved. Ambiguous spec text is
  tightened in the same edit.
- **KTD-5 — Difficulty labels suite-wide.** Add
  `metadata.difficulty: easy|medium|hard` to all named specs, and emit
  `difficulty: easy` from `scripts/gen_html_tasks.py` so generated
  `html-batch-*`/`router-eval` members carry it too (the generator is
  authoritative — the committed YAMLs are pinned to its output). Note the
  interpretation caveat: a labeled batch member is still one problem counted
  ~100×; `near_duplicate_family` remains the de-weighting mechanism.

## Assumptions

- The existing audit severities stay as-is: selfcheck failures are a new
  error surface (`harness.py selfcheck` exit non-zero), while `audit`
  remains static. `audit` gains one new error: executable-graded spec
  missing reference material (KTD-1).
- `metadata.reference` is evaluation-only material: it lives in the spec
  like `metadata.tests` and `reference_sql` already do — withheld from
  prompts by the same planner-side machinery that withholds tests and answer
  keys today. The unit that adds it must verify no planner path leaks it
  into a worker/orchestrator payload.
- Stored `runs/` data on this machine is real enough to flag walls and
  ceilings; a clean machine with no runs simply reports "no data".

## Scope Boundaries

- Fix correctness and add the verification machinery. Do **not** add new
  tasks, rebalance the suite, or delete the duplicate families — those are
  composition decisions for a follow-up.
- Do **not** regrade stored runs. `revalidate` needs a live executor the
  fail-closed posture does not provide; corrected specs apply to future
  runs only.
- Do **not** touch the observatory, judge, or planner behavior beyond the
  leak check KTD-1 requires.

### Deferred to Follow-Up Work

- Whether `html-batch-100` and `router-eval` stay as families, collapse to
  representatives, or are de-weighted in headline aggregates.
- Authoring harder developer-shaped tasks (multi-file repo bugfixes,
  log-driven debugging, refactor-with-constraints) once the suite is
  trustworthy enough for results to mean something.
- Re-running / revalidating historical runs under corrected specs once an
  isolated code runtime exists.

## High-Level Technical Design

New module `orchestral/selfcheck.py` with three layers:

1. `check_spec(spec_path) -> list[Finding]` — per-spec replay of reference
   material through existing validators (reusing `runner._validate*` and
   `sqlexec`/`fileset` helpers rather than duplicating logic).
2. `check_execution(spec_path) -> list[Finding]` — `--execute` layer: write
   reference files + `metadata.tests` to a tmpdir, `python -m unittest`
   subprocess, report pass/fail per test. Explicitly host-side and
   dev-scoped; never imported by `runner.py`.
3. `check_runs(store) -> list[Finding]` — `--runs` layer: per-check pass
   distribution from `report.json`/`index.db`; emits `suspect`,
   `rubber_stamp`, `floor`, `ceiling` findings.

CLI: `harness.py selfcheck [--execute] [--runs] [--task ID]` — exits 1 on
any default-layer failure; `--execute` failures also exit 1; `--runs`
findings are always informational.

`audit` gains: executable-graded spec without reference material → error;
`unlabeled_difficulty` respects `metadata.suite` inheritance.

## Implementation Units

### U1. `orchestral/selfcheck.py` + `harness.py selfcheck` (non-executable layer)

Reference replay for every type that carries verifiable material, plus
structural consistency for all 141 specs.

Verification: `tests/test_selfcheck.py` seeds specs with deliberate
spec↔reference contradictions (wrong `expected_answer`, `reference_sql`
returning wrong rows, terminal `commands` not satisfying `expect.files`,
`required_content` token absent from reference files) and asserts each
produces a finding; `harness.py selfcheck` on the shipped suite exits 0.

### U2. `--execute` layer + `metadata.reference` for code/bugfix specs

Author reference implementations for the 6 `code` and 2 `bugfix` specs;
swe-patch verifies via `files` + `patch` → tests. Add
`selfcheck --execute` subprocess runner. Verify `metadata.reference` never
reaches a worker/orchestrator payload (leak test mirroring the existing
answer-key withholding tests).

Verification: each executable-graded spec passes `selfcheck --execute`;
`tests/test_selfcheck_exec.py` covers a failing reference (deliberate bug →
finding) and the prompt-leak path; suite stays green.

### U3. `selfcheck --runs` empirical report

Per-check and per-task pass distributions over stored runs; `suspect` /
`rubber_stamp` / `floor` / `ceiling` flags.

Verification: `tests/test_selfcheck_runs.py` seeds a RunStore where one
check fails for every model and another passes everywhere, asserts the
flags; exit code is 0 regardless of findings.

### U4. Fix confirmed spec defects + difficulty labels

Run U1–U3, fix every confirmed contradiction (fanout already fixed — use it
as the worked example), tighten ambiguous spec text, and label
`metadata.difficulty` on curated specs.

Verification: `selfcheck` + `selfcheck --execute` clean on the shipped
suite; `audit` shows 0 errors and the unlabeled-difficulty warning count
drops to the family-scoped remainder; full CI gates green.

### U5. CI wiring

`selfcheck` (default layer) and `audit` run in the `test` job; `--execute`
runs in CI too since it executes only repo-authored content.

Verification: workflow file updated; `harness.py selfcheck` exits 0 locally
in the gate venv; CI green on the PR.

## Open Questions

- For `code` specs, is one canonical reference implementation enough, or do
  we want a deliberately-wrong mutant too (proving the tests can fail)? Lean
  toward reference-only; mutants are a testing nicety, not integrity.
- Naming: `selfcheck` vs folding into `audit --execute`. Lean `selfcheck` —
  audit is static, this executes.

## Risks & Dependencies

- **Reference leakage into prompts** — mitigated by U2's explicit leak test;
  `reference` joins `tests`/`expected_answer`/`reference_sql` on the
  withhold list.
- **Ref implementations take real care** — a sloppy reference can pass a
  weak test and entrench the weakness; U4 reviews references against spec
  edge cases, not just test-pass.
- **Empirical flags depend on local data** — `suspect`/`ceiling` findings
  are advisory and must never gate CI.
- **`--execute` is a host-subprocess exception** — it is dev-tooling over
  repo-authored content; it must stay unreachable from `Runner` and is
  documented next to the fail-closed posture (see PR #53 docs).

## Sources & Research

- Local evidence: `runs/index.db` pass distributions, `harness.py audit`
  output, `tasks/v2-fanout-records.yaml` test-vs-spec contradiction (found
  2026-09-27), spec inventory (141 specs; executable grading on 10).
- No external research — this is internal correctness work; local patterns
  (`audit`, `revalidate`, `execstub`) supply the machinery.
