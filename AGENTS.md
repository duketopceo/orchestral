# AGENTS.md — orchestral

This repo is intentionally isolated from personal agent tooling, LifeOS, custom
skills, and third-party MCPs. When you work here, use only the repo's own code
and commands.

## How to build and run

```bash
# run from the repo root
pip install -e .

python3 harness.py init
python3 harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --planner raw --dry-run
python3 harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --planner ce-plan --dry-run
python3 harness.py report
python3 harness.py report --html
python3 harness.py dashboard
python3 harness.py tui
python3 harness.py history
python3 harness.py ablate --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --sweep retry_limit=0,1,2 --dry-run
python3 harness.py scrub
```

## What to do

- Keep code in `orchestral/` and CLI in `harness.py`.
- Task specs go in `tasks/*.yaml`; model pricing/configs in `models/*.yaml`.
- Eval data goes to `runs/` (ignored by Git). Never commit `runs/`.
- Reports and scrubbed data go to `reports/` and `runs-pub/` (also ignored).
- Generated static reports live in `reports/`.

## What not to do

- Do not load or reference the user's LifeOS, TELOS, skills, or private rules.
- Do not add unrelated dependencies. Keep the stack: Python 3.11+,
  `pyyaml`, `httpx`, `rich`. Optional extras only: `textual` ([tui]),
  `playwright` ([shots]).
- Do not commit eval artifacts or API keys.

## Storage model

`runs/{orchestrator}/{task}/{worker}/{run_id}/` holds the full trace.
`runs/index.db` is the SQLite index for fast sorting and filtering.
Use `orchestral.storage.RunStore` for reads/writes and `orchestral.logger.EventLogger`
for per-action logging.

## Verification

Before committing, run all four CI gates in a throwaway venv built by
`scripts/bootstrap-venv.sh <dir>` — the same `.[dev,tui]` environment CI
installs. A shared `.venv` is mutated by every concurrent run, and a local run
that reports green is only meaningful if the `[tui]` extra was present.

```bash
scripts/bootstrap-venv.sh /tmp/gate-venv
/tmp/gate-venv/bin/python -m unittest discover -s tests
/tmp/gate-venv/bin/python -m ruff check .
/tmp/gate-venv/bin/python -m mypy orchestral harness.py
```

`python -m compileall` is not verification, and a skipped test is not a
passing test. To exercise a run end to end:

```bash
/tmp/gate-venv/bin/python harness.py init
/tmp/gate-venv/bin/python harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --dry-run
/tmp/gate-venv/bin/python harness.py report --html
```
