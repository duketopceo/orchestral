# Plan: `harness.py serve` — local web observatory

- **Type:** feat
- **Depth:** Standard
- **Origin:** `docs/plans/2026-09-16-0214-serve-web-gui-plan.md` (requirements; brainstorm-confirmed)

## Summary

Add `harness.py serve`: a localhost-only web UI over the existing run
index — mission-control overview, live-run tail, run history, run detail,
leaderboard, and a new-run form with launch/cancel. Stdlib only
(`http.server`, server-rendered HTML, ~1s fetch polling). No new
dependencies; sits beside the TUI and the static `dashboard` export.

## Problem frame

The TUI gives full observability but requires a terminal and a running
`textual` session; `dashboard` is a static snapshot. The operator wants
the same three jobs — watch a live run, audit a finished run, compare
pairings — in a browser, plus the ability to start and stop runs from
that browser.

## Requirements (from origin)

- R1. Overview home: live-run banner, leaderboard summary, recent runs.
- R2. Live-run view: event tail, phase, per-worker chips, running
  cost/tokens/elapsed; near-live (~1s) updates via polling.
- R3. Run history table with filtering; running rows link to live view.
- R4. Run detail: summary + inspection tabs (events, calls, metrics,
  plan, manifest, report).
- R5. Leaderboard view: `pairing_leaderboard` rollup, sortable.
- R6. New-run form → launches a run on a background thread.
- R7. Cancel stops serve-launched runs via the runner's `cancel_event`.
- R8. Localhost binding only; no auth; no secrets in responses.
- R9. Malformed/missing run files degrade gracefully (never a 500).
- R10. Zero new third-party dependencies.

## Key technical decisions

- **KTD1 — stdlib `http.server.ThreadingHTTPServer`, no framework.**
  The stack rule (pyyaml/httpx/rich only) holds; per-request work is
  small (index reads + file tails), so stdlib is sufficient.
- **KTD2 — reuse `orchestral.tui.state` in place.** Verified pure (no
  textual imports) and `orchestral/tui/__init__.py` lazy-imports
  `textual` only inside `run_tui`, so `from orchestral.tui.state import
  …` is safe without the extra. No code moves; if the shared surface
  grows web-specific later, lift it then.
- **KTD3 — server-rendered HTML + inline JS fetch polling.** A ~20-line
  poll loop on live pages; no SSE/websockets, no build step, no static
  assets (single inline `<style>`/`<script>` per page, dashboard-style).
- **KTD4 — JSON poll endpoints under `/api/`**. Live view polls
  `GET /api/run/<id>/live?after=<seq>` → `{events, next_offset, phase,
  workers, status, cost_usd, elapsed}`; overview polls
  `GET /api/overview` for the live banner. Pages render initial state
  server-side; polling only refreshes the live regions.
- **KTD5 — job registry mirrors the TUI job model.** A `JobRegistry`
  holds `Job` objects (reuse `tui.state.Job`/`JobStatus`) with
  `cancel_event` + `run_ids`; `launch()` runs the same spec → Runner
  loop as `tui/app.py::_execute` minus the Textual calls (~30 lines
  duplicated with difference — accepted over extracting a shared helper
  that would churn the TUI).
- **KTD6 — POST-only mutations, strict field contract.** `POST /run`
  and `POST /run/<id>/cancel` accept only declared form fields;
  `GET` is always read-only.

## Output structure

```
orchestral/web/
  __init__.py    run_server() entry (mirrors tui/__init__.py shape)
  state.py       JobRegistry + web-facing derivation over
                 tui.state/stats/storage — pure, no http imports
  render.py      page + fragment HTML builders — pure string functions
  server.py      ThreadingHTTPServer, handler, route table
tests/test_serve.py
```

## Implementation units

### U1. Pure layer — `orchestral/web/state.py` + `render.py`

**Goal:** all derivation and HTML generation testable without a server.
**Requirements:** R1–R5, R9. **Dependencies:** none.
**Files:** `orchestral/web/state.py`, `orchestral/web/render.py`,
`orchestral/web/__init__.py`, `tests/test_serve.py`.

**Approach:**
- `state.py`: `JobRegistry` (jobs dict, `launch(spec)`,
  `cancel(run_id)`, `snapshot()`), plus thin web-facing wrappers over
  `tui.state.tail_events`/`run_phase`/`worker_states`,
  `stats.pairing_leaderboard`, `RunStore.runs()/calls_for_run()`, and
  run-dir file readers (`report.json`, `metrics.json`, `manifest.json`,
  plan). Every file read is guarded — malformed JSON returns a degraded
  payload, not an exception.
- `render.py`: `page(title, body)` shell + one builder per view
  (`overview`, `live`, `history`, `detail`, `leaderboard`, `new_run`,
  `not_found`). `html.escape` on all run-derived strings. Inline CSS
  block shared across pages; live pages embed the poll script.
- `__init__.py`: `run_server(runs_dir, tasks_dir, models_dir, port)`
  stub that lazy-imports `server` (keeps `orchestral.web` importable
  for tests without binding a socket).

**Patterns to follow:** `tui/state.py` (pure-domain discipline),
`reporter.py::generate_dashboard` (self-contained HTML style),
`tui/app.py::_execute` (launch loop).

