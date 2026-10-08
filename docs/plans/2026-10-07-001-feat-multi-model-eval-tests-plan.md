---
title: "feat: Multi-model judge + embedding comparison tests"
type: feat
date: 2026-10-07
---

# feat: Multi-model judge + embedding comparison tests

## Summary

Run every eval artifact through a small **registry of judge models** and a
small **registry of embedding models**, each output marked with the model
that produced it, so verdicts and clusters become comparable evidence rather
than single-model opinions.

Spike evidence (2026-10-07, `/tmp/pplx-spike/`): `pplx-decider-v1.1-27b`
answers the same `noul`/`score` questions on the same `/api/alpha/decisions`
endpoint as the incumbent `~typesafe/jev-latest` at ~$0.00004/judgment and
0.4s latency — but agrees with Jev on only 50% of a 24-run stratified
sample, and the disagreements are informative (Jev-lenient thin artifacts;
one decider hallucinated pass on an 11-char artifact). Embedding comparison
spike: `pplx-embed-v2-late-0.6b` MaxSim separates tasks (55.8 vs 40.0) but
mean-pooling collapses to one cluster; `embeddinggemma` via Ollama stays the
cheap baseline.

Scope: `orchestral/judge.py`, `orchestral/openrouter.py`, `harness.py`
(judge flags, compare/report surfaces), new `orchestral/clusters.py`,
`models/` judge entries, `tests/`.

## Problem Frame

Today every verdict and every cluster is one model's opinion with no
comparison axis:

- `judge_artifact` writes a single `report.judge`; `report.judges[slug]`
  exists but only fills when someone manually re-judges with `--judge`.
- The embedding prototype (`/tmp/embed-proto.py`) is scratch code outside
  the repo, single-model, with no provenance marking.
- A single judge can't flag its own failure modes. The #84 trailing-JSON
  bug was a judge-output parse failure; model disagreement is the cheapest
  detector for judge-model failure classes (leniency, hallucinated passes,
  truncation blind spots).

The plan adds two small registries and marks every output with its model —
nothing else changes about the harness.

## Requirements

- R1. A run/experiment can carry **multiple judge slugs**; each produces a
  verdict stored under `report.judges[<slug>]` with model, noul, score,
  engine, cost, and latency. `report.judge` remains the primary verdict
  (first configured judge) — downstream consumers are unchanged.
- R2. A `harness.py compare --judges` (or report flag) surfaces per-pair
  verdict agreement, mean noul delta, and the list of runs where judges
  diverge (the audit surface for judge failure modes).
- R3. Embedding outputs are cached and marked by model id
  (`embeddings-<model>.json` or equivalent keyed cache); a `cluster`/`embed`
  analysis command accepts `--model` from a registry.
- R4. Every model in a registry is **marked in output**: judge verdicts
  carry `model:` slug (already the verdict shape); cluster reports print
  the model id in the header.
- R5. Total marginal cost of the extra judges must stay under a declared
  per-run cap (target < $0.001/run — decider at ~$0.00004 makes this
  trivial; a chat-engine third judge is the expensive axis and must be
  opt-in).
- R6. New surface stays analysis-side: judge verdicts feed reports;
  embeddings feed clustering/dedup reports. Neither enters worker or
  orchestrator prompts (telemetry-separation rule).

## Key Technical Decisions

- **Judge registry is a list, not a config schema change.** `--judge`
  accepts a repeat flag or comma list; `config.resolve_judge` already
  resolves ad-hoc decisions slugs via `is_decisions_model`
  (`~`/`typesafe/` prefix or `metadata.engine: decisions`).
  `perplexity/pplx-decider-v1.1-27b` needs `engine: decisions` in metadata —
  either a `models/pplx-decider.yaml` entry or extending
  `is_decisions_model` to accept `perplexity/` + `decider` slugs.
  Prefer the yaml entry: keeps the `~`-prefix hack honest.
- **Third judge axis is a chat engine, opt-in.** Two decisions engines
  (`~typesafe/jev-latest`, `perplexity/pplx-decider-v1.1-27b`) are live and
  verified on OpenRouter's Decisions API today (Strands not routable). A
  cheap chat judge (e.g. `z-ai/glm-5.3-flash`, `engine: chat` path already
  exists) adds a third voice but at generated-token cost — default judges
  list = the two decision engines.
- **Embedding registry is analysis-only, dependency-light.** Tier 1
  (default): `ollama/embeddinggemma`, `ollama/bge-m3` — both already pulled,
  httpx-only, zero new deps. Tier 2 (optional extras): local
  `pplx-embed-v2-late-0.6b` behind a `sentence-transformers` optional extra
  (MaxSim multi-vector path — not mean-pool, spike proved pooling
  collapses), and hosted `pplx-embed-v1-4b` behind `PPLX_API_KEY`. Tier-2
  entries degrade to "skipped: <reason>" when their dependency/key is
  absent — tests never fail on missing optional models.
- **New module `orchestral/clusters.py`** owns the clustering math
  (dependency-free greedy cosine for single-vector models; MaxSim
  similarity only when the optional extra is present). The prototype's
  cache format (sha1-of-text → vector) carries a model dimension.

## Implementation Units

### U1 — Multi-judge execution + provenance

Files: `harness.py`, `orchestral/judge.py`, `orchestral/config.py`,
`models/pplx-decider.yaml` (new), `tests/test_judge_decisions.py`.

- `--judge` accepts multiple slugs (repeat or comma); `_judge_from_arg`
  returns a list; launch and experiment paths judge each finished run with
  every slug, writing `report.judges[slug]`.
