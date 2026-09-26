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
ruff check .
mypy orchestral harness.py
```

These are the whole definition of "verified" — `python -m compileall` is not a
substitute, and a skipped test is not a passing test. The fourth CI step,
`harness.py audit --strict`, arrives with the audit subcommand. All of them
run in CI on every PR with no secrets needed. The paid OpenRouter eval workflow
(`.github/workflows/orchestral.yml`) runs only on same-repo PRs; external
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

## Testing expectations

- New behavior gets a test; bug fixes get a regression test.
- A test that cannot run in the gate environment fails; it never skips. Adding
  `skipUnless` around a missing dependency re-creates the false green this
  setup exists to prevent.
- Prefer the existing patterns: `MagicMock` provider clients, `tempfile`
  dirs for run storage. No live API calls in tests.
- `tests/test_integration_mock.py` shows a full non-dry-run `Runner` execution
  with injected mock clients — copy that shape for provider-affecting changes.

## PR checklist

- [ ] `python -m unittest discover -s tests` passes with **no new skips**
- [ ] `ruff check .` and `mypy orchestral harness.py` pass
- [ ] Gates were run in a `scripts/bootstrap-venv.sh` venv, not a shared
      `.venv` another run may have mutated
- [ ] Docs updated if commands, flags, or schemas changed
- [ ] No secrets, no `runs/` artifacts in the diff
