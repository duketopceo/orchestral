# v3 task family — real-repository tasks

v3 tasks grade orchestrator/worker pairings against **real open-source
repositories**, not synthetic prompts. The harness stages a pinned repo
checkout inside an isolated CubeSandbox microVM, overlays the worker's
files, installs dependencies from a staged wheelhouse (no guest network required),
and runs a declared test command. The verdict is the command's exit code —
never model stdout.

`v3-*` is a *difficulty tier*: it sits beside the cheaper `v2-*` synthetic
suite, which stays in place as the low-cost sanity lane.

## Pipeline

```
fixtures/registry.yaml          (committed: id → repo, pinned commit, license)
        |  harness.py fixtures fetch <id>
        v
fixtures/<id>.tar.gz            (gitignored: repo/ + wheelhouse/)
fixtures/<id>.lock.json         (gitignored: sha256, fetched_at)
        |  run_repo_suite()
        v
CubeSandbox microVM             files.write tarball -> tar xzf
                                worker files overlay repo/
                                metadata.test_files overlay LAST (oracle wins)
                                setup_commands (e.g. pip --no-index wheelhouse)
                                verify.command -> exit code is the verdict
```

## Authoring a fixture task

```yaml
id: v3-mi-run-length
type: code
prompt: |
  ...spec text the worker sees — embed the target source it must change...
metadata:
  fixture: more-itertools-10.8   # must exist in fixtures/registry.yaml
  workdir: repo                  # directory inside the tarball
  expected_paths: ["more_itertools/rle.py"]   # files the worker must return
  setup_commands:
    - "python -m pip install --quiet --no-index --find-links /home/user/wheelhouse pytest"
  verify:
    command: ["python", "-m", "pytest", "tests/test_v3_rle.py", "-x", "-q"]
    fail_to_pass: ["test_runs_merge", ...]    # declared scope, command-granularity verdict
  test_files:                    # oracle overlay — written AFTER worker files
    tests/test_v3_rle.py: |
      from more_itertools.rle import run_length_encode
      def test_runs_merge(): ...
  reference:                     # a spec-conforming fileset, for selfcheck
    more_itertools/rle.py: |
      ...
  timeout_seconds: 240
  difficulty: standard           # standard | hard | expert
  archetype: feature-gap         # bugfix | feature-gap | revert | refactor
  contamination_risk: low        # label, honest — published claims carry it
```

Rules the audit enforces (`harness.py audit --strict`):

- `metadata.fixture` is only legal on `type: code`.
- The fixture must be registered: repo + full commit SHA + allowlisted
  license (MIT/BSD/Apache/ISC family) + `license_url`.
- `verify.command` must be a non-empty list of strings.
- No guest command may fetch from the network — `curl`, `wget`,
  `git clone`, bare `pip install` are rejected. Install from `wheelhouse/`
  only (`--no-index --find-links`).
- `metadata.test_files` must map repo-relative paths to bodies; paths that
  escape the tree are rejected.
- `metadata.tests` on a fixture task is a warning — one grading contract.

## Registering a fixture

```bash
# fixtures/registry.yaml
fixtures:
  boltons-25.1:
    repo: mahmoud/boltons
    commit: 4e5faa3d7e4008d89e0d8bf1ea87b6d9a061a16d   # full sha
    license: BSD-3-Clause
    license_url: https://github.com/mahmoud/boltons/blob/master/LICENSE
    verify_deps: ["pytest"]      # wheels staged into wheelhouse/

python3 harness.py fixtures list     # what's registered + fetched
python3 harness.py fixtures fetch    # download + repack + sha256 lock
python3 harness.py fixtures check    # verify lock hashes still match tarballs
```

`fetch` strips the GitHub codeload wrapper directory so the archive is
`repo/<source>` + `wheelhouse/<wheels>`, rejects unsafe members (absolute
paths, `..`, links), downloads `verify_deps` as cp312/linux-aarch64 wheels
for the `code-interpreter` guest, and writes a lock file recording the
sha256. Tarballs and locks are gitignored — only `registry.yaml` is
committed. `check` re-hashes on disk so a corrupted or swapped tarball is
caught before a run burns a sandbox on it.

## Runtime requirements

- `ORCHESTRAL_CODE_RUNTIME=isolated` plus the CubeSandbox env contract
  (`scripts/cube-env.sh`: `E2B_DOMAIN`, `E2B_API_KEY` via omaseal,
  `SSL_CERT_FILE`, `ORCHESTRAL_CUBE_TEMPLATE=code-interpreter`).
- Without the isolated runtime the verifier returns a disabled,
  fail-closed report — host subprocess fallback does not exist by design.
- Guest network is never required: deps come from `wheelhouse/`, fetched
  host-side at `fixtures fetch` time.

## Calibration

Each new task is run once per cheap/mid/best reference pairing before it
joins the family. Targets: ~30% standard, ~45% hard, ~25% expert. A task
every pairing fails is treated as broken (bad spec, missing dep, flaky
suite) and fixed or dropped — same standard `audit`/`selfcheck` enforce
on the hidden-test family. Prefer named `fail_to_pass` tests over whole
suites: real repos carry flaky and network-dependent tests.

## Contamination honesty

Small obscure repos + feature-gap/revert mutations reduce memorization
advantage, but cannot prove a pass isn't recall. `contamination_risk`
labels every task; published results carry the label rather than claiming
clean measurement.
