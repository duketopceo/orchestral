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
python harness.py audit --no-holdout-arm                  # ignore the generator
```

`--strict` is wired into `.github/workflows/ci.yml`. Errors gate the build
because a requested gate that cannot fire is a correctness bug in the spec.
Warnings do not gate: they mark a gaming surface a human may have accepted on
purpose, and the job of the audit is to make that choice visible, not to make
it for you.

**What the warn-does-not-gate policy does not cover.** One class of warning is
not a judgement call, and it is error: a gate input the author declared that no
check reads. The spec says "grade these two files" or "these tokens must be in
this body", the grader looks at neither, and the audit names the exact fix and
then exits 0. Deleting a token from `validation:` is then enough to reach green
with no edit to the audit — which is the same evasion as a misspelled check name
that the runner drops, and that one has always been an error. Two rules sit on
this boundary:

| Declared but never read | Severity | Rule |
| --- | --- | --- |
| a `validation:` name the runner cannot run | error | `unknown_validation_check` |
| a `validation:` name on a type that ignores `validation:` | error | `ignored_validation_list` |
| `metadata.expected_paths` with no `has_paths` covering it | error | `unanchored_fileset` |
| `metadata.required_content` with no `has_content` | error | `unanchored_fileset` |

`unanchored_fileset` was a warning until the promotion, on the reasoning that
"a weak fileset is a surface a human may accept". That reasoning does not hold
for the declared-but-unread states, because nothing in the spec can close them
except editing the spec — which is the thing a gate exists to prevent. The
other two states it also reports (a fileset that declares nothing, and one
graded on names and byte counts only) are errors for the same reason: the
grader measures nothing about the artifact, so the score cannot be a
measurement. `has_paths` requested with an empty `metadata.required_content`
stays inside the rule at the same severity because every artifact fails on it,
so it is a broken spec rather than a scoring surface.

## Rules

| Rule | Severity | Meaning |
| --- | --- | --- |
| `unknown_validation_check` | error | `validation:` names a check the runner does not implement for that type. Silently dropped today; the run reports a pass anyway. |
| `ignored_validation_list` | error | `code` / `sql` / `extract` / `api` never read `validation:`. They compute a fixed check set from `metadata`, so anything declared there is a phantom gate. |
| `absent_grading_contract` | error | A self-anchored type ships no anchor in `metadata`, so its grader has nothing to compare the artifact against: `code` with no `metadata.tests` (or a suite that is a tautology, or one that never names the module under test), `extract` with neither a required `fields` entry nor `expected`, `sql` with no `reference_sql`, `api` with no `calls`. `sql` and `api` fail closed at runtime; `extract` does not — an empty contract grades `{}` as `passes=True score=1.0`. |
| `unanchored_fileset` | error | `multi-file` grades filenames and byte counts only, or nothing at all. A fileset is anchored only when the grader reads a body: `has_paths` plus `has_content` with tokens in `metadata.required_content`. Fires in every other state — no usable `metadata.expected_paths`, a declared input no requested check reads, declared paths checked only for existence, or `has_content` requested with nothing to look for. See the boundary above. |
| `presence_only_extract_contract` | warn | An `extract` spec grades `required` fields with no `metadata.expected`. `required` is checked for presence and type and never compared to a value, so a fabricated value scores 1.0 exactly as a correct one and `field_results` stays empty. `orchestral/extract.py` treats `required` as a legitimate anchor on purpose, so this is a coverage gap and not a contradiction of the runner. |
| `structural_only` | warn | Nothing in the grader requires topical content. `has_title` / `has_cta` / `has_form` prove markup exists, not that the artifact is about the task, so only `has_required`, `matches_pattern`, or — for text-producing types only — declared `metadata.required` clear this. `image` / `video` are exempt: their artifacts are bytes, so a text token is an unimplemented check rather than a loose one. See `judge_gated_media`. |
| `judge_gated_media` | info | An `image` / `video` artifact is encoded bytes, so no text check can anchor its subject. `png_signature` / `mp4_signature` prove format only; topicality rests on the vision judge, so a run without `--judge` grades these specs on file format alone. |
| `prompt_states_the_answer` | warn | The prompt spells out graded output — an expected value, the reference query, or the expected call list. Recitation scores the same as reasoning. `extract` is exempt by design: its prompt carries the source document. |
| `answer_derivable_from_prompt` | warn | Every graded value is readable in the prompt (`extract`, `sql`). The task ceiling is transcription and lookup, not problem solving. |
| `memorization_risk` | warn | The id or prompt matches a known textbook problem (fizzbuzz, slugify, LRU cache, expression parser, two-sum, …). A memorised answer scores the same as a solved one. |
| `near_duplicate_family` | info | Several specs share one prompt shape. They are one problem counted many times, so an aggregate pass rate inherits that single template's difficulty. |
| `no_holdout_arm` | info | No holdout arm exists, so every problem is also a published problem and contamination cannot be measured. Satisfied by a committed `metadata.holdout` spec **or** by a holdout arm that actually generates. |
| `unlabeled_difficulty` | info | No `metadata.difficulty`, so the spec cannot be excluded from a headline result. |


## The holdout arm

`no_holdout_arm` clears when a holdout arm exists, and the normal way to have one
is to generate it — a committed holdout spec is in git, so it is a published
problem wearing a holdout label:

```bash
python harness.py holdout --out runs-holdout --count 8 --seed 4242
python harness.py batch --batch-dir runs-holdout --orchestrator <slug> --worker <slug> --dry-run
```

The audit does not take that on trust. It calls the generator and requires specs
that are marked holdout and whose prompts are not already in the suite; a
generator that is missing, raises, or replays published prompts leaves the
finding standing. Accepting a declared-but-unverified arm would let the rule
report that a measurement exists while nothing produces one.

`harness.py report --contamination` then prints mean score on the published arm,
mean score on the holdout arm, and the gap, per task type. A gap is only
defined where a type appears in both arms — across types the difference measures
a change of subject, not contamination — and the report prints each side's `n`
and flags types with fewer than five runs on a side as anecdote.

## Who owns `score`: the judge is advisory

**Decision: the mechanical grade is authoritative. The judge's verdict is
recorded beside it and does not overrule it.**

The runner used to do this:

```python
report["judge"] = judge_result
if judge_result.get("score") is not None:
    report["score"] = judge_result["score"]      # measured grade overwritten
