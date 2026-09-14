# Plan — Observability v2 (benchmark piece 1)

## Goal

Every benchmark item the user approved (N-run variance, failure taxonomy,
pricing drift, per-dollar metrics) is a query over run data. Today's data has
three gaps: errors are unlabeled strings, there is no debug channel, and the
index has no call-level rows or run labels. This piece fixes the data model
before any benchmark lands on top of it.

## Requirements

1. **Failure taxonomy** — every `worker_error` and `run_failed` event carries
   `error_category`; every finished/failed run gets `failure_reason` in
   `run.json` + `runs` table. Categories: `rate_limit`, `auth`, `timeout`,
   `transport`, `provider_error`, `submitted_job`, `malformed_output`,
   `validation`, `empty_output`, `config`, `unknown`.
2. **Debug channel** — `debug.jsonl` beside `events.jsonl`. Low-level records
   (retry decisions, video poll iterations, provider resolution, judge cache
   hits) never pollute the semantic stream. Never logs file contents, media
   bytes, credentials, or polling/download URLs (same constraints as events).
3. **Calls table** — `index.db` gains `calls` (one row per llm_call/worker
   error event: run_id, phase, role, model, tokens, cost, latency_ms, attempt,
   error_category, pricing_source, dry_run). `runs` gains `latency_ms`,
   `failure_reason`, `env`, `run_group`, `replicate` — appended columns only,
   `_row_to_meta` positional reads stay valid; migrate old DBs via PRAGMA check.
4. **metrics.json** — derived per run at close: per phase+role aggregates
   (calls, tokens, cost, latency mean/max), error counts by category, attempts.
   Built by reading events.jsonl back (ledger lacks latency).
5. **Pricing source labels** — each cost record gets `pricing_source`
   (`api_reported` | `configured` | `configured_estimate` | `none`) and
   `api_cost_usd` when the API reports one. Ledger totals unchanged — recording
   only, drift comparison is piece 3.
6. **Env + replicate labels** — `run_start` event + `run.json.config.env`:
   `git_sha`, `python`, `platform`, `orchestral_version`. Runner gains
   `run_group`/`replicate`/`seed` params; CLI flags `--group`, `--replicate`,
   `--seed`, `--verbose`. (N-run loop itself is piece 2.)
7. **Verbose console** — `--verbose` echoes events + debug to stderr so the
   user sees live output (`event_type`, phase, model, latency, cost, error).
8. **Scrub policy** — `metrics.json` added to ALLOWED (derived aggregates,
   scrubbable). `debug.jsonl` omitted by default + manifest omission entry
   (internal diagnostics; conservative like archives). `raw/` subdirs are
   already never copied (`iterdir` files only) — record as omitted if present.

## Units

### U1: taxonomy + debug channel (logger.py, new taxonomy.py)

- `taxonomy.classify_exception(exc) -> str`. Order matters:
  `OpenRouterVideoSubmittedError` → `submitted_job` first (it's a broad parent);
  `HTTPStatusError` by status (429→rate_limit, 401/403→auth, else
  provider_error); `TimeoutException`/`TimeoutError` → timeout;
  `TransportError` → transport; `HTTPError` → provider_error;
  `ValidationError` → validation; `fileset.ParseError`/`JSONDecodeError` →
  malformed_output; `KeyError`/missing-env config errors → config;
  else `unknown`.
- `EventLogger(run_dir, store=None, run_id=None)`. `log_debug(component,
  message, **fields)` → `debug.jsonl` `{ts, seq, component, message, fields}`.
- When `store`+`run_id` given, `log()` also inserts a `calls` row for
  `llm_call` and `worker_error` events (fields from cost/error/metadata).
- `close()` closes both handles; context manager unchanged.

### U2: storage schema (storage.py)

- `CREATE TABLE IF NOT EXISTS calls(...)`; `ALTER TABLE runs ADD COLUMN` for
  `latency_ms REAL`, `failure_reason TEXT`, `env TEXT`, `run_group TEXT`,
  `replicate INTEGER` — guarded by `PRAGMA table_info`, appended at END so
  positional `_row_to_meta` stays correct (new cols are row[14..18]).
- `RunMeta` gains the 5 fields (defaults None/0); `index_meta` writes explicit
  column list; `record_call(**fields)` inserts into `calls`.
- `_row_to_meta` reads new positions defensively (len(row) > idx).

### U3: metrics builder (new metrics.py)

- `build_metrics(events_path) -> dict`: group events by (phase, role):
  calls, in/out tokens, cost, latency mean/max; `error_counts` by category;
  `total_attempts` (sum of `attempt` in worker_* outputs); `schema_version`.
- Runner writes `metrics.json` on success AND failure paths (try/except so a
  metrics bug never fails the run).

### U4: runner + client wiring (runner.py, openrouter.py, planners.py)

- `run_start` output + `config.env` get git_sha/python/platform/version.
- Worker retry/error events: `metadata.error_category`, `log_debug` retry
  decisions; video client gets `debug` callable (duck-typed `_dbg`, no-op when
  unset) logging poll iterations (job_id + status only, no URLs).
- `meta.latency_ms` wall clock; `failure_reason`: `exception:<cat>` on
  exception, `validation` when checks fail, `judge` when judge fails a passing
  artifact, None when passed.
- Cost dicts gain `pricing_source` + `api_cost_usd` where available.
- `_resolve_clients` failures: log to a root-level `runs/debug.jsonl`? No —
  simpler: they still raise before run_dir exists, but now carry
  `error_category=config` in the exception path via harness-level catch? Keep
  minimal: provider resolution errors propagate; `--verbose` shows them.

### U5: scrub + CLI + docs (privacy.py, harness.py, docs)

- `OMIT_NAMES = {"debug.jsonl"}` → manifest omissions; `metrics.json` allowed.
- `harness.py run`: `--verbose` (echo events/debug to stderr), `--group`,
  `--replicate`, `--seed` → Runner kwargs.
- README/ROADMAP/docs: observability section; `docs/publishing.md` notes
  debug.jsonl omission.

## Tests (tests/test_observability.py + edits)

- classify_exception: every category incl. precedence (submitted before
  transport; status-error branches).
- debug.jsonl separate file, correct fields, both handles closed.
- calls rows for llm_call + worker_error (incl. attempt + error_category).
- new runs columns populated; old 14-col DB migrates; _row_to_meta both widths.
- metrics.json aggregates correctly (multi-phase fake events file).
- scrub: debug.jsonl omitted+manifested, metrics.json published.
- pricing_source present on delegate cost records.
- --verbose writes to stderr (capsys).
- env labels in run_start/run.json; --group/--replicate/--seed land in config.

## Risks

- **Positional index breakage** — new columns MUST append; test both schemas.
- **metrics on failed run** — events file must exist before build; wrap in try.
- **debug never leaks** — same sanitization rules as events; no URLs/contents.
- **dry runs** — calls rows still recorded (dry_run=1); store wiring must not
  break the zero-config path (store is optional on EventLogger).
