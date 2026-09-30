# Plan: v3 real-repo task family on CubeSandbox

Date: 2026-09-30
Status: proposed
Depends on: PR #111 (doctor), PR #112 (`--jev-assist`) — both open, unmerged;
the A/B experiment driver from `2026-09-30-001` for U4 only.

## Goal

A `v3-*` task family that grades workers against **real open-source
repositories' own test suites**, executed inside CubeSandbox microVMs, with a
deliberate difficulty gradient — some cells the cheap pairings clear, some the
mid-tier clears, some nothing clears. The current suite has a ceiling effect:
`deepseek-v4-flash → glm-5.3-flash` passes 100% of the v2 matrix, so the
benchmark cannot rank pairings. v3 exists to restore discrimination at the top
of the range.

Non-goals: replacing the v2 family (it stays as the cheap sanity tier);
reproducing SWE-bench wholesale (we curate ~10 tasks, not 500); baking custom
sandbox templates (rejected — see Decisions).

## Verified ground truth

- **The `sandbox-code` template replica is FAILED** in CubeOps
  (`tpl-eb5676093459433d97e030bd`, created 2026-09-28): `Boot vm failed:
  CreateSerialManager(Epoll(Operation not permitted))`. This is the same
  defect fixed by cubesandbox-src commit `912869d` (termios2 ioctls missing
  from VMM/seccomp allowlists — the commit message documents the "epoll
  EPERM" message as the misleading symptom). The replica failed under the
  pre-patch shim and is now stuck `Needs rebuild`. **The fix is already
  installed**; the template just needs a rebuild.
- `orchestral/cubeexec.py:run_unittest_suite` already does everything v3
  staging needs: `sandbox.files.write`, `sandbox.commands.run`,
  host-side wall-clock deadline, fail-closed report contract with
  `sandbox_image`/`sandbox_cleanup` provenance. A repo fixture is a
  tarball written once and untarred in-guest — no template changes.
  Verified against the installed SDK: `e2b`'s `Filesystem.write` accepts
  `str | bytes | IO` and `List[WriteEntry]`, so the tarball ships as raw
  bytes in one call — no base64 hop.
- **Live probe on `code-interpreter` (post-patch shim, `CUBE_API_KEY`
  auth path — sandbox created 2026-09-30 and killed cleanly):** guest is
  Debian aarch64, Python 3.12, GNU tar 1.34, pip 25.0.1, 20G overlay —
  and **pytest is not installed**. Any pytest-based verify would need
  `pip install` inside the guest, i.e. network egress, which is open
  today but slated to close when CubeEgress policy lands. Correct under
  both regimes: the fixture tarball carries a `wheelhouse/` (wheels
  downloaded at fetch time on the host), and `setup_commands` installs
  with `pip install --no-index --find-links`. Guest `tar` is GNU; host
  (Asahi) `tar` is bsdtar — the fixture builder pins flags that both
  accept, and the guest-side extract is the only one that must be
  byte-safe anyway.
- `type: code` task shape (`tasks/code-fizzbuzz.yaml`): `metadata.module`,
  `expected_paths`, `timeout_seconds`, `reference`, `tests`. v3 adds
  `metadata.fixture` + `metadata.verify` without disturbing the existing
  contract.
- Worker output contract is `{"files": [{"path": ..., "content": ...}]}` —
  repo tasks instruct workers to return only the changed files at
  repo-relative paths; the verifier overlays them onto the staged fixture.
- Contamination is the dominant validity risk: popular repos live in
  pretraining data, so a "pass" may be recall, not repair ability. Mitigation
  is task-level: obscure/small repos, recent commits, optional scripted
  mutation (identifier renames) — recorded per task, never silent.
- Licensing blocks vendoring repo code into `tasks/`: fixtures are
  **fetched at authoring/runtime from pinned commits** (SWE-bench's model),
  never committed. Only permissive licenses (MIT/BSD/Apache-2.0/ISC) are
  eligible.
- Precedents to cite in docs: SWE-bench Verified (fail-to-pass grading),
  Multi-SWE-bench (multi-language), SWE-rebench (freshness vs contamination),
  Terminal-Bench (isolated env-driven tasks), Commit0 (generate-to-test).
- Egress from `isolated` sandboxes is currently **OPEN** (doctor reports it
  honestly; `allow_internet_access` is ignored upstream). v3 must therefore
  **not** rely on in-guest network: the fixture tarball is staged via the SDK
  filesystem API, and `verify.command` must not fetch. This also documents
  the existing caveat that oracle-bearing pass rates are advisory until
  egress policy lands.

## Design

### Fixture lifecycle (no template changes)

```
fixtures/registry.yaml        # committed: id, repo url, commit sha,
                                    # license, license_url, size class,
                                    # star band, contamination notes
fixtures/<id>.tar.gz          # fetched, gitignored
fixtures/<id>.lock.json       # fetched, gitignored: sha256, fetched_at

harness.py fixtures fetch [<id>]    # download tarball at pinned ref, hash it,
                                    # write lock; --all for the registry
harness.py fixtures check           # registry vs on-disk lock/hash drift
```

- Fetch uses `https://github.com/<org>/<repo>/archive/<sha>.tar.gz`
  (codeload), pinned by commit in the registry; `fixtures check` recomputes
  sha256 and fails on drift. License gate: registry validates `license`
  against an allowlist and requires `license_url`; `audit --strict` rejects
  fixture-referencing tasks whose fixture is absent or unlicensed.
- Sandboxes get the fixture **over the SDK wire**: the tarball as raw
  bytes via `files.write` (single call), then `tar xzf -C <workdir>` via
  `commands.run`. One write + one command regardless of repo size.
  Pathological members are rejected host-side *before* staging: absolute
  paths, `..`, symlink escapes, and members that would shadow the suite
  runner reuse `shadowing_members`-style checks on the member list.
- The fixture tarball is assembled by `fixtures fetch` as
  `repo/` + `wheelhouse/` (host-side `pip download` for every external
  verify-time dep — pure-python `py3-none-any` wheels or aarch64 manylinux
  wheels only; a repo whose test deps can't satisfy that constraint is
  ineligible). Guest `setup_commands` install from wheelhouse only:
  `pip install --no-index --find-links wheelhouse <dep>` — zero guest
  network, deterministic, works identically under open and closed egress.

### Task schema (extension of `type: code`, not a new type)

```yaml
id: v3-boltons-retry-fix
type: code
metadata:
  module: repo                       # sentinel: worker files are repo-relative
  fixture: boltons-25.1              # key into registry.yaml
  workdir: repo
  setup_commands:
    - "python -m pip install --quiet --no-index --find-links wheelhouse pytest"
  verify:
    command: ["python", "-m", "pytest", "tests/test_retryutils.py", "-x", "-q"]
    fail_to_pass: ["test_backoff_jitter_bound"]            # optional, granular
  timeout_seconds: 240
  difficulty: hard                    # band assigned after calibration runs
  archetype: bugfix                   # bugfix | feature-gap | revert | refactor
  contamination_risk: medium          # low | medium + note in docs/v3-curation.md
```

- `fixture` present ⇒ verifier stages the tarball, overlays worker files at
  repo-relative paths, runs `setup_commands`, then `verify.command`.
- `fixture` absent ⇒ existing hidden-unittest path, unchanged. One verifier,
  two modes; no dispatch fork.
- `fail_to_pass` (optional) enables per-test granularity in the report
  (`tests_passed_subset`) for BI; the run verdict stays the command's exit
  code — SWE-bench's fail-to-pass semantics, applied to a curated subset
  rather than a full suite.
- Worker prompt (authored per task) is a real issue/bug-report style text;
  it must never name the test files it will be graded by (same hidden-oracle
  rule as existing `metadata.tests`).

### Archetypes (difficulty levers)

1. **bugfix** — repo at parent of a real fix commit; prompt paraphrases the
   issue; fail-to-pass = tests the fix commit added/changed. Cheapest to
   author, most SWE-bench-like.
2. **feature-gap** — delete a documented function/feature + its callers from
   HEAD; prompt asks for the documented behavior; fail-to-pass = the
   feature's tests, kept. Contamination-resistant (the "correct" answer no
   longer exists verbatim in the fixture).
3. **revert** — apply a real regression commit; prompt is the reported
   breakage; fail-to-pass = tests broken by the regression.
4. **refactor** — rename/restructure a public API per spec; fail-to-pass =
   updated test names the author writes. Highest effort, highest novelty;
   cap at 1–2 tasks.

### Difficulty calibration (the "not all pass" requirement)

Each candidate task runs once on three reference pairings spanning the
existing matrix's quality range (cheap pair, mid pair, best pair). Assign
`difficulty`/`band` from observed outcomes; target family mix:

| Band | Observed reference pass | Target share |
|---|---|---|
| standard | all 3 reference pairs pass | ~30% of tasks |
| hard | 1–2 of 3 pass | ~45% |
| expert | 0–1 of 3 pass | ~25% |

A task where all reference pairs fail twice is treated as broken, not
expert — it gets fixed or dropped (same spec-integrity standard as
`harness.py audit --strict`).

### Test-selection rule (learned from SWE-bench)

Verify on a **named fail-to-pass subset**, not the full suite. Real suites
have network tests, timing races, platform-gated skips. Curation picks the
subset the fix/feature actually owns; `verify.command` may name files +
`-k`/`--lf`-style selectors rather than `discover`.

## Units

### U0 — Clear the failed template + confirm the patch holds (cubeVM host)

The stuck `sandbox-code` replica is a real fleet defect and blocks any
future "bake into template" path — fix it now while it's cheap.

- CubeOps → Templates → `tpl-eb5676093459433d97e030bd` → Rebuild (or CLI
  equivalent); record outcome.
- If rebuild still fails with `CreateSerialManager(Epoll)`: strace the VMM
  boot (`scripts/verify-cube-patches.sh` already locates the shim), confirm
  which syscall SIGSYS'd, extend `seccomp_filters.rs` allowlist on
  `fix/asahi-aarch64-bringup`, repatch, and add the sentinel to
  `verify-cube-patches.sh`. (Do **not** assume epoll — the patched commit
  documents that error string as misleading; measure.)
- Re-run `scripts/verify-cube-patches.sh --smoke`; a real microVM boot is
  the acceptance signal.
- Record rebuild outcome + evidence in `.bringup-evidence/`.

Gate: template shows Ready/compatible and a smoke sandbox boots. v3 units
below do **not** depend on this — they stage fixtures at runtime.

### U1 — Fixture registry + fetcher (orchestral)

- `fixtures/registry.yaml` + `.gitignore` entries for fetched blobs;
  `harness.py fixtures fetch|check`; sha256 lock files; license allowlist
  gate (`MIT`, `BSD-*`, `Apache-2.0`, `ISC`).
- `audit --strict` fails on: task referencing an unregistered fixture,
  registry entry without `license_url`, or fetched hash ≠ lock hash.
- Tests: fetch determinism (mocked transport), hash-mismatch rejection,
  license-gate rejection, gitignore coverage.

### U2 — Fixture staging + verify command in the isolated verifier

- Extend `orchestral/codeexec.py` / `cubeexec.py` `run_unittest_suite` (or a
  sibling `run_repo_suite` sharing the sandbox lifecycle): stage base64
  tarball → untar confined to workdir → overlay worker files →
  `setup_commands` → `verify.command` → same fail-closed report contract
  plus `fixture_id`, `repo_commit`, `staged_files` provenance fields.
- Host-side member screening (absolute/`..`/symlink/`shadowing_members`)
  before staging. Host-side member listing/screening uses Python's
  `tarfile` module (portable — no bsdtar/gtar flag concern); the
  guest-side `tar xzf` runs on the image's GNU tar 1.34 (probe-verified).
- The existing host wall-clock deadline wraps the whole stage+setup+verify
  window; `timeout_seconds` budget shared honestly in the report.
- Tests: staging writes/untars exactly the fixture members; pathological
  tarball rejected pre-write; worker overlay wins over fixture files;
  wheelhouse is installed with `--no-index`; `fail_to_pass` parsing;
  timeout enforced; no-network assertion (a `setup_commands`/`verify.command`
  entry that fetches — `curl`, `wget`, `pip install` without `--no-index` —
  fails `audit --strict`, not just at runtime).

### U3 — Author the v3 set (curation + calibration)

- Candidate pool to validate (all to be license/size/star-checked before
  commit): `boltons`, `more-itertools`, `parse`, `py-cpuinfo`,
  `sortedcontainers`, `python-dateutil`, `click` — expect to drop the
  popular ones on contamination grounds; prefer <5k-star repos and recent
  commits. Supplement with 1–2 Rust/Go/TS repos only if their toolchains are
  present in `sandbox-code` (check image first — it's a Python image;
  non-Python likely needs runtime installs, which means setup time and
  network — flag as stretch, not base).
- Author ~10–12 tasks across archetypes: ~5 bugfix, ~3 feature-gap,
  ~2 revert, ~1 refactor. Each records `archetype`, `source_commit`,
  `fail_to_pass` rationale, `contamination_risk` in metadata +
  `docs/v3-curation.md` table.
- Calibration: one run per task on the three reference pairings (isolated
  runtime); assign band; fix-or-drop anything degenerate.
- `audit --strict` covers new specs; `selfcheck --execute` where applicable.
- Tests: schema validation; audit gate passes on the authored set.

### U4 — Experiment + BI integration

- Add v3 cells to `experiments/jev-ab.yaml` (task axis grows; same
  pairing/arm structure). Dependency: experiment driver from plan 001 U1.
- Coverage ledger tracks v3 cells; publication metadata (`post`,
  `cell-state`) applies unchanged — published cards must cite
  `fixture_id` + `repo_commit` so a reader can reconstruct the corpus.
- BI payload adds `difficulty` band + `archetype` axes so the observatory
  can facet "which pairings survive the expert band" — the discrimination
  story this family exists to tell.
- Scrub/publication: fixtures are public repo code (no secrets); verify
  scrub pass anyway — worker artifacts may embed host paths/keys.

### U5 — Docs + runbook

- `docs/v3-task-family.md`: authoring guide (archetypes, fail-to-pass
  selection, calibration procedure, contamination policy, licensing gate,
  "why fixtures aren't vendored"), precedence cites (SWE-bench et al.),
  and the honest caveat section (egress-open until CubeEgress policy;
  oracle tasks advisory).
- `docs/eval-runbook.md` update: `fixtures fetch` as a launch precondition,
  `fixtures check` in the pre-spend checklist.
- `ROADMAP.md` entry for the v3 family.

## Risks / honest caveats

- **Contamination is unquantifiable.** We mitigate (obscure repos, recent
  commits, mutation) and record `contamination_risk` per task, but cannot
  prove a pass isn't recall. Published claims must use the recorded risk
  labels, not raw pass rates.
- **Real suites are flaky.** Mitigated by named fail-to-pass subsets; a
  task that flakes in calibration is fixed or dropped, never shipped flaky.
- **Runtime cost is real.** Repo tests run for minutes (vs ~8s for
  code-fizzbuzz). The A/B driver's cost-scaled rep sizing absorbs this —
  it's why v3 lands after/alongside the driver rather than before it. v3
  tasks are runnable standalone via `harness.py run` immediately after U2.
- **Single-arch fixtures.** Tarballs are source-only, so arch-neutral; but
  `setup_commands` that fetch would break if/when egress policy lands —
  the no-network audit rule in U2 keeps the family correct under the
  intended end state.
- **Template rebuild may reveal a second seccomp gap.** U0's contingency
  is measured, not assumed; either way it stays inside
  `fix/asahi-aarch64-bringup` + the patch-integrity manifest.

## Verification (all gates, unchanged)

```bash
scripts/bootstrap-venv.sh /tmp/gate-venv
/tmp/gate-venv/bin/python -m unittest discover -s tests
/tmp/gate-venv/bin/python -m ruff check .
/tmp/gate-venv/bin/python -m mypy orchestral harness.py
/tmp/gate-venv/bin/python harness.py audit --strict      # task specs changed
/tmp/gate-venv/bin/python harness.py selfcheck --execute # grading behavior changed
```

Plus per-unit: U0 smoke boot; U2 live staged-fixture run on `sandbox-code`;
U3 calibration evidence (run ids in `docs/v3-curation.md`).
