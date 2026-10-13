---
status: plan
date: 2026-10-13
type: fix
---

# Fix: stale validate/selfcheck contract vs fixture-graded specs

## Diagnosis (confirmed, not suspected)

`harness.py validate` FAILs all 9 `tasks/v3-*.yaml` specs with
`missing metadata module, tests`. The specs are correct; **the contract
table is stale**.

`_REQUIRED_META["code"] = ("module", "tests")` (harness.py:471) predates
the repo-fixture grading path. At runtime (runner.py:1710-1785):

- `fixture` present → grading goes through `verify.command` +
  `fail_to_pass` + `test_files` in the sandbox; `cubeexec` fails closed
  with `"repo suite requires metadata.verify.command"` if it's absent —
  **that** is the real hard requirement.
- `module` defaults to `solution.py` and is only used as the expected-path
  fallback when `expected_paths` is undeclared — every v3 spec declares
  `expected_paths`, so `module` is never read.
- `metadata.tests` is only read in the non-fixture `else` branch
  (`run_unittest_suite` on inline source) — dead for fixture specs.

Same staleness in two more places:

- `audit.py` `GRADING_CONTRACT["code"] = ("tests",)` — the gradability
  anchor. For fixture specs the anchor is `verify.command` + a non-empty
  staged oracle (`test_files` / `fail_to_pass`); `tests` alone is the
  wrong key.
- `selfcheck.py check_execution` reads `metadata.tests` and returns `[]`
  silently when absent — so `--execute` on a v3 spec **skips grading
  verification entirely** without reporting it. That is an
  honest-outcomes bug, not just noise.

## Units

- **U1 — conditional contract.** `_REQUIRED_META` gains a fixture
  predicate: `code` (and check `bugfix`/`swe-patch` for the same
  pattern) requires `verify.command` + `test_files` when `metadata.fixture`
  is set, `module` + `tests` otherwise. The validator should check the
  keys the runner's selected branch actually dereferences.
- **U2 — audit anchor.** `GRADING_CONTRACT["code"]` becomes
  "(`tests`) OR (`verify.command` with non-empty `test_files` or
  `fail_to_pass`)" so the vacuity check — `pass`-body suites report
  `tests_run=1 ok=True` — still fires on fixture specs with an empty
  oracle.
- **U3 — selfcheck honesty.** `check_execution` on a fixture spec either
  runs the verify path (preferred — it's the real grader, but requires a
  sandbox/env) or returns a named skip finding (`execute_skipped_fixture`),
  never silent `[]`.
- **Tests.** Validator cases covering both branches of U1; a regression
  asserting all current `v3-*.yaml` now pass `validate`; a fixture-spec
  case asserting empty `test_files` still trips the U2 anchor.

## Out of scope

- Changing grading behavior — this aligns validation with what the runner
  already does.
- Backfilling `module`/`tests` onto v3 specs to satisfy the stale table —
  wrong fix; it would declare keys the runner ignores.

## Gates

`validate` on main = 9 FAILs today. After: 0 FAILs, and a spec with
`fixture` + no `verify.command` must fail — the true runtime fatal key.
