# orchestral

Eval harness for testing orchestrator→worker model pairs over OpenRouter or
any OpenAI-compatible endpoint.

A big model plans and delegates. Small models write the code. We measure whether
cheap planners + cheap workers produce good output at tiny cost, or whether you
need a frontier orchestrator to squeeze quality out of budget workers.

## The core question

Does the quality of the final output depend more on:

- the **orchestrator** (planning, decomposition, assembly, error correction)?
- the **worker** (actual code generation)?
- or is there a sweet spot where a mid-tier planner gets frontier-quality results
  from budget workers?

## How it works

1. Define a task (e.g. "build a landing page for X") in a YAML spec
2. Pick orchestrator and worker model slugs
3. The harness runs each orchestrator × worker pairing through:
   - **Plan**: orchestrator decomposes the task into subtasks
   - **Delegate**: each subtask prompt goes to a worker model
   - **Assemble**: orchestrator merges worker outputs into a final artifact
   - **Validate**: structural checks (parses, non-empty, no placeholders)
   - **Judge** (optional): an LLM scores the artifact
   - **Retry**: failed subtasks go back to the worker (configurable limit)
4. Results land in `runs/` with the artifact, plan JSON, cost breakdown, and timing
5. Reports rank pairings by quality-per-dollar

## Install

```bash
pip install -e .            # from a clone
pip install "orchestral @ git+https://github.com/duketopceo/orchestral"  # or straight from git
pip install -e .[shots]     # optional: screenshot capture (playwright)
pip install -e .[dev]       # optional: ruff + mypy for development
```

Set your provider key (OpenRouter is the default):

```bash
export OPENROUTER_API_KEY=sk-or-...
```

Other providers work via `metadata` on the model config — see
[docs/providers.md](docs/providers.md).

## Quickstart

```bash
orchestral init                    # create runs/ + SQLite index
orchestral run --task landing-page-coffee \
    --orchestrator deepseek/deepseek-v4-flash-0731 \
    --worker z-ai/glm-5.3-flash --dry-run   # no API calls, sample data
orchestral run --task landing-page-coffee \
    --orchestrator deepseek/deepseek-v4-flash-0731 \
    --worker z-ai/glm-5.3-flash             # real run
orchestral report                  # table of stored runs
orchestral dashboard               # reports/dashboard.html
```

`python harness.py ...` works identically if you prefer not to install.

## Commands

| Command | What it does |
|---|---|
| `init` | Create the runs directory and SQLite index |
| `run` | One orchestrator × worker pairing on one task |
| `grid` | Every orchestrator × worker pairing on one task (`--orchestrators`, `--workers`, `--jobs`) |
| `batch` | One pairing across many tasks (`--batch-dir` or `--batch-tasks`, `--jobs`) |
| `ablate` | Sweep one knob for a pairing (`--sweep retry_limit=0,1,2` or `prompt_variant=terse,detailed`) |
| `history` | Per-model aggregates across all stored runs |
| `report` | List/compare runs (`--pairings`, `--html`, `--sort`, `--json`) |
| `dashboard` | Static HTML dashboard with cost-vs-quality scatter |
| `tui` | Live terminal dashboard (`--refresh`) |
| `shots` | Screenshot stored HTML artifacts (needs `[shots]` extra) |
| `scrub` | Redact secrets/paths from `runs/` into `runs-pub/` + `manifest.json` |

Shared run flags (on `run`, `grid`, `batch`, `ablate`): `--planner raw|ce-plan`,
`--judge <slug>`, `--no-judge-cache`, `--retry-limit N`, `--prompt-variant NAME`,
`--dry-run`, `--json`.

Global flags (before the subcommand): `--runs-dir`, `--tasks-dir`, `--models-dir`.

## Task formats

Implemented task types: **HTML page generation**, **image generation**
(OpenRouter Images API), and **video generation** (OpenRouter Videos API).
Validation checks and the full schema are documented in
[docs/task-spec.md](docs/task-spec.md); model config fields in
[docs/model-config.md](docs/model-config.md).

Planned task types: multi-file projects, API integrations.

## Structure

```
tasks/           task specs (YAML)
models/          orchestrator and worker model configs
prompts/         orchestrator prompt variants
runs/            output artifacts (ignored by git)
runs-pub/        scrubbed, publishable output of `scrub` (ignored by git)
reports/         generated reports/dashboards (ignored by git)
orchestral/      library modules (storage, logger, config, runner, providers)
harness.py       CLI entry point
tests/           stdlib unittest suite
```

## Storage

Eval artifacts are **kept local**, not committed to Git. GitHub only holds the
code, task specs, and model configs.

Each run is a folder:

```
runs/{orchestrator}/{task}/{worker}/{run_id}/
  run.json       # metadata, cost, score, status
  events.jsonl   # every agent action, reasoning, tool call, latency
  plan.json      # orchestrator decomposition
  worker-*.json  # individual worker outputs
  artifact.*     # assembled final output (html, png, ...)
  screenshot.png # rendered capture for html artifacts (optional)
  cost.json      # per-call cost breakdown
  report.json    # validation and judge results
```

`runs/index.db` is an SQLite index for fast sorting and filtering.

## Publishing results

`orchestral scrub` copies allowlisted run artifacts into `runs-pub/`, redacts
credentials/paths/endpoints, preserves binary files byte-for-byte, and writes a
`manifest.json` index. See [docs/publishing.md](docs/publishing.md).

## GitHub Action

To run evals in *another* repo, install orchestral from git inside your
workflow rather than copying this repo's workflow:

```yaml
- uses: actions/setup-python@v5
  with: {python-version: "3.11"}
- run: pip install "orchestral @ git+https://github.com/duketopceo/orchestral"
- run: orchestral run --task landing-page-coffee --orchestrator "$ORCH" --worker "$WORK"
  env:
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
```

This repo's own `.github/workflows/orchestral.yml` dogfoods the harness on
internal PRs (skipped on forks, which can't see the secret). `.github/workflows/ci.yml`
runs tests, lint, and types on every PR with no secrets required.

## License

MIT
