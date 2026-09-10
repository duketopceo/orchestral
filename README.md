# orchestral

OpenRouter eval harness for testing orchestrator→worker model pairs.

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
2. Pick orchestrator models and worker models from OpenRouter
3. The harness runs every orchestrator × worker pairing through:
   - **Plan**: orchestrator decomposes the task into subtasks
   - **Delegate**: each subtask prompt goes to a worker model
   - **Assemble**: orchestrator merges worker outputs into a final artifact
   - **Validate**: structural checks (parses, non-empty, no errors)
   - **Retry**: failed subtasks go back to the worker (configurable limit)
4. Results land in `runs/` with the artifact, plan JSON, cost breakdown, and timing
5. A comparison report ranks pairings by quality-per-dollar

## Task formats

First task type: **HTML page generation** — cheap to run, visually verifiable by
screenshot grid, and naturally decomposes into subtasks.

Planned task types: video generation, image generation, multi-file projects,
API integrations.

## Structure

```
tasks/           task specs (YAML)
models/          orchestrator and worker model configs
runs/            output artifacts (ignored by git)
reports/         comparison tables (markdown)
ui/              static web UI for browsing runs
orchestral/      library modules (storage, logger, config, runner)
harness.py       main CLI
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
  artifact.*     # assembled final output (html, json, zip, ...)
  cost.json      # per-call cost breakdown
  report.json    # validation and judge results
```

`runs/index.db` is an SQLite database that indexes every run. The CLI uses it for
fast sorting and filtering, and the web UI can load it later without walking the
whole tree.

If you outgrow local storage, swap `orchestral/storage.py` to use R2/S3 or a
separate `orchestral-runs` repo. The default keeps it cheap and private.

## Usage

```bash
export OPENROUTER_API_KEY=sk-or-...

# Run a task across all orchestrator × worker pairings
python harness.py run --task tasks/landing-page.yaml

# Run with a specific subset
python harness.py run --task tasks/landing-page.yaml --orchestrators 3 --workers all

# Generate comparison report
python harness.py report --task tasks/landing-page.yaml

# Terminal dashboard
python harness.py tui
```

## GitHub Action (per-repo, no local install)

Copy `.github/workflows/orchestral.yml` into the target repo and set the
`OPENROUTER_API_KEY` secret. On every PR it:

- installs `orchestral`
- runs the eval
- uploads the HTML report + dashboard as artifacts
- comments the cost/token summary on the PR
- approves the PR if `total_cost_usd <= MAX_COST_USD`, requests changes if not

For a true "reviewer" experience like TestDriver, build a GitHub App that uses
the same code path; the Action is the simplest per-repo setup today.

## License

MIT
