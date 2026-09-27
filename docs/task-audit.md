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
| an `expected_paths` entry no `has_paths` covers | error | `unanchored_fileset` |
| a `required_content` declaration with no `has_content` | error | `unanchored_fileset` |

`unanchored_fileset` was a warning until the promotion, on the reasoning that
"a weak fileset is a surface a human may accept". That reasoning does not hold
for the declared-but-unread states, because nothing in the spec can close them
except editing the spec — which is the thing a gate exists to prevent. The
other two states it also reports (a fileset that declares nothing, and one
graded on names and byte counts only) are errors for the same reason: the
grader measures nothing about the artifact, so the score cannot be a
measurement. `has_content` requested with an empty `metadata.required_content`
stays inside the rule at the same severity because every artifact fails on it,
so it is a broken spec rather than a scoring surface.

## Rules

| Rule | Severity | Meaning |
| --- | --- | --- |
| `unknown_validation_check` | error | `validation:` names a check the runner does not implement for that type. Silently dropped today; the run reports a pass anyway. |
| `ignored_validation_list` | error | `code` / `sql` / `extract` / `api` never read `validation:`. They compute a fixed check set from `metadata`, so anything declared there is a phantom gate. |
| `ignored_metadata_required` | error | `metadata.required` is declared where no grader reads it. `has_required` is the only consumer and only on `html` / `constraint` / `needle`, so on any other type — or on a text type that never requests `has_required` — the key grades nothing while the spec claims a subject. |
| `absent_grading_contract` | error | A self-anchored type ships no anchor in `metadata`, so its grader has nothing to compare the artifact against: `code` with no `metadata.tests` (or a suite that is a tautology, or one that never names the module under test), `extract` with neither a required `fields` entry nor `expected`, `sql` with no `reference_sql`, `api` with no `calls`. `sql`, `api` and `extract` all fail closed at runtime — an empty `extract` contract reports `contract_anchored=false` and `passes=false`. |

| `structural_only` | warn | Nothing in the grader requires topical content. `has_title` / `has_cta` / `has_form` prove markup exists, not that the artifact is about the task, so only `has_required` with a non-empty `metadata.required`, or `matches_pattern` with a non-empty `metadata.pattern`, clear this. Both halves are required: the runner reads each declaration in exactly one place, inside that check, so a declaration without the check anchors nothing and the check without a declaration has nothing to compare against. The declaration's *shape* is modelled the way the runner reads it, because the runner iterates `required`: a mapping contributes its keys, and a bare string its characters, so `required: kite` anchors nothing. This is not keyed on the task type — `constraint` and `needle` are labels the runner uses, not graders, so a `validation: [html]` spec of either type is checked like any other. The suggested fix is type-aware: a type whose grader never reads text (`image`, `video`) has no compliant way to anchor the subject from the spec. |
| `unanchored_fileset` | error | `multi-file` grades filenames and byte counts only, or nothing at all. A fileset is anchored only when the grader reads a body: `has_paths` plus `has_content` with tokens in `metadata.required_content`. Fires in every other state — no usable `metadata.expected_paths`, a declared input no requested check reads, declared paths checked only for existence, or `has_content` requested with nothing to look for. |
| `presence_only_extract_contract` | warn | An `extract` spec grades `required` fields with no `metadata.expected`. `required` is checked for presence and type and never compared to a value, so a fabricated value scores 1.0 exactly as a correct one and `field_results` stays empty. `orchestral/extract.py` treats `required` as a legitimate anchor on purpose, so this is a coverage gap and not a contradiction of the runner. |


| `judge_gated_media` | info | An `image` / `video` artifact is encoded bytes, so no text check can anchor its subject. `png_signature` / `mp4_signature` prove format only; topicality rests on the vision judge, so a run without `--judge` grades these specs on file format alone. |

