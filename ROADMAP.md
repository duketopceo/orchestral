# Roadmap

## v0.1 — HTML page generation (MVP)

- [x] `harness.py` CLI: `run` and `report` subcommands
- [x] OpenRouter client (httpx, streaming optional, cost tracking from response headers)
- [x] Task spec loader (pydantic, YAML)
- [x] Model config: orchestrators list, workers list, retry limits per worker
- [x] Plan→delegate→assemble→validate→retry loop
- [x] One task: `tasks/landing-page.yaml` (coffee subscription, SaaS, portfolio — pick one)
- [x] Validation: HTML parses, non-empty, no obvious placeholder text
- [x] Output: `runs/<orchestrator>/<worker>/` with artifact + plan.json + cost.json
- [x] Comparison report: markdown table with cost, pass/fail, token counts
- [x] Default models: 3 orchestrators × 5 workers = 15 runs per task

**Goal:** run one task, see the grid, know what a pairing costs.

## v0.2 — More tasks, more models

- [x] 100 HTML page task batch (10 prompts × all pairings) — specs in `tasks/batch-100/`, `harness.py batch` command
- [ ] Add image generation task type (OpenRouter multimodal)
- [ ] Expand model grid: 5 orchestrators × 10 workers
- [ ] Parallel runs (asyncio or multiprocessing, one pairing per process)
- [ ] Cost table sorted by quality-per-dollar
- [ ] Screenshot capture for HTML outputs (playwright)
- [ ] Visual comparison grid (HTML gallery page)

**Goal:** answer the core question with enough data points to be meaningful.

## v0.3 — Judgment and refinement

- [ ] LLM-as-judge (frontier model scores each output, cached)
- [ ] Retry-limit ablation: does more retries fix bad workers or just burn tokens?
- [ ] Orchestrator prompt ablation: terse vs detailed planning prompts
- [ ] Cost-per-quality scatter plot (matplotlib)
- [ ] Historical model performance tracking across runs
- [ ] Video generation task type (when OpenRouter supports it reliably)

**Goal:** move from "look at the pages" to "rank the pairings automatically."

## v1.0 — Shared tool

- [ ] Publish as open-source repo with proper docs
- [ ] Task spec schema documented
- [ ] Model config schema documented
- [ ] CI: lint, type check, basic integration test with mock OpenRouter
- [ ] Example results published (the 100 HTML page grid)
- [ ] Support non-OpenRouter providers (direct API keys for comparison)
