---
title: Judge resampling — k-sample verdicts with honest aggregation
created: 2026-10-12
status: draft
plan_kind: feature
---

# Judge resampling — k-sample verdicts with honest aggregation

## Problem frame

Every stored verdict in `runs/index.db` is one draw from a stochastic
judge. The literature is now unambiguous about what that costs us:

- Snyk VulnBench (arXiv 2606.15762): 80 of 161 unique agentic findings
  appeared in only 1 of 5 identical scans; 22 appeared in all 5.
- Stanford determinism study (arXiv 2502.20747): temp-0, context-cleared
  code reviews still varied run to run.
- EdgeLog's 400-call study: a single review pass found 34% of real
  defects; repeats took coverage 34%→61%→76%; and the most-repeated
  findings were false positives — **repeat count is not a truth signal**.
- Large Language Monkeys (arXiv 2407.21787): coverage grows log-linearly
  with samples; majority voting / reward models plateau early and never
  reach the coverage ceiling.

So: a single judge verdict is a biased, noisy sample. `noul` already
returns a probability — we just never resample around the uncertain
region. Worse, `judge_cache_key` pins one sampled verdict to a content
sha forever: **a cached wrong verdict is permanent**, and it is
indistinguishable from a cached right one.

The goal is not "judge more." It is **verdict honesty**: measure how
split the judge actually is, mark split cells inconclusive (the
honest-outcomes taxonomy already has that bucket), and spend extra
samples only where the judge is uncertain.

## Requirements

