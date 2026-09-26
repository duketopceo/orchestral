# Contributing

Thanks for helping improve orchestral. This file covers setup, testing, and
what a good PR looks like.

## Setup

One venv per run. `.venv` in a clone is shared by every concurrent run, and
`pip install -e .` inside it is a cross-run mutation — it can drop an extra
another run depends on, after which that run's gate reports green while
measuring less than it claims.

```bash
git clone https://github.com/duketopceo/orchestral
cd orchestral
scripts/bootstrap-venv.sh /tmp/my-venv   # creates the venv, installs .[dev,tui]
source /tmp/my-venv/bin/activate
```

`bootstrap-venv.sh` installs exactly what `ci.yml` installs (`.[dev,tui]`). A
venv built any other way does not run the same gates: without the `[tui]`
extra, `unittest discover` reports `OK` with the whole Textual suite skipped,
and `mypy` sees the TUI base class as `Any` and reports nothing where it
reports two errors with `textual` present.

## Test / lint / typecheck

```bash
python -m unittest discover -s tests   # stdlib unittest — no pytest
python harness.py audit --strict       # task specs: no unrunnable checks
ruff check .
mypy orchestral harness.py
```

All three run in CI on every PR with no secrets needed — tests, lint, and types
each as their own job, so a failure in one never stops the others from
reporting; the task-spec audit runs as a step in the `test` job. A skipped test
is not a passing test, and `python -m compileall` is not a substitute. The paid
OpenRouter eval workflow (`.github/workflows/orchestral.yml`) is dispatch-only
and environment-gated — it is never reachable from pull-request code; external
contributions are covered by the mock-provider integration tests.


## Conventions

- **Keep the stack minimal**: Python 3.11+, `pyyaml`, `httpx`, `rich`. No new
  runtime dependencies without a strong reason; optional features go in extras.
- Library code lives in `orchestral/`; the CLI lives in `harness.py`.
- Task specs go in `tasks/*.yaml`; model configs in `models/*.yaml`.
- Use `orchestral.storage.RunStore` for run reads/writes and
  `orchestral.logger.EventLogger` for per-action logging.
- Never commit `runs/`, `runs-pub/`, `reports/`, or API keys. `scrub` exists to
  produce publishable output — see [docs/publishing.md](docs/publishing.md).

### `preserve/*` refs are recovery targets, never merge bases

A `preserve/*` branch is a frozen snapshot kept so unlanded work stays
recoverable off-machine. That is its only job.

- **Never merge into one.** They are meant to stay byte-identical forever, which
  is what makes their tree hash checkable from an empty fetch. Merging `main` in
  destroys the property that made them worth having.
- **Never open a PR with one as the base.** CI tests the synthetic merge ref
  `refs/pull/N/merge`, so a PR based on a stale snapshot inherits the snapshot's
  entire history — including code that `main` has since fixed. `update-branch`
  cannot rescue it: GitHub answers `422 There are no new commits on the base
  branch`, because the base has nothing the head lacks. PR #70 is the worked
  example.
- **Retarget onto the branch the work actually lives on** — usually `main`, or the
  feature branch that carries the lineage — before opening the PR.
- **To preserve a head before merging into it**, create a *new* `preserve/*` ref
  pointing at the current head. Do not reuse or move an existing one.

### A dangling object is weaker than a `preserve/*` ref, and an empty stash list is not evidence

Work that only exists as a dangling or stash-shaped object is the weakest form
of local-only work there is. It has no name, no branch, and `git gc --prune`
reaps it on a normal schedule. A `preserve/*` ref at least has a name you can
look up.

- **Never leave a run's uncommitted work only in a stash.** If a run stashes
  rather than commits, that stash must be pushed to a named `preserve/*` ref
  before the run ends.
- **`git stash list` returning nothing is not evidence that nothing was stashed.**
  It is evidence that the stash was dropped. A dropped stash leaves its objects
  dangling and unreachable, and its absence from the stash list is the *normal*
  appearance of lost work.
- **Recover by pinning, not by looking.** `git fsck --unreachable` finds
  stash-shaped commits; give each one a ref and push it. Do not infer recovery
  from the absence of a search hit, and do not tell anyone their work is gone
  until the objects have actually been looked for.

This is not hypothetical: on this repository 32 stash-shaped dangling commits
were found and pinned in a single sitting, and 17 uncommitted paths had already
vanished from a working tree with no commit and no stash entry to account for
them.

The reason this is written down: at the time of writing, 27 of 28 `preserve/*`
refs on `origin` carry a version of `orchestral/tui/widgets.py` that `main` has
already superseded, and not one of the 28 is an ancestor of `main`. Any of them
used as a base is a red check waiting to happen, and the staleness compounds —
`main` moved twice in one evening, so refs that were "current" hours ago are not
any more. `tests/test_preserve_ref_convention.py` keeps this section from being
deleted; it does not enforce the rule, see the note in that file.

## Testing expectations

- New behavior gets a test; bug fixes get a regression test.
- A test that cannot run in the gate environment fails; it never skips. Adding
  `skipUnless` around a missing dependency re-creates the false green this
  setup exists to prevent.
- Prefer the existing patterns: `MagicMock` provider clients, `tempfile`
  dirs for run storage. No live API calls in tests.
- `tests/test_integration_mock.py` shows a full non-dry-run `Runner` execution
  with injected mock clients — copy that shape for provider-affecting changes.
- If a commit carries tests alongside non-test work, the subject line says so
  (`test:`/`fix:`/`refactor:`, not `chore:`) or the body names the test payload.
  A subject that describes only part of the payload makes the commit
  unauditable by `git log --stat`.

## Commit traceability note — `cb75325`

`cb75325` is titled `chore: make Lint and Types executable in CI`, but its
payload also adds 17 lines to `tests/test_extract_task.py` — two tests for the
list branch of `_strict_eq`, which the `zip(..., strict=True)` change in the
same commit touches. Its body documents the tests, so the content is not lost;
only the subject line is incomplete.

Those 17 lines are byte-identical to the two tests in un-merged PR 61
(`44ec97a2`). The commit is **not** from PR 61: it is from PR 52
(`fix/duk114-lint-types-green`), folded into PR 55 via `0798438` and merged as
`a069ab5`. PR 61 is still closed-unmerged, so its copy of these tests must be
dropped before that PR is reopened, or it will re-add them.

`cb75325` also does not touch `.github/workflows/ci.yml`. The `Tests -> Lint ->
Types` sequential steps with no `continue-on-error` — the coupling its own body
names as the root cause — are still in place, so a red `Tests` step still skips
both gates. The commit made the code clean; it did not make the gates
reachable. The "All three run in CI on every PR" line above is therefore not
yet true for `Lint` and `Types`.

## PR checklist

- [ ] `python -m unittest discover -s tests` passes with **no new skips**
- [ ] `ruff check .` and `mypy orchestral harness.py` pass
- [ ] Gates were run in a `scripts/bootstrap-venv.sh` venv, not a shared
      `.venv` another run may have mutated
- [ ] Docs updated if commands, flags, or schemas changed
- [ ] No secrets, no `runs/` artifacts in the diff
