# Contributing

Thanks for helping improve orchestral. This file covers setup, testing, and
what a good PR looks like.

## Setup

```bash
git clone https://github.com/duketopceo/orchestral
cd orchestral
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
```

## Test / lint / typecheck

```bash
python -m unittest discover -s tests   # stdlib unittest — no pytest
ruff check .
mypy orchestral harness.py
```

All three run in CI on every PR with no secrets needed. The paid OpenRouter
eval workflow (`.github/workflows/orchestral.yml`) runs only on same-repo PRs;
external contributions are covered by the mock-provider integration tests.

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
- Prefer the existing patterns: `MagicMock` provider clients, `tempfile`
  dirs for run storage. No live API calls in tests.
- `tests/test_integration_mock.py` shows a full non-dry-run `Runner` execution
  with injected mock clients — copy that shape for provider-affecting changes.

## PR checklist

- [ ] `python -m unittest discover -s tests` passes
- [ ] `ruff check .` and `mypy orchestral harness.py` pass
- [ ] Docs updated if commands, flags, or schemas changed
- [ ] No secrets, no `runs/` artifacts in the diff
