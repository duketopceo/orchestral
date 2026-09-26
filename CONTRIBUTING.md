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

## Testing expectations

- New behavior gets a test; bug fixes get a regression test.
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
reachable. The "All four run in CI on every PR" line above is therefore not
yet true for `Lint` and `Types`.

## PR checklist

- [ ] `python -m unittest discover -s tests` passes
- [ ] `ruff check .` and `mypy orchestral harness.py` pass
- [ ] Docs updated if commands, flags, or schemas changed
- [ ] No secrets, no `runs/` artifacts in the diff
