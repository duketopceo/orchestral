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

## PR checklist

- [ ] `python -m unittest discover -s tests` passes
- [ ] `ruff check .` and `mypy orchestral harness.py` pass
- [ ] Docs updated if commands, flags, or schemas changed
- [ ] No secrets, no `runs/` artifacts in the diff
