# Plan: local web GUI (`harness.py serve`)

A localhost web observatory over the existing run index — the third
surface alongside the TUI and the static `dashboard` export. It renders
the same data the TUI derives from `runs/index.db` and the run dirs, and
can launch and cancel runs. Zero new dependencies: stdlib server,
server-rendered pages, fetch-polling for live updates.

## Product contract

**Actor:** the operator running evals on this machine.
**Outcome:** observe and control runs from a browser — monitor a live
run, audit a finished run's evidence, compare pairings — without the
terminal.

**Scope decided in brainstorm:**

- All three observatory jobs are first-class: live monitoring, run
  history/detail, leaderboard comparison.
- Browser can launch runs (task + orchestrator + worker + replicates +
  seed + dry-run) and cancel them.
- Localhost only. No auth. Never binds `0.0.0.0`.
- Cancel covers serve-launched runs only (same limit as the TUI).
- Thin observability layer — the web UI must not become a second
  orchestration engine. It reads the index/run dirs and delegates
  execution to the existing runner.

## Views

Navigation model: mission-control home plus dedicated views (the
brainstorm's A+B mix).

- **Overview (home, `/`):** live-run banner (phase, elapsed, cost,
  worker status chips), leaderboard summary, recent runs. One click to
  any job's depth view. Auto-refreshes while any run is live.
- **Live run (`/run/<id>/live`):** event tail, phase indicator,
  per-worker chips, running cost/tokens/elapsed, selected-event detail.
  Updates by polling `events.jsonl` (`sequence`-offset, partial-line
  safe — the same derivation the TUI uses). Cancel button for
  serve-launched runs.
- **Run history (`/runs`):** the run table with filtering, sorted
  newest-first; running rows link to the live view, finished rows to
  detail.
- **Run detail (`/run/<id>`):** summary header plus the inspection tabs
  the TUI has — events, calls, metrics, plan, manifest, report.
- **Leaderboard (`/leaderboard`):** `pairing_leaderboard()` rollup —
  pass rate, median score/cost/duration, failure rate, cost-per-pass,
  low-n disclosure. Sortable.
- **New run (`/new`):** form — task select (from `tasks/`), orchestrator
  and worker selects (from `models/`), replicates, seed, dry-run toggle.
  Submits to a background runner thread; the new run appears in history
  and its live view becomes followable.

## Behavior requirements

- **Launch:** `POST /run` starts a run on a background thread via the
  existing runner path (dry-run supported). The run dir exists once
  `on_run_created` fires — the same seam the TUI uses.
- **Cancel:** `POST /run/<id>/cancel` sets the run's cancel event.
  Cancelled runs record `status="cancelled"` through the existing
  runner path — no new cancellation semantics.
- **Polling cadence:** near-live, ~1s, like the TUI tail. No websockets,
  no SSE — plain fetch polls. Pages with no live content need no poll.
- **Reads only the existing substrate:** `RunStore` for the index,
  run-dir files (`events.jsonl`, `report.json`, `metrics.json`,
  `manifest.json`) for detail. Malformed/missing files render a
  degraded panel, never a 500.
- **Reuses the pure layer:** state derivation (`tail_events`,
  `run_phase`, `worker_states`, leaderboard sort) stays in
  `orchestral/tui/state.py`-style pure functions — no Textual imports,
  shared between TUI and web if extraction is clean, otherwise mirrored.
- **Safety:** localhost binding only; no secrets in any response (no
  API keys, no env dumps); POST endpoints accept only form fields the
  launch/cancel contract declares.

## Non-goals

- Authentication, TLS, LAN exposure.
- Cancelling runs launched by the CLI or TUI (needs a file-based cancel
  flag — deferred).
- Replacing the TUI or `dashboard` export — all three coexist.
- Streaming transports (SSE/websockets), a JS framework, any new
  dependency.

## Success criteria

- `harness.py serve` prints a localhost URL; opening it shows the
  overview.
- Launch a dry-run from `/new` → run appears in history, live view
  tails its events, finishes with a report.
- Cancel a running serve-launched run → status `cancelled` in index and
  run dir.
- All views work against the real `runs/` archive — history, detail
  tabs, leaderboard populated.
- Everything passes with zero new third-party deps.

## Testing

- Pure derivation/rendering helpers unit-tested without a server.
- HTTP-level tests: `http.server` in a thread against a seeded
  `runs/` dir — each route 200s, detail renders, malformed run files
  don't 500.
- Launch/cancel: POST launches a dry-run job that completes; cancel
  produces `cancelled` status.
- Playwright smoke (optional, `[shots]` extra pattern): overview +
  live view render.

## Risks

- Runner threads + `http.server` — keep each request handler's work
  small; long work stays in the runner thread.
- Reusing TUI state functions: `orchestral/tui/state.py` must stay
  importable without `textual` installed — verify at plan time; if it
  isn't, extract the pure layer rather than duplicating it.
- A browser left open polls a live run's `events.jsonl` — reads must be
  tolerant of concurrent appends (already solved for the TUI tail).
