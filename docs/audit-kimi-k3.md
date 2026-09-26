# External Logic Audit — Kimi K3 (moonshotai/kimi-k3 via OpenRouter)

- Date: 2026-09-20
- Method: full-source single-shot review of 9 core modules (`orchestral/runner.py`,
  `planners.py`, `openrouter.py`, `judge.py`, `storage.py`, `costs.py`, `extract.py`,
  `calibrate.py`, `harness.py`) by `moonshotai/kimi-k3` (temperature 0), prompted for
  logic errors, race conditions, cost-accounting errors, cache-key collisions, silent
  exception swallowing, determinism violations, and spec-vs-implementation mismatches.
  Findings returned as strict JSON; triaged by hand against the source.
- Spend: ~$1.70 total (36,665 in / 102,000 out tokens across 10 calls; first
  `runner.py` call truncated at 4k output tokens and was re-run at 32k).
- Verification of fixes: `compileall` clean; `pytest tests` 361 passed, 9 skipped;
  repeated dry-run batches produce byte-identical `total_cost_usd`.

## Totals

32 findings: 1 HIGH, 19 MEDIUM, 12 LOW. 26 fixed, 6 discarded with reasons below.

## Fixed

### openrouter.py
- **HIGH** `_post_with_retry` caught only `TimeoutException`/`NetworkError`; transport
  errors like `RemoteProtocolError`, `ProxyError`, `UnsupportedProtocol` escaped
  unwrapped and unretried. Now catches `httpx.TransportError` (retry + `OpenRouterError`).
- **MEDIUM** A transport failure on the `/videos` submit POST raised `OpenRouterError`
  (retryable upstream) → duplicate billable video generation. Submit failures and
  missing-job-id responses now raise `OpenRunnerVideoSubmittedError` (never retried).
- **MEDIUM** `chat()` did `data.get("choices", [{}])[0]` — an HTTP-200 body with
  `"choices": []` raised `IndexError`. Now validated, raising `OpenRouterError`.
- **MEDIUM** Provider `Retry-After` was used unclamped: negative values crash
  `time.sleep`, huge values hang the harness. `_coerce_retry_after` now clamps to
  (0, 60] seconds and treats non-positive as "no header".
- **MEDIUM** `videos()` returned raw `usage` and no `api_cost_usd`, breaking the
  documented "shaped like `images`" contract. `api_cost_usd` added (usage left raw to
  preserve the provider-reported cost object the video tests assert on).
- **MEDIUM** `images()` silently returned `b""` when the response item had neither
  `b64_json` nor `url`. Now raises `OpenRouterError` (matches the videos contract:
  empty media must not trigger a billable resubmission).

### planners.py
- **MEDIUM** Dry-run "deterministic" artifacts used unseeded `random` for token counts
  and latencies, so dry-run costs/logs differed between identical runs. Replaced with
  pure character-derived counts and a fixed `_DRY_RUN_LATENCY_MS = 250.0`. Verified:
  repeated dry-run batches now produce identical total costs.
- **MEDIUM** `plan_raw`/`plan_ce` mutated `plan["task_id"]` without checking the
  `_extract_json` result is a dict — a JSON array from the model raised `TypeError`
  instead of the clean `ValueError` failure path. Guard added in both planners.
- **MEDIUM** `assemble_ce`'s "final verification pass" was dead code: parse result
  discarded inside `contextlib.suppress(Exception)` while still billing the call. The
  verification result is now parsed and a `passed: false` verdict raises
  `ValueError("final verification failed: …")`; unparseable verdicts no longer
  blanket-suppressed.
- **LOW** `_read_prompt_file` cached by path forever — prompt edits during a long
  process were ignored. Cache key now includes file mtime.

### judge.py
- **MEDIUM** `bool(result.get("passed"))` scored the string `"false"` as a pass, and
  `float(result["score"])` ran outside the try, so a malformed score escaped the
  `parse_failed` fallback. Normalization moved inside the try; `passed` now only
  accepts bools or "true"/"false" strings.
- **MEDIUM** Judge saw `artifact[:2000]` with no disclosure. Prompt now appends an
  explicit truncation notice.

### costs.py
- **MEDIUM** `_video_rate` gated the whole `video_pricing` lookup on `resolution` being
  truthy, so wildcard keys (`"*:audio"`, `"*"`) were never probed for unknown
  resolutions — mis-pricing against the documented behavior. Wildcards now probed with
  `res = resolution or "*"`.
- **LOW** `compute_cost` docstring claimed "normalized_token_usage" but returned the
  input unchanged. Docstring corrected.

