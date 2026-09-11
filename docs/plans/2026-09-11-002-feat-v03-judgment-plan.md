---
title: "v0.3 Judgment and Refinement - Plan"
type: feat
date: 2026-09-11
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: ce-plan-bootstrap
---

# v0.3 Judgment and Refinement - Plan

## Goal Capsule

- **Objective:** Implement the v0.3 roadmap items that turn the harness from "look at the pages" into "rank the pairings automatically": judge support on grid/batch with result caching, a generic `ablate` sweep command covering retry-limit and prompt-variant ablations, pluggable orchestrator planning prompts, and cost-vs-quality + per-model history views on the dashboard.
- **Authority:** `ROADMAP.md` v0.3 checklist is the product source. `AGENTS.md` stack constraint governs dependencies — no new required deps; plotting stays inside the existing HTML report surface (inline SVG), following the optional-extra precedent set by `shots`.
- **Execution profile:** One branch, atomic commits per unit. Dry-run paths must keep working without an API key. The judge cache must be safe under parallel `grid --jobs N`.
- **Stop conditions:** Any change that requires committing eval artifacts, adds a required (non-extra) dependency, or breaks `--dry-run`.
- **Tail ownership:** This plan ends at a reviewed, committed, pushed PR with CI decided; merge is the user's call.

---

## Product Contract

### Summary

v0.2 gave the harness breadth (tasks × models × modalities). v0.3 gives it judgment: every run can be scored by a frontier judge model (cached so re-runs are free), ablation sweeps answer "does more X help" questions directly, and the dashboard surfaces cost-vs-quality and per-model history instead of raw run tables.

### Requirements

**Judge everywhere + cache**

- R1. `grid` and `batch` accept `--judge <slug>` and run the existing judge pass per run (text tasks today).
- R2. Judge results are cached in `runs/index.db` keyed by `(task_id, judge_slug, artifact_sha256)` so re-running a grid or batch against unchanged artifacts costs zero judge calls. A `--no-judge-cache` flag bypasses reads (still writes).
- R3. Image tasks can be judged by a vision-capable judge model: `judge_artifact` accepts image bytes and sends them as an `image_url` content part when `task.type == "image"`. The judge model is the user's responsibility to pick; no new modality gating beyond what's already there.
- R4. Judge calls on grid/batch are parallel-safe: cache reads/writes go through `RunStore` (WAL + busy timeout already in place).

**Ablations**

- R5. New `ablate` subcommand runs one pairing across a sweep of a single knob: `harness.py ablate --task T --orchestrator O --worker W --sweep retry_limit=0,1,2` or `--sweep prompt_variant=terse,detailed`. Each sweep point is a normal run (same run-dir layout, same index rows) distinguished by a `config.sweep` marker (`{"knob": "retry_limit", "value": 2}`).
- R6. Ablations print a comparison table at the end (knob value → pass/score/cost/tokens), answer the roadmap question "does more retries fix bad workers or just burn tokens", and respect `--jobs`/`--dry-run`.
- R7. `--retry-limit N` on `run`/`grid`/`batch`/`ablate` overrides the worker's YAML `retry_limit` for that invocation only.

**Prompt variants**

- R8. Orchestrator planning prompts become files under `prompts/` (e.g. `prompts/orchestrator-terse.md`, `prompts/orchestrator-detailed.md`). The existing built-in system string becomes `prompts/orchestrator-default.md` content and remains the default.
- R9. `--prompt-variant <name>` on `run`/`grid`/`batch`/`ablate` selects `prompts/orchestrator-<name>.md`; unknown names fail fast with the list of available variants. The chosen variant is recorded in run `config` so reports and sweeps can compare.

**Judgment surfaces**

- R10. `dashboard.html` gains a cost-vs-quality scatter (inline SVG — cost per run on x, score or pass on y, colored by orchestrator×worker pairing) and a per-model history table (runs, pass rate, avg score, avg cost, total cost per role/model across all indexed runs).
- R11. `harness.py history` prints the same per-model aggregates as a terminal table, with `--orchestrator`/`--worker` filters reusing `list_runs` filters.

### Scope Boundaries

