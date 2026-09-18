# Validation contracts that must stay invariant

Two invariants emerged from the Grok 4.3 frontier review (132 runs, 48 invalid)
and the evidence-v2 review rounds. Both prevent the same failure shape: a spec
that *looks* checked but isn't.

## Validators fail closed on unknown check names

Every name-driven validator (`_validate`, `_validate_image`, `_validate_video`,
`_validate_multi`) keeps a `known` set and fails the run when
`requested - known` is non-empty — including when known checks in the same list
pass. The pre-guard behavior was silently dropping typo'd names
(`has_requried` no-opped while `non_empty` passed), which is exactly how v1
produced "passing" runs that never measured the declared contract.

`harness.py validate` enforces the same contract earlier via `_CHECK_NAMES`
(per-type allowed names) plus `_VALIDATION_META` (names requiring metadata).
Execution-graded types (code, bugfix, swe-patch, sql, extract, api, terminal)
ignore `validation:` by design — grading is execution, not name lookup.

When adding a check name to a validator: add it to the validator's `known`
set, to `_CHECK_NAMES` for that type, and to `_VALIDATION_META` if it reads a
metadata key. `docs/task-spec.md` lists each type's check table.

## Dry-run stubs satisfy declared spec contracts

A dry run proves a spec is self-consistent — it is *not* a stub that skips
validation. Whatever the spec declares, the stub must satisfy:

- `metadata.reference_text` — pipeline/constraint dry-run artifact
- `metadata.required` — appended to the dry-run text artifact (runner.py,
  the assemble branch)
- `metadata.member_required` — injected into declared dry-run file-set
  members (planners.py `delegate_multi`)
- `metadata.expected_answer` / `reference_sql` / `reference_patch` —
  type-specific delegate stubs return the reference

The asymmetry to avoid: injecting tokens only where the validator happens to
read them today. Spec-declared requirements belong in the stub regardless of
which check currently consumes them — a validator that deepens later
(member_required did) must not turn a green dry-run red.

`member_requirements()` in `fileset.py` is the shared sanitizer: member names
canonicalize through `sanitize_path` (same form `build_zip` and `expected_paths`
use), and token values normalize to `list[str]` — a bare `"coffee"` is one
token, not five single-character checks. Two spec keys canonicalizing to the
same member union their tokens rather than silently overwriting.