### extract.py
- **MEDIUM** Grading used `==`, conflating `True == 1` / `False == 0` (inconsistent
  with `_type_ok`, which excludes bool from int). New `_strict_eq` (bool-distinct,
  recursion into containers) used for `expected` grading and enum membership.

### calibrate.py
- **MEDIUM** Explicit `null` in `report.json`'s judge block bypassed the documented
  run-level fallback (`dict.get(k, default)` only fires on missing key). Now
  None-coalesced for `score` and `passed`.
- **LOW** Corrupt `report.json` was indistinguishable from missing — silently
  substituted fallback data into agreement metrics. Now counted in
  `coverage["corrupt"]`.
- **LOW** Docstring claimed judged-but-unlabeled runs appear in `coverage`; they never
  did. Spec corrected to match implementation.

### runner.py
- **MEDIUM** After retry exhaustion, only `out is None` was rejected — an exhausted
  empty dict reached assembly and logged `worker.completed`, skipping the
  `empty_output` failure category. Post-loop check now `if not out:`. (The suggested
  `_subtask_produced_output` check was rejected: sql/extract outputs legitimately carry
  `query`/`extracted`, not `content`, and would have failed correct runs — caught by
  `tests/test_sql_task.py`.)
- **MEDIUM** `ScreenshotUnavailable` was imported inside the `try` block; if the
  `shots` import raised, the `except ScreenshotUnavailable` clause itself raised
  `NameError`. Restructured as `try/except ImportError/pass/else`.

### storage.py
- **LOW** `list_runs()` used `if limit:` — `limit=0` silently returned all rows. Now
  `if limit is not None:`.
- **LOW** `summary()` GROUP BY queries had no ORDER BY, so dict key order (and
  serialized summary output) could vary between processes. `ORDER BY` added.

### harness.py
- **MEDIUM** `--desc` was `store_true` with `default=True` — always descending, the
  flag was a no-op and ascending was unreachable. Now `argparse.BooleanOptionalAction`
  (`--desc/--no-desc`).
- **MEDIUM** Pairing table `qpd = quality/cost if cost > 0 else inf`: a zero-cost,
  zero-quality pairing (all failed) ranked at the top of the quality-per-dollar table.
  0/0 now yields 0.0.
- **LOW** `export --format` was silently ignored on mismatched combinations
  (`--run --format csv` → Markdown; `--format md` without `--run` → CSV). Now exits 2
  with a usage error; a missing `events.jsonl` for `--format jsonl` exits 1 instead of
  silently exporting empty content.

## Discarded (false positives / accepted as-is)

1. **judge.py — "pricing_source discarded from compute_cost"** (MEDIUM): FP.
   `compute_cost` returns `(cost, TokenUsage)`, not a pricing source; the hardcoded
   `"configured"` is correct — cost always comes from the configured rate card, and the
   provider-reported charge is logged separately as `api_cost_usd`.
2. **runner.py — "`results[position]` unguarded in text branches"** (MEDIUM): FP.
   `position` comes from the planner's `assemble_media`, which resolves by `subtask_id`
   and clamps into range before returning; every call site also guards `if results`.
   Out-of-range is unreachable.
3. **runner.py — "judge cache key omits model params/language"** (LOW): partially
   valid but deferred. Judge generation params are hardcoded (temperature 0.2), so no
   param collision exists; the language omission is real but fixing it invalidates the
   entire existing judge cache for a one-byte gain. Accepted risk, noted here.
4. **extract.py — "`extract_json` returns None for JSON `null`"** (LOW): real but
   niche — an artifact whose entire content is the literal `null` is misfiled as
   "unparseable" instead of "not an object". Changing the sentinel would ripple through
   public callers for a negligible taxonomy gain. Accepted as-is.
5. **planners.py — "assemble_media empty-results fallback"** (LOW): unreachable —
   `assemble_media` is only invoked after ≥1 delegated candidate exists; the empty
   list cannot occur on any task type's code path.
6. **openrouter.py — videos `usage` normalization** (partial): the suggested
   `token_usage_from_raw(...).to_dict()` breaks the provider-reported cost object
   (`usage.cost`) the tests and cost reconciliation rely on; fixed instead by adding
   `api_cost_usd` alongside the raw usage.

## Post-fix verification

- `python3 -m compileall orchestral harness.py` — clean.
- `pytest tests` — 361 passed, 9 skipped (4 regressions caught during fix development:
  sql `content`-contract mismatch, judge-cache key change, video usage shape — all
  resolved by narrowing the fixes).
- Dry-run determinism: repeated `harness.py run --dry-run` batches of
  `code-fizzbuzz` and `api-order-lookup` produce identical `total_cost_usd`
  (3.54e-05, 5.55e-05 respectively) where they previously varied run-to-run.