- **Deferred:** video task type (roadmap's own caveat: when OpenRouter supports it reliably); matplotlib/plotly rendering (inline SVG in the existing HTML report is sufficient — no new dep); judge-model leaderboard views beyond the history table.
- **Out:** non-OpenRouter providers, CI gates on quality thresholds, auto-publishing of reports.

### Key Technical Decisions

- **KTD-1 (session-settled):** Optional capabilities stay optional — lazy imports, extras, graceful degradation. No new extras are needed for this phase at all: SVG scatter is pure string generation in `reporter.py`.
- **KTD-2 (session-settled):** Tests are stdlib `unittest` under `tests/`; no pytest.
- **KTD-3 (session-settled):** All persistence goes through `RunStore`; all LLM-call accounting goes through `EventLogger`/`CostLedger`. The judge cache is a new table on `runs/index.db`, not a sidecar file.
- **KTD-4:** The ablation mechanism is generic (`--sweep knob=v1,v2,...` writing `config.sweep` markers) rather than two purpose-built commands — one mechanism covers both roadmap ablations and future knobs.
- **KTD-5:** Prompt variants live in `prompts/*.md` as data files, not code — keeps the ablation honest (the plan code path is identical; only the prompt text differs).

---

## Implementation Units

- **U1. Judge on grid/batch + cache + image judging**
  - `--judge` on `grid` and `batch`; pass judge into `runner.run`.
  - `judge_artifact` gains an optional image-bytes path (chat `image_url` content part, base64 data URI) used when `task.type == "image"` and a judge is provided.
  - New `judge_cache` table in `runs/index.db` via `RunStore` (`artifact_sha256, task_id, judge_slug, result_json, created_at`; PK = `(task_id, judge_slug, artifact_sha256)`); `Runner.run` consults it before calling the API and writes results back. `--no-judge-cache` skips reads.
  - Files: `harness.py`, `orchestral/runner.py`, `orchestral/judge.py`, `orchestral/storage.py`, `tests/test_judge_cache.py` (new).
  - Tests: cache hit skips the API call; `--no-judge-cache` ignores stored results; grid with judge calls judge per unique artifact; image judge path builds an image_url message; dry-run unaffected.

- **U2. `ablate` subcommand + `--retry-limit` override**
  - `--retry-limit N` on run/grid/batch overrides `worker.retry_limit` per invocation.
  - `harness.py ablate --task T --orchestrator O --worker W --sweep <knob>=<csv>` runs the pairing once per value. Supported knobs at launch: `retry_limit` (ints), `prompt_variant` (names; depends on U3). Rejects unknown knobs with a clear message.
  - Each sweep run writes `config.sweep = {"knob": ..., "value": ...}` (flow into `RunMeta.config`, already JSON).
  - Prints a sweep comparison table at the end; respects `--jobs` and `--dry-run`; non-zero exit on any failed sweep run (same convention as grid/batch).
  - Files: `harness.py` (new `cmd_ablate`, argparse entry), `orchestral/runner.py` (retry-limit plumbing if needed), `tests/test_ablate.py` (new).
  - Tests: sweep over retry_limit produces N indexed runs with distinct `config.sweep` markers; unknown knob rejected; `--dry-run` works; failures exit non-zero.

- **U3. Prompt variants under `prompts/`**
  - Extract the current orchestrator system prompt into `prompts/orchestrator-default.md`; `_build_messages` (or the plan call site) loads `prompts/orchestrator-<variant>.md` when `--prompt-variant` is given.
  - Ship `orchestrator-terse.md` and `orchestrator-detailed.md` as the ablation pair.
  - Variant recorded in run `config.prompt_variant`; missing variant file → exit 1 listing available variants.
  - Files: `orchestral/planners.py`, `orchestral/config.py` or `harness.py` for flag plumbing, `prompts/*.md` (new), `tests/test_prompt_variants.py` (new).
  - Tests: default path identical to today; terse/detailed load their files; unknown variant errors listing choices; variant lands in `meta.config`.

- **U4. Dashboard scatter + history view**
  - `reporter.py`: cost-vs-quality SVG scatter section in `_dashboard_html` (x = total cost, y = score-or-pass, per-pairing color); per-model history table (per orchestrator/worker/judge role: runs, pass rate, avg score, avg cost, total cost).
  - `harness.py history`: terminal table of the same per-model aggregates via `RunStore.list_runs`/`summary`, with `--orchestrator`/`--worker` filters.
  - Files: `orchestral/reporter.py`, `harness.py`, `tests/test_history.py` (new).
  - Tests: dashboard contains the scatter section with one point per finished run; history table aggregates correctly including unscored runs; empty index renders without errors.

- **U5. Roadmap checkboxes + README flag docs**
  - Mark the shipped v0.3 boxes; document `--judge`, `--no-judge-cache`, `--retry-limit`, `--sweep`, `--prompt-variant`, `history` in README/AGENTS command list.
  - Files: `ROADMAP.md`, `README.md`, `AGENTS.md` (command list only).

## Ordering & Dependencies

- U1 → U2 (ablate sweeps benefit from judge cache for cost sanity; both share retry plumbing).
- U3 → U2's `prompt_variant` knob (sweep supports retry_limit first; prompt_variant lands with U3 — implement whichever order is natural, but `ablate --sweep prompt_variant=...` requires U3's loader).
- U4 independent; can run in parallel with U1–U3.
- U5 last.

## Risks

- **Judge cost blowup on grid:** mitigated by the cache (R2) — re-runs and repeated artifacts across pairings cost nothing; `--judge` stays opt-in.
- **Cache key correctness:** judge result must key on artifact bytes + judge slug + task; include `task_id` so identical artifacts on different tasks don't collide.
- **Prompt-variant drift:** variants are data files; keep the default variant byte-identical to the current built-in so existing results stay comparable (record `prompt_variant` in config regardless).
- **Sweep explosion:** `ablate` is one pairing × N values, small by construction; cap sweep values at 8 with a clear error.
- **Vision judging availability:** image judging is opt-in via `--judge` on image tasks; if the judge model isn't vision-capable the API errors surface through the normal run-failure path.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --judge anthropic/claude-haiku-4.5 --dry-run
python3 harness.py grid --task landing-page-coffee --judge anthropic/claude-haiku-4.5 --jobs 5 --dry-run
python3 harness.py ablate --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --sweep retry_limit=0,1,2 --dry-run
python3 harness.py ablate --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --sweep prompt_variant=terse,detailed --dry-run
python3 harness.py history
python3 harness.py dashboard   # scatter + history sections render
```