**Test scenarios:**
- `tail_events`-backed live payload: seed `events.jsonl` with a partial
  last line → no crash, offset advances past complete lines only.
- `run_phase`/`worker_states` on a seeded event stream → expected phase
  and chip states.
- Detail payload on a run dir missing `report.json` → degraded dict,
  no exception.
- Leaderboard builder on seeded `RunStore` → rows match
  `pairing_leaderboard` output.
- Render functions return `str` containing escaped values (seed a run
  id/task containing `<script>` → `&lt;` in output).
- `JobRegistry.launch` with `dry_run=True` spec → job reaches
  SUCCEEDED, run_id recorded; `cancel` mid-job → CANCELLED and run
  status `cancelled`.

**Verification:** unit tests pass with no HTTP involved; package
imports without `textual` installed.

### U2. HTTP server — `orchestral/web/server.py`

**Goal:** all routes live; launch/cancel wired to the registry.
**Requirements:** R1–R9. **Dependencies:** U1.
**Files:** `orchestral/web/server.py`, `tests/test_serve.py`.

**Approach:**
- `ThreadingHTTPServer` bound to `127.0.0.1` only (reject/ignore any
  host override — no `--host` flag at all).
- Route table: `GET /` overview, `GET /runs`, `GET /run/<id>`,
  `GET /run/<id>/live`, `GET /leaderboard`, `GET /new`,
  `GET /api/overview`, `GET /api/run/<id>/live?after=N`,
  `POST /run`, `POST /run/<id>/cancel`.
- `POST /run` parses form fields (task, orchestrator, worker,
  replicates, seed, dry_run, judge optional) → `JobRegistry.launch` →
  redirect to `/run/<id>/live` when the run id is known, else `/`.
- `POST /run/<id>/cancel` → `registry.cancel` → redirect back.
- Unknown routes → rendered 404 page. `GET` handlers never mutate.
- `run_server()` prints the URL and serves forever (Ctrl-C exits).

**Test scenarios:**
- Seeded `runs/` dir + server on an ephemeral port: every GET route →
  200 + expected content markers.
- `/api/run/<id>/live?after=N` returns only events after offset N and a
  monotonically increasing `next_offset`.
- `POST /run` with a dry-run spec → 302, run completes, appears in
  `GET /runs`.
- `POST /run/<id>/cancel` on a running job → subsequent poll shows
  `cancelled`; cancel on an unknown/finished run → 404 or no-op
  redirect, never 500.
- `GET /run/<missing>` → 404 page.
- Malformed `events.jsonl` mid-run → live endpoint still 200s.
- POST with an unexpected field → rejected (400) or ignored per
  contract — assert whichever the unit implements.

**Verification:** tests pass; `curl` against a live instance matches
expected routes.

### U3. CLI + docs — `harness.py serve`

**Goal:** the command exists and the docs describe it.
**Requirements:** all (delivery seam). **Dependencies:** U2.
**Files:** `harness.py`, `README.md`, `ROADMAP.md`, `AGENTS.md`.

**Approach:**
- `serve` subparser: `--runs-dir/--tasks-dir/--models-dir` (global),
  `--port` (default pick, e.g. 8787), `--open` (stdlib `webbrowser`).
- `cmd_serve` calls `web.run_server(...)`; no framework lazy-import
  needed (stdlib only).
- README commands table + a short "Web GUI" section; ROADMAP: check the
  v1.2 `serve` item; AGENTS.md: add the serve smoke line to the
  verification block if consistent with its style.

**Test scenarios:**
- `harness.py serve --port <ephemeral>` in a thread/subprocess →
  `GET /` returns 200; process terminates cleanly on signal.
- Help text lists `serve`.

**Verification:** `python3 harness.py serve --port 0` (or ephemeral)
serves the overview; docs render.

### U4. Browser smoke — optional Playwright check

**Goal:** prove the pages render in a real browser.
**Requirements:** R1, R2. **Dependencies:** U3.
**Files:** `tests/test_serve_browser.py` (skip-if-missing pattern like
`[shots]` tests).

**Approach:** `pytest.importorskip`-style guard (repo uses unittest —
mirror however `shots` tests skip when playwright is absent); launch
`run_server` on an ephemeral port, assert overview + live view render
with expected text. Single smoke, not a test suite.

**Test scenarios:**
- Overview renders run rows; live view shows phase text while a
  dry-run job is in flight (or assert structure when no job exists).

**Verification:** test passes locally with playwright installed;
skipped without it.

## Scope boundaries

- No auth/TLS/LAN binding; no websockets/SSE; no static assets.
- Cancel covers serve-launched runs only (file-based cancel flag for
  CLI/TUI runs is deferred).
- Does not change runner, storage, or event schemas.

### Deferred to follow-up work

- File-based cancel flag so the web UI (and TUI) can cancel
  CLI-launched runs.
- Lifting shared derivation out of `orchestral/tui/` if a third
  surface appears.

## Risks

- **Concurrent access:** handler threads read `events.jsonl` while the
  runner writes — `tail_events` is already partial-line safe; keep all
  file reads through it/guarded readers.
- **Import hygiene:** `orchestral.web` must never import `textual` —
  covered by a test importing the package and asserting no textual in
  `sys.modules`.
- **Request-scope creep:** strict form contract + GET read-only keeps
  the attack surface minimal on an unauthenticated localhost server.
