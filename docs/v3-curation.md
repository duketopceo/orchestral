# v3 task family — curation ledger

Per-task provenance for the real-repo family (`docs/v3-task-family.md` is the
authoring guide; this file is the *evidence* — where each task came from,
why its oracle is what it is, and what calibrated difficulty says).

Difficulty bands are **authoring assignments** until the calibration column
carries run ids. A band without evidence is a guess, not a measurement —
published claims must use the recorded labels, not raw pass rates.

## Authored set

| Task | Archetype | Fixture | Source commit | fail_to_pass rationale | Band | Contamination |
|---|---|---|---|---|---|---|
| `v3-mi-run-length` | feature-gap | more-itertools-10.8 | — (new module `rle.py`) | authored oracle: empty/singletons/runs/order/unhashable/generator/large | standard | low |
| `v3-mi-chunked-by` | feature-gap | more-itertools-10.8 | — (new module) | authored oracle | standard | low |
| `v3-mi-runs-of` | feature-gap | more-itertools-10.8 | — (new module `runs.py`) | authored oracle: empty/no-runs/located/edges/predicate/generator | standard | low |
| `v3-boltons-deep-merge` | feature-gap | boltons-25.1 | — (new module `mergeutils.py`) | authored oracle | hard | medium |
| `v3-boltons-parseqsl-encoding` | bugfix | boltons-parseqsl | fix `d45cc414e3` (upstream #455) | fix's added test `test_parse_qsl_encoding`, verbatim | standard | medium |
| `v3-boltons-namedutils-empty` | bugfix | boltons-namedutils | fix `970051b664` (upstream #462) | fix's 4 added tests, verbatim (parametrized over both factories) | standard | medium |
| `v3-boltons-bits-empty` | bugfix | boltons-bits | fix `efff866a3b` (upstream #468) | fix's 2 added tests, verbatim | hard | medium |
| `v3-boltons-jsonl-seek` | bugfix | boltons-jsonl | fix `805c803ca7` (upstream #464) | fix's added test `test_jsonl_relative_seek_binary_matches_text`, verbatim | hard | medium |
| `v3-boltons-barrellist-delete` | bugfix | boltons-barrellist | fix `574393a4c3` (upstream #472) | fix's 2 added tests, verbatim (helper `_multi_sublist_bl` inlined) | expert | medium |

## Provenance notes

- **Bugfix fixtures pin the parent** of the fix commit — the repo arrives
  broken; the fix's added tests ship as `metadata.test_files` oracle files
  (verbatim, BSD-3-Clause, source commit cited in each file header).
  `verify.command` runs the oracle file only — never the whole suite
  (network tests and platform-gated skips would poison the verdict).
- **Every task was verified host-side before commit**: the staged fixture +
  oracle test fails on the broken checkout (nonzero pytest exit) and passes
  with `metadata.reference` overlaid (exit 0). This is the spec-integrity
  floor — the same standard `selfcheck --execute` applies to
  `metadata.tests` tasks, exercised here through the real fixture tarball.
- **Contamination**: boltons (~7k stars) and more-itertools (~3.5k) are
  mid-visibility; all five bugfix commits are September-2026 vintage —
  recent enough to reduce but not eliminate pretraining recall. The
  worker prompt paraphrases the upstream issue and never names the graded
  test file.

## Deferred archetypes

- **revert** — plan asks for "a real regression commit applied". The
  boltons sweep found no fix commits without bundled tests in the usable
  history window, and the fetcher has no patch channel (fixtures are
  unmodified codeload snapshots). Honest execution needs either a
  fetch-time `patch:` field in the registry or a curated commit whose own
  suite is red — deferred rather than relabeling a bugfix.
- **refactor** — highest-effort archetype (author-written renamed tests
  for a restructured public API). Deferred; cap at 1–2 when picked up.

## Calibration

Pending — each task needs one run on three reference pairings (cheap /
mid / best) under `ORCHESTRAL_CODE_RUNTIME=isolated`, then band
reassignment and a fix-or-drop pass on anything degenerate. Record run
ids here when they land.

| Task | cheap | mid | best | band evidence |
|---|---|---|---|---|
| (all) | — | — | — | pending |