- `models/pplx-decider.yaml`: `slug: perplexity/pplx-decider-v1.1-27b`,
  `metadata.engine: decisions`, `provider: openrouter`, pricing
  $0.02/$0.00.
- Primary verdict stays `report.judge` = first judge's result;
  `judge_passed`/`judge_score` index columns unchanged.
- Per-judge cost feeds the run's cost ledger (phase `judge`,
  model-attributed — already the shape).
- Tests: fake decisions client returns distinct nouls per slug; assert
  `report.judges` has both, `report.judge` equals primary, costs summed.

### U2 — Judge agreement surface

Files: `harness.py` (compare/report flag), `orchestral/report.py` or
wherever compare lives, `tests/test_compare_judges.py`.

- `harness.py compare --judges` (name TBD at implementation; follow the
  existing compare/report structure): for every run with ≥2 judge
  verdicts, emit per-pair verdict agreement %, mean |noul| delta, mean
  score delta, and the diverging run list (run_id, task, both verdicts).
- Output is deterministic text/JSON for evidence reuse in the A/B writeup.
- Tests: fixture reports with two judges agreeing/diverging; assert the
  agreement math and the divergence list order.

### U3 — Embedding registry + cluster command

Files: new `orchestral/clusters.py`, `harness.py` (`cluster` subcommand),
`tests/test_clusters.py`.

- Model registry: `{"ollama/embeddinggemma": ollama_embed,
  "ollama/bge-m3": ollama_embed, "pplx/v2-late-0.6b": st_multi_embed,
  "pplx/embed-v1-4b": pplx_api_embed}` — each entry knows its provider +
  output shape (single-vector vs multi-vector). Registry keys are
  CLI-facing ids that map to provider model ids: `ollama/<tag>` is the
  Ollama model tag, `pplx/v2-late-0.6b` resolves to HF
  `perplexity/pplx-embed-v2-late-0.6b`, and `pplx/embed-v1-4b` is the
  Perplexity API model id. Reports and cache payloads carry the registry
  key verbatim — one name everywhere.
- `harness.py cluster --model <id> [--plans|--artifacts]` runs the
  prototype's extraction (plan reasoning+subtasks; artifact text) →
  per-model cache → greedy clusters → printed cluster table.
- Cache path carries the model id (`runs/_embeddings/<model>.jsonl`);
  entries are sha1-keyed so re-runs are incremental.
- Optional models report `skipped: needs sentence-transformers` /
  `skipped: needs PPLX_API_KEY` rather than failing.
- Tests: fake-vector embedder fixture (deterministic vectors), assert
  cluster output is stable, cache keys include model id, missing-dep path
  reports skipped not crash.

### U4 — Cross-model comparison readout

Files: `orchestral/clusters.py`, `harness.py`, `tests/test_clusters.py`.

- `harness.py cluster --compare a,b,c` runs each model over the same
  corpus and prints: cluster count at fixed thresholds, same-task vs
  diff-task similarity means, and the top divergent pairs.
- This is the evidence table for "which embed model earns its slot" —
  parked spike output becomes a repeatable report.
- Tests: three fake embedders with planted separation; assert the compare
  table ranks them correctly.

## Test Scenarios

- U1: two judges on one run → `report.judges` has both keys; one judge
  raising mid-run doesn't lose the other's verdict; index columns still
  reflect the primary.
- U2: agreement % is right on a fixture pair set; divergent list shows
  run_ids sorted by |noul delta|; runs with one judge are excluded, not
  counted as disagreements.
- U3: deterministic vectors cluster identically on re-run; a second model
  id produces a second cache file; missing optional dep → skip message.
- U4: planted-separation fixtures rank models in the expected order;
  `--compare` with a single model errors cleanly.

## Risks & Open Questions

- **Decider hallucination on thin artifacts** (spike: 11-char artifact →
  noul 0.86). U2's divergence list is exactly the audit tool for this —
  flag, don't auto-trust. Consider a minimum-artifact-length guard before
  calling any judge; that's existing-code behavior worth checking in U1.
- **`perplexity/pplx-decider-v1.1-27b` needs `engine: decisions` metadata**
  or it routes to the chat path. The yaml entry solves it;
  `is_decisions_model` fallback for `pplx-*decider*` slugs is a nice-to-have.
- **PPLX_API_KEY availability** — hosted embed tier is optional precisely
  because the key may not exist yet; do not block U3/U4 on it.
- **Judge cost on big matrices**: two decision judges ≈ $0.0001/run —
  safe; the opt-in chat judge is the only spend-relevant axis and stays
  off by default.
- **Strands decider unroutable on OpenRouter** as of 2026-10-07; revisit
  if Amazon exposes it — a third *decisions-engine* voice beats a chat
  judge if it ever appears.

## Verification

- `python -m unittest tests.test_judge_decisions tests.test_compare_judges
  tests.test_clusters`
- Full suite + ruff + mypy on changed files.
- One real re-judge pass over a 20-run sample with both decision judges;
  confirm `report.judges` populates and `--judges` compare output matches
  the spike's 50%-agreement ballpark.
- `cluster --model ollama/embeddinggemma --plans` on the live store
  reproduces the dialect-cluster finding from the prototype.

## Cost

U1/U2 re-judge pass on ~700 stored runs: ~$0.06 worst case. Ongoing
per-run marginal: ~$0.0001 (two decision engines). Embedding tests: $0
(local models); hosted tier only if PPLX_API_KEY materializes.
