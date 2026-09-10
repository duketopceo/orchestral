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
runs/            output artifacts, one directory per orchestrator×worker pairing
reports/         comparison tables (markdown)
harness.py       main CLI
```

## Usage

```bash
export OPENROUTER_API_KEY=sk-or-...

# Run a task across all orchestrator × worker pairings
python harness.py run --task tasks/landing-page.yaml

# Run with a specific subset
python harness.py run --task tasks/landing-page.yaml --orchestrators 3 --workers all

# Generate comparison report
python harness.py report --task tasks/landing-page.yaml
```

## License

MIT
