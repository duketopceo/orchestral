---
title: "Code census — October 2026 baseline"
date: 2026-10-06
---

# Code census — October 2026 baseline

Baseline measurement for the safe code-reduction program
(`docs/plans/2026-10-06-001-refactor-safe-code-reduction-plan.md`, U1).
All numbers from `main` at `a3d1221`, codebase-memory index refreshed
2026-10-06 (`mode=full`, `persistence=true`).

## Size

| Surface | Files | LOC |
|---------|-------|-----|
| `orchestral/` (incl. `web/` 4,706) | ~60 | 30,446 |
| `harness.py` | 1 | 2,821 |
| `scripts/` | ~10 | 1,664 |
| `demo/` | 1 | 391 |
| `tests/` | ~120 | 37,924 |
| `ui/js/` | ~25 | 5,107 |

Python source total ≈ **35,300 LOC**; tests **37,924** (1.07× source — the
suite outweighs the code). `ast` symbol count in `orchestral/` + `harness.py`:
1,104 functions, 92 classes.

## Graph state

- codebase-memory index: **9,201 nodes / 35,626 edges** (was 5,655/20,514 at
  the stale 2026-09-30 index — confirms the "reindex before trusting
  zero-caller results" caveat in the plan).
- Graph covers `ui/js/`, `infra/cloudflare/`, and `tests/` — answers the
  plan's open question: CBM does index JavaScript here.
- 1,313 files excluded by gitignore/skip-lists (fixtures, `runs/`); one
  partial parse (`ui/app.css` lines 993–995) — cosmetic, non-blocking.

## Dead-candidate signal (pre-filter)

Raw count of `Function` nodes with **zero inbound edges**: **94**.

Distribution (top):

| File | Zero-inbound fns | Read |
|------|------------------|------|
| `harness.py` | 25 | Mostly `cmd_*` argparse handlers — dispatch entry points, registry will protect |
| `orchestral/audit.py` | 13 | `check_*` functions — near-certainly name-prefix dispatch; registry must cover |
| `orchestral/holdout.py` | 4 | `_needle_*`/`_sql_spec` spec builders — check call sites |
| `orchestral/cf.py` | 3 | `ingest_client`/`push_run_events_only`/`verify` — check CLI/service wiring |
| tests, ui/js, infra | ~30 | Discovery-run tests, JS registry, worker test files — registry/minus-list |

`orchestral/` zero-inbound names worth noting (26 total): private helpers
with no callers (`_pass_rate_stat`, `_scored_mean`, `_fixture_authorizer`,
`_download`, `_is_binary`), a public-looking `codeexec.summarize_unittest_output`,
and the 13 `audit.check_*` dispatch functions. The audit cluster alone
demonstrates why U2 (entry-point registry) precedes any deletion: naive
zero-inbound would flag the whole audit check suite.

## Interpretation

- The honest dead pool is small relative to code size — expect the report
  (U3) to land tens of candidates, not hundreds. The bigger prize is likely
  Tier-B/C simplification-adjacent code, deferred by plan scope.
- `state.py` (3,349 LOC) has only 1 zero-inbound function — its size is
  breadth, not dead weight. Sweep sizing should follow the report, not LOC.
- Test volume (38k LOC) means test-helper dead code is plausible; R5 rules
  apply — delete test code only with the dead code it covered.

## Reproduce

Index: `index_repository` on repo root, `mode=full`, `persistence=true`.
Zero-inbound query: `MATCH (n:Function) WHERE n.in_degree = 0 RETURN
n.file_path, count(n) ORDER BY count(n) DESC` via `query_graph`. LOC: `wc -l` per dir.
Next census after the U4 sweeps complete; the delta is the program score.
