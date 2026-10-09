---
title: "feat: Dead-code candidate report — tiered kill list (reduction U3)"
type: feat
date: 2026-10-08
---

# feat: Dead-code candidate report — tiered kill list (reduction U3)

## Summary

Implements U3 of `docs/plans/2026-10-06-001-refactor-safe-code-reduction-plan.md`:
`scripts/dead_code_report.py`, a reproducible pipeline that joins three
independent evidence sources (codebase-memory call graph, vulture, coverage)
against the `orchestral/entrypoints.yaml` minus-list, and emits a committed,
deterministic, machine-readable kill list at
`docs/analysis/dead-code-report-2026-10.json`. This unit produces *candidates
with evidence*, not deletions — U4 converts Tier A into batched PRs.

## Problem Frame

The parent plan's U3 spec fixes the goal, files, and tier semantics. What it
left open — and this plan resolves — is how a plain script obtains graph data
(codebase-memory is normally an MCP surface), how freshness is enforced, how
the registry's pattern-kinds map to concrete exclusions, and which census
ambiguities in "zero inbound" must be pinned down so the report is honest
rather than merely plausible.

Verified during planning (these are decisions, not assumptions):

- `.codebase-memory/graph.db.zst` is a zstd-compressed **SQLite** database —
  readable offline via `zstd -dc` + stdlib `sqlite3`. No MCP server needed at
  report time; the index artifact is the input.
- `.codebase-memory/artifact.json` records `commit` and `indexed_at` — the
  freshness assert the parent plan's risk table requires.
- Zero-inbound over inbound `CALLS|USAGE` edges only reproduces the census
  distribution exactly (`harness.py` = 25). Counting `in_degree` (which folds
  `CALL_REFERENCE`/`INHERITS`/`IMPLEMENTS`) yields the census's higher 94 —
  the canonical query is `CALLS|USAGE`, which already absorbs the registry's
  `callback_argument` hazard (argument-position references produce `USAGE`).
- The graph records calls *through* function-scope imports: `cmd_sync`'s
  `from orchestral import cf` produces a real `cmd_sync -> cf.verify` USAGE
  edge, so `cf.verify` is correctly not a candidate. The `lazy_import`
  pattern remains a belt check, not a blind spot.
- Node `properties` JSON carries `is_test`, `is_entry_point`, `is_exported`,
  `signature`, `lines` — free corroborating metadata per candidate.
- `vulture` and `coverage` are not repo deps; `uvx` exists on this machine.
  Both are ad-hoc inputs, keeping `.[dev]` unchanged.

---

## Requirements

Trace to parent plan (`2026-10-06-001`); only deltas or pins are listed.

- R1/R2/R4/R5 unchanged — two-source evidence per deletion candidate,
  registry subtracted before emission, reproducible re-runs, tests never
  flagged on zero-caller evidence alone.
- P1. Canonical candidate query: `Function` and `Method` nodes with zero
  inbound edges of type `CALLS` or `USAGE`, in repo-owned files
  (`orchestral/`, `harness.py`, `scripts/`, `demo/`, `tests/`, `ui/js/`).
  Documented in the script docstring; deliberately excludes
  `CALL_REFERENCE`/`INHERITS`/`IMPLEMENTS` (a reference is not a call).
- P2. Freshness gate: `artifact.json.commit` must equal `git rev-parse HEAD`;
  mismatch fails unless `--allow-stale-index`, which then stamps
  `index_commit` on the report and every row.
- P3. Report byte-determinism: same inputs produce identical bytes — rows
  sorted by `(file, start_line, symbol)`, no wall-clock fields.
- P4. Optional sources degrade, never fabricate: without `--vulture-file` or
  `--coverage-json` the report still emits, but no row may claim evidence
  it did not see, and Tier A is unreachable without a second source.

## Key Technical Decisions

- **Script reads the index artifact, not the MCP server.** `zstd -dc` to a
  temp file (or `zstandard` module if importable — try import, fall back to
  the CLI), then stdlib `sqlite3`. Keeps the report runnable in any
  worktree/CI context where the artifact exists, and keeps `ce-work` agents
  out of MCP wiring.
- **Coverage is an input file, not an orchestrated run.** The script accepts
  `coverage json -o` output via `--coverage-json`. The collection command
  (`coverage run --source=orchestral,harness -m unittest discover -s tests`)
  runs ad-hoc in the gate venv with `coverage` pip-installed — it stays out
  of `[dev]` extras because the committed report is the artifact
  contributors consume, not per-clone tooling.
