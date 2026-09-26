# Task-spec audit

`harness.py audit` reads every spec under `tasks/` and reports two things a
run log cannot:

1. **Can every check the suite asks for actually fire?** A misspelled or
   unimplemented `validation:` name used to be dropped without comment, so a
   spec could request a gate that never runs and still be recorded as a pass.
2. **Can a model score on a spec without doing the work?** Structural-only
   checks, answers printed in the prompt, textbook problems, and 100 copies of
   one template all inflate a score without measuring capability.

It is a pure read of the YAML — no provider calls, no model code executed — so
it is safe to run in CI on every commit.

```bash
python harness.py audit                 # human-readable
python harness.py audit --json          # machine-readable, grouped by rule
python harness.py audit --strict        # exit 1 on any error-severity finding
python harness.py audit --min-family 3 --similarity 0.9   # tune clustering
```

`--strict` is wired into `.github/workflows/ci.yml`. Errors gate the build
because a requested gate that cannot fire is a correctness bug in the spec.
Warnings do not gate: they mark a gaming surface a human may have accepted on
purpose, and the job of the audit is to make that choice visible, not to make
it for you.

## Rules

| Rule | Severity | Meaning |
| --- | --- | --- |
| `unknown_validation_check` | error | `validation:` names a check the runner does not implement for that type. Silently dropped today; the run reports a pass anyway. |
| `ignored_validation_list` | error | `code` / `sql` / `extract` / `api` never read `validation:`. They compute a fixed check set from `metadata`, so anything declared there is a phantom gate. |
| `absent_grading_contract` | error | A self-anchored type ships no anchor in `metadata`, so its grader has nothing to compare the artifact against: `code` with no `metadata.tests`, `extract` with neither a required `fields` entry nor `expected`, `sql` with no `reference_sql`, `api` with no `calls`. `sql`, `api` and `extract` all fail closed at runtime — an empty `extract` contract now reports `contract_anchored=false` and `passes=false`. |
| `structural_only` | warn | Nothing in the grader requires topical content. `has_title` / `has_cta` / `has_form` prove markup exists, not that the artifact is about the task, so only `has_required`, `matches_pattern`, or declared `metadata.required` clear this — and `metadata.required` counts only when `has_required` is actually requested, because that is the only place the runner reads it. The suggested fix is type-aware: a type whose grader never reads text (`image`, `video`) has no compliant way to anchor the subject from the spec. |
| `unanchored_fileset` | warn | `multi-file` grades filenames and byte counts only. No check reads the file bodies. Fires in all three unanchored states: no usable `metadata.expected_paths`, declared paths that `has_paths` was never asked to check, or declared paths checked only for existence. |
| `prompt_states_the_answer` | warn | The prompt spells out graded output — an expected value, the reference query, or the expected call list. Recitation scores the same as reasoning. `extract` is exempt by design: its prompt carries the source document. |
| `answer_derivable_from_prompt` | warn | Every graded value is readable in the prompt (`extract`, `sql`). The task ceiling is transcription and lookup, not problem solving. |
| `memorization_risk` | warn | The id or prompt matches a known textbook problem (fizzbuzz, slugify, LRU cache, expression parser, two-sum, …). A memorised answer scores the same as a solved one. |
| `near_duplicate_family` | info | Several specs share one prompt shape. They are one problem counted many times, so an aggregate pass rate inherits that single template's difficulty. |
| `no_holdout_arm` | info | No spec sets `metadata.holdout`, so every problem is also a published problem and contamination cannot be measured. |
| `unlabeled_difficulty` | info | No `metadata.difficulty`, so the spec cannot be excluded from a headline result. |

## Adding a check name

`VALIDATION_CHECKS` in `orchestral/audit.py` is the single source of truth for
which `validation:` names the runner implements per type, and `runner.py`
imports it. If you add a check to a validator, add it to that table in the same
change.

Two tests in `tests/test_audit.py` hold the table to the runner:

- `test_registry_covers_every_task_type_exactly_once` — every `TASK_TYPES`
  entry appears in the registry exactly once, as either a `VALIDATION_CHECKS`
  key or an `IGNORES_VALIDATION` member.
- `test_every_registered_name_is_assigned_by_its_validator` — every registered
  name (minus the `html` shorthand, which the runner expands) is assigned as
  `checks["<name>"]` in the body of the `Runner` method that implements that
  registry entry, and every registry key has an entry in the test's
  `VALIDATOR_FOR_TYPE` map. The body is parsed rather than substring-matched, so
  a commented-out assignment does not satisfy it. Two more tests feed the check
  a deliberately broken registry, because a guard that only ever sees a clean
  registry cannot detect its own blind spot.

That second test is the only thing standing between the registry and a phantom
gate. The audit itself cannot catch a name that is registered but never
assigned: it reads the same table the runner does, so a name added to
`VALIDATION_CHECKS["image"]` with no matching assignment in `_validate_image`
still audits clean and still returns exit 0 under `--strict`.

## What `absent_grading_contract` does and does not prove about a suite

The rule parses `metadata.tests` and requires three things, all decidable from
the source without executing it:

- the suite parses;
- it carries an assertion `unittest` will actually collect — a `test*` method of
  a class that resolves to `unittest.TestCase` or
  `unittest.IsolatedAsyncioTestCase`, following the module's `import unittest`,
  `import unittest as ut` and `from unittest import TestCase as TC` forms. A
  coroutine `async def test*` on a *plain* `TestCase` does not count: unittest
  never awaits it and reports the suite as passing anyway;
- that assertion reads something. An assertion is treated as reading nothing
  when every expression in it folds to a constant — `assert 1 == 1`,
  `assert not None`, `self.assertIn(1, [1, 2])`, `assert len([]) == 0`. The
  check is deliberately method-agnostic: it asks whether the assertion can read
  the artifact, not which of the 41 `assert*` methods it is.

**Not provable by a static read:**

- An assertion arranged by the test itself — `self.flag = True` then
  `self.assertTrue(self.flag)` — provably passes for any artifact. Detecting it
  needs execution: run the suite against a stub and require it to fail. This
  audit executes nothing, so it does not claim to catch it.
- A base class or assertion source reached through indirection the folder does
  not model — a class built by a metaclass, a decorator that injects a test, an
  assertion assembled at runtime.

**Why that matters here.** At this commit code execution is live:
`run_unittest_suite` writes the suite to `task_tests.py` in a temp directory and
runs `python -Es -m unittest -v task_tests` in a subprocess with
`env={"PATH": "/usr/bin:/bin"}` and a wall-clock timeout. A suite that passes for
any artifact therefore scores `score=1.0` against a stub, and the cases above are
where that can still happen. There is no OS-level isolation around that
subprocess — no container, no seccomp, no separate user, no resource limits
beyond the timeout. Whether that is an acceptable boundary for model-authored
code is a security review, tracked in DUK-87 and routed to the Identity Auditor.
This file records the exposure; it does not bless it.


## What this does not do

- It does not prove a spec is contamination-free. `memorization_risk` is a
  signature match against known textbook problems, not a probe.
- It does not check the *worker* prompt for leaked answer keys. That is a
  runtime property of `orchestral/planners.py`, and only a run can observe it.
- It does not judge artifacts. It grades the problem, not the response.