if judge_result.get("passed") is not None:
    passes = passes and judge_result["passed"]    # verdict ANDed with the judge
```

So a stored `report["score"]` and a stored `passes` could both be the LLM judge's
opinion rather than a measurement. The only calibration on file,
`reports/judge-calibration.md`, does not support that authority: its own
limitation section records kappa 0.41 measured against the **validator** fallback
with **zero live judge verdicts** (`judge_output_coverage.non_null_judge_outputs:
0`). The number in that report is human-label agreement with the mechanical
grade. It says nothing about the judge, because the judge column was empty.

That is the same failure mode the rest of this file is about: a stored number
that looks authoritative and is not. The fix is not to make the number more
confident, it is to stop the uncalibrated signal from occupying the authoritative
slot.

**What changed, and what did not.**

| | before | after |
|---|---|---|
| `report["score"]` | judge's score when a judge ran | the mechanical score, always |
| `passes` | mechanical **and** judge | mechanical only |
| `report["judge"]` | judge's verdict | unchanged — score, passed, reasoning all still stored |
| `report["score_source"]` | absent | `"mechanical"`, so the record names its own authority |
| `meta.failure_reason` | `"judge"` when checks passed but the judge said no | `"validation"` — a judge disagreement is kept in `report.judge`, not relabelled as the cause |

`JUDGE_IS_AUTHORITATIVE` in `orchestral/runner.py` is `False` and names this
section. It is not a flag to flip on a hunch: restoring judge precedence needs a
calibration whose `judge_output_coverage.non_null_judge_outputs` is greater than
zero, which means new judged runs and a fresh `harness.py calibrate` pass, and
then a deliberate code change. A flag flip would let the next person restore an
unvalidated override without reading why it was removed.

**What would change the decision.** A kappa computed against real judge verdicts
on a labelled set, high enough that you would trust the judge over the validator
on a case where they disagree. Below that, keep the mechanical grade and treat
the judge as a second opinion for a human to read. Note that the two graders
disagree for a reason worth keeping: the eight false positives in the calibration
were landing-page artifacts that satisfy the structural checks and do not satisfy
the task contract. The mechanical grade is not the ceiling of what is knowable
here — it is the part that is currently measured.

**Not done, deliberately.** No attempt was made to improve the judge, the
prompts, or the validator coverage. This records who owns a number that was
stored without a mandate.

## Adding a check name

`VALIDATION_CHECKS` in `orchestral/audit.py` is the single source of truth for
which `validation:` names the runner implements per type, and `runner.py`
imports it. If you add a check to a validator, add it to that table in the same
change.

Three tests in `tests/test_audit.py` hold the table to its two other copies:

- `test_registry_covers_every_task_type_exactly_once` — every `TASK_TYPES`
  entry appears in the registry exactly once, as either a `VALIDATION_CHECKS`
  key or an `IGNORES_VALIDATION` member.
- `test_every_registered_name_is_assigned_by_its_validator` — every registered
  name (minus the `html` shorthand, which the runner expands) appears as
  `checks["<name>"] =` in the body of the `Runner` method that implements that
  registry entry, and every registry key has an entry in the test's
  `VALIDATOR_FOR_TYPE` map.
- `test_every_registered_check_name_is_documented_in_the_task_spec` — every
  registered name appears in the hand-maintained check table in
  `docs/task-spec.md`, so a name cannot ship implemented, tested, and
  undocumented.

The second test is the only thing standing between the registry and a phantom
gate. The audit itself cannot catch a name that is registered but never
assigned: it reads the same table the runner does, so a name added to
`VALIDATION_CHECKS["image"]` with no matching assignment in `_validate_image`
still audits clean and still returns exit 0 under `--strict`.

**A third copy exists and is not a gate.** `orchestral/planners.py` carries
`"success_criteria": ["parses", "non_empty", "has_title", "has_cta",
"has_form"]` in two places. `parses` is not a registered name — the registry
name is `html_parses` — so the list is one token out of date. Nothing reads
`success_criteria`; it is an LLM prompt hint, so it cannot gate anything and no
test guards it. It is recorded here rather than fixed, because the module is
outside the scope of the gate work and correcting it would put an unrelated
change in this diff. If a check is ever made load-bearing there, this list
becomes a fourth copy to guard.

## What `absent_grading_contract` does and does not prove about a suite

The rule parses `metadata.tests` and requires four things, all decidable from
the source without executing it:

- the suite parses;
- it carries an assertion `unittest` will actually collect — inside a `test*`
  method of a `unittest.TestCase` subclass, since that is all the default loader
  collects;
- that assertion is not a tautology. Three shapes are provable and are flagged:
  an `assert` whose test folds to a truthy constant, a `unittest` assertion
  whose argument constant-folds (`assertTrue(True)`, `assertEqual(1, 1+0)`,
  `assertEqual(0, len(''))`), and a comparison assertion handed the same
  expression twice or a container holding nothing but the needle
  (`self.assertIs(s, s)`, `assertIn(x, [x])`).
- the suite names `metadata.module` somewhere — as `import solution`, `from
  solution import solve`, a bare `solution`, or
  `importlib.import_module("solution")`. A suite that never names it cannot read
  the artifact, so nothing it asserts is a function of the submitted code, and
  that is decidable without running anything.

Folding is what closes the arithmetic and comparison shapes.
`ast.literal_eval` rejects `1 + 0` and `1 == 1` outright, so a suite asserting
either audited clean while `assertEqual(1, 1)` did not. The folder in
`orchestral/audit.py` folds the operators over already-foldable operands, plus
`len`/`abs`/`str`/`int`/`float`/`bool` over a foldable argument. A name, an
attribute, or a call it does not know is not a constant, so `assert result is
not None` is never mistaken for one.

**Not provable by a static read:** an assertion that repeats an expression
*containing a call*. `self.assertEqual(f(x), f(x))` is textually the same shape
as `assertIs(s, s)`, but `f` may read the artifact, so the outcome is not fixed
and the doc keeps it as execution-only. A repeat with no call in it is a
guaranteed pass and is flagged; a repeat with a call in it is not. Detecting the
first kind of self-arranged assertion — `self.assertTrue(self.flag)` after the
test sets it — needs execution: run the suite against a stub and require it to
fail. This audit executes nothing, so it does not claim to catch those.

**Why that matters here.** At this commit code execution is live:
`run_unittest_suite` writes the suite to `task_tests.py` in a temp directory and
runs `python -Es -m unittest -v task_tests` in a subprocess with
`env={"PATH": "/usr/bin:/bin"}` and a wall-clock timeout. A suite that passes for
any artifact therefore scores `score=1.0` against a stub, and this rule cannot
see it. There is no OS-level isolation around that subprocess — no container, no
seccomp, no separate user, no resource limits beyond the timeout. Whether that
is an acceptable boundary for model-authored code is a security review, tracked
in DUK-87 and routed to the Identity Auditor. This file records the exposure; it
does not bless it.

## What this does not do

- It does not prove a spec is contamination-free. `memorization_risk` is a
  signature match against known textbook problems, not a probe.
- It does not check the *worker* prompt for leaked answer keys. That is a
  runtime property of `orchestral/planners.py`, and only a run can observe it.
- It does not judge artifacts. It grades the problem, not the response.
- It does not check that an anchor is *distinguishing*, only that one is
  declared. A `has_required` token copied out of the prompt into an `<h1>`
  satisfies the check without any topical work. `tests/test_spec_anchors.py`
  is the counterweight: it holds hand-written honest pages and asserts that the
  generic template scores on none of them and that the family discriminates.
