---
name: orchestral-quickstart
description: Run a model-pairing eval with orchestral from zero context. Use when asked to compare an orchestrator model with worker models on real tasks, produce pass/fail evidence with cost, or measure whether a cheaper orchestrator beats an expensive one. Covers install, key-free verification, writing a task spec and model config, one real run, and reading the verdict.
---

# orchestral — agent quickstart

orchestral is an open-source eval harness for one specific question: given a
task, does the outcome improve more when you spend on the **orchestrator**
(planning, decomposition, assembly) or on the **worker** (the model that
writes the artifact)? It runs orchestrator × worker pairings against YAML
task specs, validates artifacts mechanically, and scores them with a
decisions-engine judge (not a chat model).

This document is the whole path. You do not need to have seen this repo
before. Two routes:

- **No API key** — steps 1-4 prove the harness end-to-end with `--dry-run`.
  Costs nothing, calls no model, still writes a real run directory.
- **With an `OPENROUTER_API_KEY`** — steps 5-8 produce a real judged run.
  A single run on a small task costs roughly $0.0001-$0.01; tell your
  human before spending.

Verified against harness `1.0.0`, Python 3.11+.

## Steps

1. **Clone and install.**

   ```bash
   git clone https://github.com/duketopceo/orchestral && cd orchestral
   python3 -m venv .venv && .venv/bin/pip install -e .
   ```

   `orchestral` and `python harness.py` are equivalent entry points; the
   examples use `harness.py` so no PATH juggling is needed.

2. **Initialize the run store.**

   ```bash
   .venv/bin/python harness.py init
   ```

   Creates `runs/` and a SQLite index at `runs/index.db`. Global flags
   like `--runs-dir PATH` go **before** the subcommand
   (`harness.py --runs-dir /tmp/x run ...`).

3. **Prove the plumbing with a dry run (no key needed).**

   ```bash
   .venv/bin/python harness.py run --task landing-page-coffee \
       --orchestrator deepseek/deepseek-v4-flash-0731 \
       --worker z-ai/glm-5.3-flash --dry-run
   ```

   `--task` takes the spec's `id:` field, not its filename —
   `tasks/landing-page.yaml` has `id: landing-page-coffee`. A dry run
   writes a real run directory with synthetic output and no API calls.
   Expect `Passes: False | Failure: validation` — the synthetic artifact
   deliberately does not satisfy the task's content checks. That is the
   plumbing proving itself, not a model verdict.

4. **Read the run directory.** Each run lands at
   `runs/<orchestrator>/<task>/<worker>/<run_id>/`:

   | File | What it is |
   |------|------------|
   | `manifest.json` | pins: `harness_version`, `git_commit`, `task_hash`, `orchestrator_prompt_hash`, model slugs, python/platform. A score diff means the run changed, not the environment |
   | `report.json` | the verdict: `passed`, `score`, `checks` (per-check booleans), `judge` block, `errors` |
   | `artifact.*` | the thing the worker actually produced |
   | `plan.json` | the orchestrator's decomposition |
   | `cost.json`, `events.jsonl` | billed spend and the call ledger |

   `harness.py report` prints the run table. `harness.py serve` opens the
   observatory UI over the same store.

5. **Keyed path: set the key, pick slugs, run for real.**

   ```bash
   export OPENROUTER_API_KEY=sk-or-...   # never write it to a file in the repo
   .venv/bin/python harness.py run --task landing-page-coffee \
       --orchestrator deepseek/deepseek-v4-flash-0731 \
       --worker z-ai/glm-5.3-flash
   ```

   Any `slug` in `models/*.yaml` works; `orchestral models sync` refreshes
   pricing from OpenRouter. The judge defaults to `~typesafe/jev-latest`
   (a decisions engine: typed verdict + calibrated score, no free-text
   parsing). `--no-judge` skips judging; `--judge a,b` adds second-opinion
   judges under `report.judges`.

6. **Write your own task** — `tasks/<name>.yaml`:

   ```yaml
   id: my-first-task        # the --task argument; unique across tasks/
   type: html               # html | code | multi-file | sql | needle | ...
   title: "Tea shop landing page"
   prompt: |
     Build a landing page for a fictional tea shop with a headline,
     three feature sections, and a subscribe form. Mention pricing.
   metadata:
     difficulty: easy
   ```

   Full schema: `docs/task-spec.md`. `harness.py validate` checks every
   spec; on a fresh clone it reports ~9 known `FAIL` lines for `v3-*`
   real-repo tasks (they need a checked-out module) — not a problem with
   your spec. Note for first runs: `code`-type tasks need an isolated
   runtime (E2B/CubeSandbox) to execute their test contract; without one
   they report `validation` failure ("code execution disabled"). `html`
   tasks validate statically and need nothing extra.

7. **Add a model** — append to `models/default.yaml` (or a new file there):

   ```yaml
   models:
     - slug: "vendor/model-name"      # OpenRouter slug
       name: "Display Name"
       role: worker                    # orchestrator | worker | judge | reference
       input_price_per_mtok: 0.10
       output_price_per_mtok: 0.30
       context: 131072
       max_tokens: 32768
   ```

   Prices drive `cost_usd` honesty; OpenRouter-reported usage is
   authoritative when present. Other providers: `docs/providers.md`.
   Judge configs live in the same directory (`role: judge`,
   `metadata.engine: decisions`).

8. **Get a real signal, not a single sample.** One run is an anecdote:

   ```bash
   .venv/bin/python harness.py run --task landing-page-coffee \
       --orchestrator A --worker B --replicates 4 --seed 7
   .venv/bin/python harness.py report          # the table
   .venv/bin/python harness.py serve           # the observatory
   ```

   `report --judge-agreement` compares judges when runs carry second
   opinions. `harness.py experiment --matrix experiments/<name>.yaml`
   drives a full pairing matrix with a budget cap.

## Reading a verdict honestly

- `status: finished` + `passed`/`score` — a real verdict.
- `status: finished` + `judge.inconclusive` — the judge saw the artifact
  and could not decide; the run counts but scores nothing.
- `status: failed` — the run died *before* a verdict; `failure_reason`
  names the class (`malformed_output`, `validation`, `transport`,
  `executor_*`). It is an exception taxonomy, not "the model was bad".
- Dry-runs never carry real verdicts regardless of what they print.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `unrecognized arguments: --runs-dir` | global flags go before the subcommand |
| `No task found for id 'x'` | `--task` wants the spec's `id:` field, not the filename |
| `validate` FAILs on `v3-*` specs | known — real-repo tasks need a checked-out module; ignore |
| `code`-type run fails validation: "code execution disabled" | needs an isolated runtime; use `html`/`sql`/`needle` tasks without one |
| Judge inconclusive on a real run | usually a provider hiccup; `harness.py judge` backfills verdicts later |
| `401`/auth errors on a real run | `OPENROUTER_API_KEY` unset or wrong; check `echo ${OPENROUTER_API_KEY:+set}` |

## Rules for agents

- Never commit `runs/`, keys, or `.env` — `runs/` is gitignored, keep it
  that way.
- State expected spend before a real run; `--max-cost USD` hard-caps a
  run and `--daily-cap USD` caps a batch.
- `orchestral init`/`run` against an existing `runs/` is additive and
  safe — never delete a run store that isn't yours.