| `prompt_states_the_answer` | warn | The prompt spells out graded output — an expected value, the reference query, or the expected call list. Recitation scores the same as reasoning. `extract` is exempt by design: its prompt carries the source document. |
| `answer_derivable_from_prompt` | warn | Every graded value is readable in the prompt (`extract`, `sql`). The task ceiling is transcription and lookup, not problem solving. |
| `memorization_risk` | warn | The id or prompt matches a known textbook problem (fizzbuzz, slugify, LRU cache, expression parser, two-sum, …). A memorised answer scores the same as a solved one. |
| `near_duplicate_family` | info | Several specs share one prompt shape. They are one problem counted many times, so an aggregate pass rate inherits that single template's difficulty. The six shared terms are ordered by count, then by how rare the term is across the whole suite, then by name. The first key is the point of the finding and the second is what makes it useful: a term in most of the suite says nothing about one family, so a tie on count alone fills the list with `and` and `at`. The third key is what makes it reproducible — counting is stable, choosing among ties is not.
| `unreadable_spec_fields` | error | A field of the spec is a type the grader cannot read: `metadata` is not a mapping, `validation` is not a list of strings, `prompt` is not a string, `id` is not a string, `type` is not a string, or a requested `metadata.required` cannot be iterated. Under this file's `ok` policy — "errors mean a requested gate cannot fire" — each is a gate that cannot fire. On the file path `load_task` rejects the field-level ones with `ConfigError` before the audit sees them, so those fire for a caller that built a `TaskSpec` directly; a malformed `required` is visible either way. The rule returns before any rule runs, so findings needing only the readable fields — an unknown check name, an unimplemented type — are suppressed for that spec. |
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
  name (minus the `html` shorthand, which the runner expands) is assigned as
  `checks["<name>"]` in the body of the `Runner` method that implements that
  registry entry, and every registry key has an entry in the test's
  `VALIDATOR_FOR_TYPE` map. The body is parsed rather than substring-matched, so
  a commented-out assignment does not satisfy it. Two more tests feed the check
  a deliberately broken registry, because a guard that only ever sees a clean
  registry cannot detect its own blind spot.
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
- it carries an assertion `unittest` will actually collect — a `test*` method of
  a class that resolves to `unittest.TestCase` or
  `unittest.IsolatedAsyncioTestCase`, following the module's `import unittest`,
  `import unittest as ut`, `from unittest import TestCase as TC`,
  `from unittest import case` and `import unittest.case` forms. A coroutine
  `async def test*` on a *plain* `TestCase` does not count: unittest never
  awaits it and reports the suite as passing anyway;
- that assertion is not a constant — an assertion every expression of which
  folds to a constant evaluates the same for any artifact, so it cannot
  discriminate. `assert 1 == 1`, `assert not None`, `self.assertIn(1, [1, 2])`,
  `assert len([]) == 0` and `assert not (1 == 2)` are all caught. The check is
  method-agnostic: it does not enumerate the `assert*` methods unittest
  provides, because the version of that list differs between 3.11 and 3.14 and
  a rule pinned to a count is a rule that silently rots. A comparison
  assertion handed the same expression twice — `self.assertIs(s, s)`,
  `assertIn(x, [x])` — is also a constant: it holds for any value the name is
  bound to, so no artifact can turn it red;
- a test body that is inert (`pass`, a docstring) is not a gate;
- the suite names `metadata.module` somewhere — as `import solution`, `from
  solution import solve`, a bare `solution`, or
  `importlib.import_module("solution")`. A suite that never names it cannot read
  the artifact, so nothing it asserts is a function of the submitted code, and
  that is decidable without running anything.

**The folder is a model, not a proof.** It folds constants, lists, tuples,
sets, dicts, unary and binary operators, `and`/`or` with their
short-circuiting, comparisons (to the boolean they evaluate to, `False`
included), and calls to a fixed list of pure builtins. It does **not** fold
f-strings, slices, subscripts, comprehensions, conditional expressions, lambdas,
walrus assignments, or attribute access. A tautology written with any of those
is not detected — `assert f"{1}" == "1"` and `assert "a,b".split(",") == ["a",
"b"]` both audit clean. That is the current limit of the rule, stated as a
limit; it is not a claim that the rule asks whether an assertion can read the
artifact, because it asks the narrower question of whether the assertion is
built only from the constructs above.


**Not provable by a static read:**

- An assertion arranged by the test itself — `self.flag = True` then
  `self.assertTrue(self.flag)` — provably passes for any artifact. Detecting it
  needs execution: run the suite against a stub and require it to fail. This
  audit executes nothing, so it does not claim to catch it.
- A base class reached through indirection the resolver does not model — a class
  assigned at module level (`TC = unittest.TestCase`) rather than imported. The
  resolver follows `import` and `from ... import` forms and a base defined
  earlier in the same suite, and nothing else, so such a class does not resolve
  to a `TestCase` and the suite is reported rather than cleared. That direction is
  safe — such a suite is unusual — but the report says the suite passes for any
  artifact, which is a claim about the *author's* suite that has not been
  checked. Treat it as "not confirmed a gate", not as "is not a gate".
- A test body that does something but shows the audit no assertion — an
  assertion assembled at runtime. The rule stays silent rather than call it a
  constant, which would be false in both halves: there *is* an assertion, and
  whether the suite discriminates is not something a static read can say.
- A suite that mixes a constant assertion with one that reads the artifact. The
  rule stays silent, and the reason is not that the suite is sound: `assert 1 == 2`
  fails for *every* artifact, so the score is 0 whatever the solution produces. A
  suite pinned at 0 cannot inflate a score, which is the only thing this rule
  exists to catch, so reporting it would spend an author's attention on a spec
  that cannot game anything. The audit is not a linter for constant-failing
  tests.

