# Plan: N-runs per pairing — replicates, variance, error bars

Piece 2 of the benchmarks work. Builds on observability v2 labels
(`run_group`, `replicate`, `seed`, `latency_ms`, `failure_reason`).

## Goal

A single CLI invocation can run a pairing N times under one `run_group`,
and `report` can aggregate those replicates into mean/stddev/percentile
stats so a pairing comparison is backed by variance, not single runs.

## Design

### `orchestral/stats.py` (new, pure)

- `mean`, `stdev` (sample, `statistics.stdev`, 0.0 for n<2), `percentile`
  (linear interpolation over sorted values).
- `CellAggregate` dataclass: `run_group`, `task_id`, `orchestrator`,
  `worker`, `runs`, `finished`, `passed`, `pass_rate` (passed/runs —
  failures count against), `score_mean`, `score_sd` (over scored runs
  only), `cost_mean`, `cost_sd`, `cost_total`, `latency_p50`,
  `latency_p95`, `tokens_mean`, `successes_per_dollar`
  (passed / cost_total), `failures` (failure_reason -> count).
- `aggregate(runs, by_group=True)` -> `list[CellAggregate]` keyed on
  `(run_group, task_id, orchestrator, worker)`; `by_group=False` drops
  the group key for cross-group pairing rollup.

### `harness.py`

- `--replicates N` in `_add_run_flags` (run, grid, batch, ablate).
- Validation in `_run_preamble`: `N >= 1`; mutually exclusive with
  `--replicate`.
- `_resolve_replicates(args)` -> `(group, n)`: auto-names
  `rep-YYYYMMDD-HHMMSS` when `N > 1` and `--group` is absent. An explicit
  `--group` without `--replicates` still labels runs as today.
- Replicate `i` in 1..N gets `replicate=i`; when `--seed S` is set the
  replicate's seed is `S + i - 1` (documented: varying seeds measure
  variance; identical replicates need N manual runs).
- run: sequential loop, per-run line, then a mean±sd summary line.
- grid/batch/ablate: work items expand to `item × replicates`; the
  existing `--jobs` pool handles them unchanged.
- `report --groups`: new table mode — one row per
  (group, task, orch, worker) cell with n, pass%, score±sd, cost±sd,
  p50/p95 latency, successes/$. `--group` filter + fields added to
  `report --json` rows.

### `orchestral/storage.py`

- `list_runs(..., run_group=None)` filter.

### `orchestral/reporter.py`

- "Replicate groups" table on the HTML report index, only when any run
  has `run_group` set. Text `mean ± sd` cells (no JS).

## Verification

- Unit tests: percentile/stdev edges, aggregation math, failure counts,
  `by_group` rollup.
- `cmd_run --replicates 3 --dry-run` produces 3 runs, shared group,
  replicates 1-3, seeds S..S+2.
- `report --groups` prints the aggregate; `--json` includes it.
- Full suite + ruff + mypy + compileall; AGENTS.md smoke.
