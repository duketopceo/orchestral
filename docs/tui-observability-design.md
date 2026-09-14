# TUI observability & experiment archive — design

Orchestral benchmarks orchestrator×worker model pairings. The TUI is a thin
live-observability layer over the harness: the harness produces structured
events and durable records; the TUI consumes them. It never re-implements
orchestration and never parses stdout.

## User flows

1. **Watch a live run** — pairing, task, phase, worker states, event timeline,
   running cost/token totals, elapsed. Cancel safely.
2. **Browse history** — filter/sort completed runs; open one for detail.
3. **Audit a result** — manifest, full event timeline, per-role usage,
   artifacts, evaluation evidence, failure context.
4. **Compare pairings** — leaderboard aggregates with sample-size disclosure.

Default view: Live Run when a run is active, else Run History.

## Event schema (v2)

`events.jsonl` remains the per-run semantic stream; `debug.jsonl` stays
operational and unpublished. Every event gains:

- `sequence` — monotonically increasing int per run (timestamp order alone is
  insufficient under concurrency).
- `run_id` — the run this event belongs to.
- `worker_id` — `worker-N` for worker-scoped events, else null.

`type` carries a stable lifecycle vocabulary (minimum set):

```
run.created        task.loaded        run.started
orchestrator.started   orchestrator.completed
delegation.created
worker.started     worker.progress    worker.completed    worker.failed
synthesis.started  synthesis.completed
evaluation.started evaluation.completed
usage.recorded     artifact.saved
run.completed      run.failed         run.cancelled
```

Detail events (`llm_call`, `worker_error`, `worker_retry`, `judge_*`,
`screenshot_skipped`) coexist — they carry call-level payload; lifecycle
events carry phase transitions. `schema_version` becomes `"2"`.

## Archive schema

Existing layout is kept (`runs/{orch}/{task}/{worker}/{run_id}/` +
`runs/index.db`); the spec's `.orchestral/` suggestion is superseded by the
repo standard in AGENTS.md.

New per-run file: **`manifest.json`** — immutable run identity, written at
start (status `running`), finalized at end (status + `finished_at`):

```
run_id, started_at, finished_at, status
task_id, task_version, task_hash            # sha256 of task spec content
orchestrator_model, worker_model, judge_model     # exact API IDs
orchestrator_provider, worker_provider, judge_provider
harness_version, git_commit, python_version
orchestrator_prompt_hash, worker_prompt_hash, config_hash   # sha256
concurrency, timeout_seconds, retry_policy
```

Status vocabulary: `running | passed | failed | cancelled` (harness has no
queue; `timed_out`/`invalid` are failure categories on `failure_reason`).

SQLite (`index.db`) stays the queryable source of truth: `runs`, `calls`,
`judge_cache`. Large payloads live in artifact files, not table rows.

## Run state machine

```
created -> running -> passed
                   -> failed      (validation, judge, provider, parse, ...)
                   -> cancelled   (user request between subtasks)
```

No scattered booleans: `runs.status` + `runs.passes` + `runs.failure_reason`.

## Screen map (lands with the TUI views PR)

- **Live Run** (`1`): manifest header, worker state chips, event timeline
  (tailed from events.jsonl), selected-event detail, cost/token/elapsed bar,
  `c` cancel.
- **History** (`2`): run table with `/` filter, `r` refresh, Enter → detail.
- **Leaderboard** (`3`): pairing aggregates, sort by cost-per-pass /
  pass-rate / median score / median cost / latency, low-sample warning.
- **Run Detail** (Enter): manifest, events, calls, metrics, report, plan,
  artifact paths.
- `e` exports selection; `?` help; `q` quits; Esc backs out.

## Artifact retention

Keep manifests, events, metrics, and reports indefinitely (small). Raw worker
output and artifacts are kept — storage is not yet material. `scrub` remains
the publish path; `manifest.json` is publishable (hashes only, no secrets).

## Ranking methodology

Pairing leaderboard aggregates by `(orchestrator, worker)`:

- `n` runs, `finished`, `tasks_covered` (distinct task ids)
- pass rate (failures count against it), median score, median cost,
  median duration, failure rate
- **cost per pass** = total cost / passed runs — primary ranking
- `low_sample` flag when `n < 10` — never presented as "best"

Invalid runs (never finished) are excluded from medians but counted in n and
failure rate.