- **Vulture is an input file too** (`uvx vulture <paths> --min-confidence 60
  > /tmp/vulture.txt`; parse `file:line:` records). The confidence value is
  stored in `evidence[]` so a low-confidence flag reads differently than a
  90% one.
- **Symbol key is file+line containment, not name-matching.** Graph nodes
  give `file_path`/`start_line`/`end_line`; vulture gives `file:line`;
  coverage gives per-file executed/missing line sets. A function is
  coverage-zero-hit iff none of its line range is in `executed_lines`
  (nested defs make this approximate — noted in `note` when a range
  contains another candidate).
- **Pattern exclusions are code, not prose.** `dunder_protocol` -> name
  matches `^__\w+__$`; `unittest_discovery` -> file under `tests/` and name
  matches `Test*`/`test_*`; `callback_argument` -> already absorbed by the
  `USAGE` edge set; `getattr_proxy` -> candidate's module AST-contains a
  `__getattr__` function def => cap tier at B, note `module defines
  __getattr__ delegate`; `lazy_import` -> candidate's module appears in the
  function-scope-import set of a registered root module (AST scan) => cap
  at B, note `lazy-imported module`. Registry `entry_points` names are
  minus-listed by qualified name.
- **Tests/** candidates emit but cap at B** with `surface: tests` — R5
  means they ride along with the production code they cover, and the row
  is how U4 finds that pairing.
- **`ui/js/` candidates are Tier C unconditionally** — the graph indexes JS
  (census confirmed) but route/template wiring is weak-signal; corroboration
  does not promote them.

## High-Level Technical Design

```mermaid
flowchart LR
    subgraph inputs [Inputs]
        DB[graph.db.zst + artifact.json]
        REG[entrypoints.yaml]
        VUL[vulture.txt]
        COV[coverage.json]
    end
    DB --> F[freshness assert<br/>commit == HEAD]
    F --> Q[zero-inbound CALLS|USAGE<br/>Function+Method pool]
    REG --> M[minus-list + pattern caps]
    Q --> J[join on file+line]
    VUL --> J
    COV --> J
    M --> J
    J --> T{tier}
    T -->|graph + vulture/cov| A[A]
    T -->|single source| B[B]
    T -->|ui/js, tests, proxy-capped| C[C or B-capped]
    A --> OUT[dead-code-report-2026-10.json<br/>sorted, byte-deterministic]
    B --> OUT
    C --> OUT
```

Row shape (per parent spec):

```json
{"symbol": "orchestral.foo._bar", "file": "orchestral/foo.py",
 "lines": [12, 30], "tier": "A",
 "evidence": [{"source": "graph", "detail": "zero inbound CALLS|USAGE"},
              {"source": "vulture", "detail": "60% confidence"}],
 "note": ""}
```

## Implementation Units

### U1. Graph reader + registry filter core

- **Goal:** Candidate pool extraction that is provably fresh and honest.
- **Requirements:** P1, P2, R2
- **Files:** `scripts/dead_code_report.py` (new),
  `tests/test_dead_code_report.py` (new)
- **Approach:** Decompress `graph.db.zst` (`zstandard` import fallback to
  `zstd -dc` subprocess, clear error if neither), freshness-assert against
  `git rev-parse HEAD`, run the canonical zero-inbound query over
  `CALLS|USAGE` for `Function`/`Method` nodes in repo-owned paths, load
  `entrypoints.yaml` (same `yaml.safe_load` pattern as
  `scripts/audit_entrypoints.py`), subtract `entry_points` by qualified
  name, and apply the pattern exclusions/caps from KTD above. Lazy-import
  and `__getattr__` detection are a small AST pass over the candidate's own
  module file plus `harness.py` — read once, memoized.
- **Patterns to follow:** `scripts/audit_entrypoints.py` — same
  "enumerate, compare, report" script shape.
- **Test scenarios:**
  - Fixture sqlite (schema subset: `projects`/`nodes`/`edges`) with one
    dead function, one called function, one registry-listed `cmd_*` —
    yields exactly the dead function.
  - `artifact.json.commit` != HEAD => nonzero exit; passes with
    `--allow-stale-index`.
  - A `__getattr__`-bearing module's zero-inbound candidate is emitted but
    capped at B with the delegate note.
  - A `tests/test_x.py` `test_*` function with zero inbound is excluded by
    the unittest-discovery pattern.
- **Verification:** unit tests green; live run on the real index lists a
  candidate set consistent with census expectations (tens, not hundreds).

### U2. Evidence join + tiered deterministic emit

- **Goal:** The full three-source join and the committed JSON format.
- **Requirements:** R1, P3, P4
- **Files:** `scripts/dead_code_report.py` (same),
  `tests/test_dead_code_report.py` (same)
- **Approach:** Parse vulture `file:line` output and `coverage json`
  `files[f].executed_lines/missing_lines`; map both onto graph nodes by
  file+line containment. Assign tier per KTD rules. Emit sorted JSON
  `{schema, index_commit, index_fresh, counts{pool,A,B,C}, rows[]}` to
  `--out` (default `docs/analysis/dead-code-report-2026-10.json`).
  CLI flags: `--graph-db`, `--registry`, `--vulture-file`,
  `--coverage-json`, `--allow-stale-index`, `--out`, `--repo-root`.
- **Test scenarios:**
  - Fixture: dead fn + vulture hit + coverage miss => Tier A; dead fn +
    graph only => Tier B; JS-path row => Tier C.
  - Registry-listed symbol with a vulture hit is still absent.
  - Same inputs twice => byte-identical output.
  - `--vulture-file` absent => zero Tier-A rows, no fabricated evidence.
- **Verification:** deterministic-output test passes; `--help` documents
  every flag.

### U3. Live collection run + committed report

- **Goal:** The real artifact and its hand-verification.
- **Requirements:** R1, R4
- **Dependencies:** U1, U2
- **Files:** `docs/analysis/dead-code-report-2026-10.json` (new),
  `docs/analysis/code-census-2026-10.md` (append tier counts)
- **Approach:** Reindex (`index_repository mode=full persistence=true` —
  current artifact is stale at `a3d1221`, two merges behind `143400a`);
  run the suite under `coverage` in the gate venv (pip install ad-hoc;
  first full-suite coverage run is its own chore — document any needed
  sandbox/TUI exclusions inline in the run command); run `uvx vulture`;
  generate and commit the report. Hand-read 5 Tier-A candidates; record
  tier counts in the census doc next to the raw pool number, including the
  94->~65 reconciliation note (canonical edge set vs `in_degree`).
- **Test expectation:** none — data artifact; the spot-check is the test.
- **Verification:** report committed; 5/5 spot-checked Tier-A candidates
  are truly unreachable; census doc records tier counts.

---

## Scope Boundaries

### Deferred to Follow-Up Work

- **U4 deletion sweeps** — this unit emits candidates only; no deletion.
- **U5 ratchet/cadence doc** — parent plan keeps it as its own unit; this
  plan ships the script it would schedule.
- **Vulture-only symbols outside the graph pool** (e.g. unused variables,
  attributes — vulture sees non-function dead code the graph doesn't model):
  out of scope; the pool is graph-anchored by design.
- **Whole-module deletions** — parent's existing deferral stands.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Graph misses a dispatch the registry doesn't know | caps (B) + notes on proxy/lazy surfaces; registry drift test stays the backstop |
| Coverage run flaky on sandbox/TUI paths | coverage is corroboration only; a partial coverage file caps tiers, never fabricates |
| Stale index emits phantom candidates | P2 freshness gate refuses to run |
| zstd/`zstandard` both absent on a contributor box | clear error message naming both fallbacks |
| Report churns on every commit (index_commit field) | expected — it is honest staleness, and R4 wants re-runs diffable |

## Verification

`tests/test_dead_code_report.py` green; committed
`docs/analysis/dead-code-report-2026-10.json` regenerates byte-identically on
re-run; 5 Tier-A spot checks all truly unreachable; census updated. Standard
gate before commit: `unittest discover -s tests`, `ruff check .`,
`mypy orchestral harness.py` — plus `mypy` on the new script is in scope only
if `harness.py`-adjacent type-checking would otherwise flag it (scripts/ is
outside the mypy gate per `AGENTS.md`).

## Open Questions

- Do sandbox/TUI tests survive a coverage-instrumented full-suite run, or do
  they need omission markers? Resolved empirically at U3; exclusions get
  documented in the script's docstring.
- Should `coverage` land in `.[dev]` eventually? Deferred — decide after the
  first collection shows how often agents re-run it.
