# Experiments

Committed experiment **specs** — matrices only, never run output (runs land
in `runs/` as always). Each file is one A/B experiment: every
`(task, orchestrator, worker)` cell runs paired replicates on the
`baseline` and `jev` (`--jev-assist`) arms.

## File format

```yaml
name: my-experiment            # run_group prefix — resumes key on it
orchestrators: [slug, ...]
workers: [slug, ...]
tasks: [task-id, ...]          # ids inside tasks/ — paths are rejected
```

## Running

```bash
python harness.py experiment --matrix experiments/jev-ab.yaml --dry-run   # priced plan
python harness.py experiment --matrix experiments/jev-ab.yaml \
    --budget 12.00 --daily-cap 15.00 --seed 7 --jobs 2
python harness.py coverage --matrix experiments/jev-ab.yaml               # done/pending ledger
```

Repetition count is **cost-scaled**, not fixed: `ceil(cell_budget /
estimated_pair_cost)` clamped to [5, 100]. Runs execute in batches of
`--batch-size` replicate indexes (each index = both arms, order
alternating); between batches the driver checks live spend (calls-table
meter), batch infra-error rate (>50% → cell `aborted`, persisted), and the
arm-difference 95% CI (half-width ≤ `--diff-eps` → cell `done` early).
Interrupt and re-run the same command to resume — `done`/`aborted` cells
are skipped.

The judge is constant across arms (the decisions engine) — judge-score
deltas between arms are self-referential and labeled as such; mechanical
pass is the primary axis.
