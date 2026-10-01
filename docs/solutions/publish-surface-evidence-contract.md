# Publish surfaces must share the display surface's evidence basis

Learned in the X-publish rebuild (2026-09-30, `thread_context` / `draft_thread`
review). Three findings in one review round were the same failure shape: a
number or claim computed on a *different* population than the leaderboard the
reader compares it against. Any surface that emits claims destined for outside
the observatory — X threads, card exports, future publish channels — must derive
its framing from the exact population and ordering the in-product display shows.

## Rank denominators must match what the board numbers

The leaderboard renders only *eligible* rows — finished, non-`low_sample`
pairings ordered by `_pairing_quality_key` — and denominates rank as
`lens.ranking.length`. The first `thread_context` ranked the full sorted board
including low-sample tails, so a published "4/4 pairings" contradicted a board
that showed the subject "—" (unranked) among 3 eligible rows.

The contract: `thread_context` ranks over the same eligibility predicate and
ordering as `lens.ranking`, and a low-sample subject reports `low_sample`
(honest "not ranked — thin sample") instead of a rank the board withholds.
Holdout-only pairings stay excluded entirely — unpublished arms do not belong
in public claims, even as neighbors.

## "Across N runs" and "$X" must share one basis

Card `cost_usd` sums finished runs only; `cost_per_pass` (and the cheapest-peer
crown) is computed on all runs including failed ones. A pairing whose failed
runs burned money posted "Economics: $0.0000 across 3 runs" — a false zero-spend
claim. The fix exposes the subject's `cost_total`/`runs` (all runs) through
`thread_context` so the dollars and the count cover the same population. Never
mix a finished-run dollar figure with an all-runs count, or vice versa.

## Derived fields reach the model; model-authored text never does

`draft_thread` scrubs not just transcripts/artifact previews but every
*model-authored* string on the card — `judge_reasoning`, `description`,
`description_by`, `description_model`. An evaluated orchestrator's plan.json
summary or a judge's reasoning is untrusted data and a cross-model injection
path into posts a human ships. The boundary is: templated/derived numbers and
labels reach the writer prompt; anything a model wrote does not.