**Declared anchors are checked, not assumed.** `metadata.required` is read by
the runner in exactly one place, inside `if "has_required" in requested`, and
`metadata.pattern` only inside `if "matches_pattern" in requested`. So a
declaration anchors the subject only when that check is requested *and* the
declaration names something. Requesting `has_required` with no `required`
declares nothing to compare against — the runner reports that as an error — and
`required: [""]` is satisfied by every artifact, so neither clears
`structural_only`. The shape of the declaration matters as much as its content,
because the runner iterates it: `for t in required` means a mapping contributes
its keys, a list its items, and a **bare string its characters**, so
`required: kite` asks only that the artifact contain `k`, `i`, `t` and `e` and
clears nothing. A token of one character is satisfied by nearly any artifact, so
`[""]`, `[0]` and `["a"]` declare nothing too, and a token that is entirely
whitespace declares nothing either — ordinary indented HTML satisfies `"  "`.

A token is `str(value)` and nothing more, because that is what the runner
compares. `" kite "` keeps its spaces on both sides: the audit does not strip,
so an artifact containing the bare word scores 0 exactly as the runner will
report, instead of the audit calling the spec anchored when it is not.

A declaration the runner cannot iterate at all — `required: 5`, `required: true`,
a date — is a *malformed* declaration rather than an absent one, and the advice
says so. The runner raises on it at grading time, so the audit is the only static
place that can name it.

**That finding is an error, so `--strict` exits 1 on it.** A spec whose grader
raises does not pass the gate. It was a warning in an earlier revision, on the
argument that the runner's own abort is louder than any audit line — which is an
argument about noise, not about the criterion `--strict` is wired to, and the
answer to noise is a precise message, which the finding already carries. Under
this file's own policy it is an error: `AuditReport.ok` says *"errors mean a
requested gate cannot fire"*, and a `required` the grader cannot iterate is
exactly that.

A declaration the grader *can* iterate but that names too little — a bare string,
a one-character token — is a different defect and stays a `structural_only`
warning. The severity follows the grader's behaviour rather than a preference:
unreadable is an error, readable-but-weak is a warning.

One limit on this: a `pattern` that matches every artifact — `.`, `^`, `.*`,
`[\s\S]*` — clears the finding, because deciding how strong a regex has to be is a
separate analysis from whether one was declared, and only the second is done here.
A declaration is compared against `metadata.pattern` as written, exactly as
`re.search(str(pattern), artifact)` will read it.



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

**The audit is built not to wedge, and what that claim rests on.** It is a
whole-suite gate, so a rule that never returns, or raises, costs every spec its
verdict rather than one spec a finding. Three things are bounded:

- *constant folding* — operands and results are capped, `**` and sequence
  repetition are predicted from their operands and refused before the work
  happens, and folding stops at a fixed depth. Refusing to fold is the
  conservative direction — the assertion is then treated as possibly reading the
  artifact, which is what a real assertion looks like — so the cost of a cap is a
  missed tautology;
- *base-class resolution* — a base chain is walked with an explicit worklist, not
  by recursion, because chain length is not bounded the way tree depth is. Tree
  depth is the parser's business and it refuses beyond a few hundred; a chain of
  a thousand `class C1(C0): pass` statements is a thousand separate top-level
  statements and parses fine. Recursing it took the gate's answer for every spec
  with it;
- *spec-controlled text in patterns* — `metadata.calls[].method` is interpolated
  into a regex, so it is escaped. A nested quantifier there is a denial of
  service against the gate: `(A+)+B` doubles the backtracking cost every two
  characters.

Two things this does not buy, stated because the claim above is easy to over-read:

- *It is not a bound on the clock.* Resolving a base chain is linear in the chain
  and is done once per class, so N chained classes cost O(N²): 0.4 s at N=1000,
  3.8 s at N=3000, 11.3 s at N=5000. That is a cost to whoever writes the spec,
  not an outage — no wrong answer and no crash at any size — and it is a
  spec-authoring cost rather than a denial of service, because the input has to
  live in a spec file.
- *It is a statement about the inputs that were tried, not a proof.* A suite too
  deeply nested for the parser to read at all is reported as unreadable rather
  than raised, and an input that defeats one of these bounds is not known. What
  the bounds buy is that the known ones cost a missed detection rather than a
  missing verdict.

One more bound is worth naming because it is not a bound on folding at all: **no
metadata value an author can write may raise, and no field of a spec may have a type
the grader cannot read.** All six `TaskSpec` fields are accounted for: `id`, `type`,
`metadata`, `validation` and `prompt` are checked, and `assets` is the one field
nothing in the project reads. `metadata.required` read without
a type guard raised `TypeError` on `required: 5` and took the gate's answer for
every other spec with it, in the one function in the file that read metadata
unguarded. Every metadata read is now covered by a test that feeds each rule
every shape (`TestHostileMetadataNeverRaises`), because the shape of that bug was
an omission and a fix to the one instance would not have caught the next.



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
