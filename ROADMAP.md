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
- [x] Add image generation task type (OpenRouter Images API, `artifact.png`, per-image pricing)
- [x] Expand model grid: 5 orchestrators × 10 workers (image workers filtered by `modalities` metadata)
- [x] Parallel runs (`--jobs N` on `grid` and `batch`, thread pool, SQLite WAL index)
- [x] Cost table sorted by quality-per-dollar (`harness.py report --pairings`)
- [x] Screenshot capture for HTML outputs (`harness.py shots`, optional `playwright` extra, auto-capture at end of run)
- [x] Visual comparison grid (`reports/gallery.html`, screenshot thumbnails with iframe fallback)

**Goal:** answer the core question with enough data points to be meaningful.

## v0.3 — Judgment and refinement

- [x] LLM-as-judge (frontier model scores each output, cached) — `--judge` on run/grid/batch/ablate, results cached in `runs/index.db` keyed on (task, judge, artifact sha); `--no-judge-cache` bypasses reads; image tasks judged via vision-capable models
- [x] Retry-limit ablation — `harness.py ablate --sweep retry_limit=0,1,2` (does more retries fix bad workers or just burn tokens?)
- [x] Orchestrator prompt ablation — `prompts/orchestrator-*.md` variants + `--prompt-variant`; sweep via `ablate --sweep prompt_variant=terse,detailed`
- [x] Cost-per-quality scatter plot — inline SVG on `dashboard.html` (zero-dep instead of matplotlib)
- [x] Historical model performance tracking — `harness.py history` + per-role tables on the dashboard
- [x] Video generation task type (OpenRouter async Videos API; `artifact.mp4`, `mp4_signature` validation, per-second pricing; judging deferred)

**Goal:** move from "look at the pages" to "rank the pairings automatically."

## v1.0 — Shared tool

- [x] Publish as open-source repo with proper docs
- [x] Task spec schema documented (`docs/task-spec.md`)
- [x] Model config schema documented (`docs/model-config.md`)
- [x] CI: lint, type check, basic integration test with mock providers (`.github/workflows/ci.yml`)
- [ ] Example results published (the 100 HTML page grid) — `scrub` → `runs-pub/` + `manifest.json` ships the pipeline (`docs/publishing.md`); publishing real run data is a follow-up once run data is synced to this machine
- [x] Support non-OpenRouter providers (OpenAI-compatible chat endpoints via `metadata.provider`/`base_url`/`api_key_env`; image tasks remain OpenRouter-only)