- **R1 — k independent verdicts per judgment point.** A `--judge-samples
  N` flag (default 1 = today's behavior) makes the primary judge draw N
  verdicts for the same artifact. Each sample is a full
  `Provider.decide`/chat call — independently metered in `calls`, no
  free-riding. Applies to `judge_artifact` and `judge_pair`; second-opinion
  judges (`report.judges[slug]`) stay at 1 sample each unless the flag is
  later extended (explicit non-goal for this change).

- **R2 — resampling must defeat the cache.** `judge_cache_key(spec,
  payload)` maps content→one verdict. With N>1, identical payloads would
  replay the same cached verdict N times — the single most important bug
  this change could ship. Sample i's cache key gets `f"{sha}:{i}"`; sample
  0 keeps the bare sha so existing caches stay valid.

- **R3 — honest aggregation, not majority vote.** Aggregate N verdicts
  into a judgment result with an added `samples` list (per-sample
  noul/score/passed/confidence) plus:
  - `pass_fraction` — fraction of definitive samples with `passed`
    (inconclusive samples excluded from the denominator, counted
    separately in `inconclusive_samples`; **`null` when no definitive
    samples exist** — never a divide-by-zero guess)
  - `verdict` — passed iff **all** definitive samples passed; failed iff
    all failed; **inconclusive when samples split or none definitive**
  - `judge_split: true` on any mixed or fully-inconclusive set
  Majority vote is explicitly rejected: the EdgeLog finding is that
  repeated findings include repeated *false* findings. Split →
  inconclusive is the honest outcome, and it feeds the existing
  inconclusive pipeline (never a fabricated verdict — KTD7's rule).

- **R4 — adaptive sampling band.** Sampling cost is linear in N; the
  accuracy value concentrates at the uncertain region. When N>1 the
  runner first draws sample 0; if its noul falls **inside** the
  uncertainty band `[0.3, 0.7]` (defaults, `--judge-band lo hi`
  overridable) or is null, remaining samples are drawn; a confident
  single verdict (noul outside the band) short-circuits. Chat judges have no noul — they always
  draw all N. This keeps the common case at 1 call and pays only where
  verdicts can flip.

- **R5 — spend metering stays true.** `_check_judge_cost_cap` scales its
  worst-case estimate by N for the primary judge. Per-sample costs land
  in `calls` individually, so `group_spend`/`spend_today` keep working
  with no changes.

- **R6 — split-rate visibility.** `judge_agreement` (orchestral/judge.py)
  gains a same-model variant: per (task, judge) report `split_rate`,
  mean `pass_fraction`, and sample count — feeding the writeup's
  honesty section with measured verdict noise, not an assumption.

## Non-goals

- Resampling second-opinion judges (panel × samples matrix) — combinatorial
  spend; panel diversity already decorrelates across providers.
- Aggregating by score-weighting or reward models — the literature shows
  selection plateaus; inconclusive-on-split is the honest endpoint.
- Wisp-side confidence gating (low-confidence route → clarify) — same
  principle, different repo; tracked as follow-up, not in this change.
- Backfilling existing verdicts — historical single samples stay as
  recorded; `samples: []`/`judge_split` absent marks them legible.

## Implementation units

### U1 — sampling + aggregation in `orchestral/judge.py`

Files: `orchestral/judge.py`, `tests/test_judge_sampling.py` (new)

- `aggregate_samples(samples: list[dict]) -> dict` — pure function
  implementing R3 (pass_fraction, verdict, judge_split, per-sample list,
  mean noul preserved as `noul` for downstream compatibility).
- `judge_artifact` and `judge_pair` gain `samples: int = 1` and
  `band: tuple[float, float] = (0.3, 0.7)` kwargs; the decisions path
  loops `_judge_via_decisions` per R4 (draw 1, band-check, draw rest).
  Chat-judge path loops the chat call identically; force sampling
  temperature ≥ the model's default for diversity (temp-0 samples are
  near-identical draws — determinism-study caveat, worth a code comment).
- `judge_cache_key` gains a `sample_idx` param; idx 0 → bare sha (R2).

Test scenarios:
- aggregate: all-pass → passed; all-fail → failed; 2-of-3 split →
  inconclusive + judge_split + pass_fraction ≈ 0.67; all-null →
  inconclusive; inconclusive samples excluded from pass_fraction
  denominator but counted in `inconclusive_samples`.
- cache key: idx 0 unchanged vs existing key (backwards-compat assert),
  idx 1 ≠ idx 0 ≠ idx 2.
- adaptive band: mocked `decide` returning noul 0.95 → exactly 1 call;
  noul 0.5 → exactly N calls; noul None → N calls.
- chat judge: N calls regardless of first verdict.
- aggregation output preserves `noul`/`confidence`/`engine`/`model`
  fields existing consumers read (regression assert on keys).

### U2 — runner plumbing

Files: `orchestral/runner.py`, `harness.py`,
`tests/test_judge_sampling.py` (extend)

- `--judge-samples` (int, default 1) and `--judge-band` (two floats)
  CLI flags → `RunnerConfig`; `run()` passes them into
  `judge_artifact`/`judge_pair`.
- `report.judge` records the aggregated result incl. `samples` and
  `judge_split`. `report.passes`/`score` stay **mechanical-only** —
  the judge does not mutate them (runner.py:~1186 contract); a split
  verdict surfaces as `report.judge.verdict: "inconclusive"` +
  `judge_split`, flowing through the existing inconclusive path.
- `_check_judge_cost_cap` worst-case estimate multiplies the primary
  judge's unit cost by `--judge-samples`.
- `runs.config` JSON records `judge_samples` + `judge_band` for
  provenance (the experiment ledger reads config, not schema columns).

Test scenarios:
- flag plumbing: `--judge-samples 3` reaches judge call (mock provider
  counts calls); default 1 = unchanged behavior (regression).
- cost cap: N=3 × cheap judge estimate under cap → passes; over cap →
  refuses with the existing error text.
- report shape: `report.judge.samples` length matches calls made;
  `report.judge.verdict` is aggregate not sample-0 echo (split fixture).

### U3 — split-rate reporting

Files: `orchestral/judge.py` (`judge_agreement` or sibling fn),
`orchestral/report.py` or the report path that renders judge panels,
`tests/test_judge_sampling.py` (extend)

- `same_judge_split_rate(rows) -> dict` — group sampled verdicts by
  (task, judge slug): split_rate, mean pass_fraction, n_samples. Reads
  `report.judge.samples` where present; runs without `samples`
  metadata are **excluded from the denominator** (their outcome is
  unknown, not a non-split) and reported as `unsampled_runs: n`.
- Surface in `harness.py report --judge-agreement` output (extend the
  existing panel rather than a new flag).

Test scenarios:
- 3 sampled runs, one split → split_rate 1/3.
- unsampled runs (no `samples` key) excluded from the rate denominator,
  surfaced as `unsampled_runs` — no fabricated non-splits from absent
  data.
- per-judge breakdown isolates slugs (jev vs pplx-decider rows don't
  merge).

## Decisions

1. **Split → inconclusive, never majority vote.** The measured evidence
   (EdgeLog, VulnBench) is that repetition does not certify truth —
   false findings repeat. Our taxonomy already owns inconclusive; a
   split verdict *is* inconclusive evidence.
2. **Adaptive band on noul, not unconditional k.** LLM Monkeys shows
   coverage scales with samples, but spend scales identically. The
   band confines the multiplier to the region where a verdict can
   actually flip; confident regions stay at 1 call.
3. **Same-model resampling, not panel expansion.** Panels decorrelate
   across vendors; resampling measures this judge's own noise — which
   is the unmeasured variable in every stored verdict today.
4. **Cache-key suffix, not cache bypass.** Keeping sample 0 on the bare
   sha preserves the existing cache (no backfill, no double-spend on
   reruns); suffixed keys are honest — two identical payloads *should*
   share a cache only when asking for the same sample index.

## Risks

- **Borderline-band games.** A judge whose nouls cluster at 0.49/0.51
  pays 3× per call and still splits — that is the *correct* cost, the
  band must not be widened to hide it. Watch `split_rate` per task.
- **Correlated samples.** Same model, same payload — samples correlate;
  pass_fraction is not a calibrated probability of correctness. The
  report must carry it as "agreement," never "confidence of truth."
- **Chat-judge near-determinism.** Temp-0 chat judges may return
  identical verdicts and look "unanimous" while being one sampled
  opinion — U1 forces sampling temperature up; still, unanimity at k=3
  is weaker evidence than it looks. Document, don't overclaim.
- **Spend shape.** N=3 with a fat uncertainty band could triple judge
  spend on hard tasks; `--judge-cost-cap` scaling (R5) is the guard,
  and the band defaults are deliberately narrow.

## Measurement

Success is a number, not a feeling: after this lands, a modest
`--judge-samples 3` pass over a slice of existing tasks should produce
a nonzero-but-small `split_rate` (expect 5–20% on borderline task
families per the literature). That measured rate becomes a line in the
writeup — "verdict noise, measured" — instead of an assumed one.
