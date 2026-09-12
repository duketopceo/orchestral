---
title: "feat: v1.0 shared tool — publishable open-source harness"
date: 2026-09-11
type: feat
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

## Goal Capsule

- **Objective:** A stranger can clone orchestral, `pip install` it, write a task spec and model config from the docs alone, run an eval against OpenRouter or any OpenAI-compatible endpoint, and publish scrubbed example results — with CI proving tests, lint, and types on every change.
- **Means:** Repo hygiene + packaging fixes, a provider seam over the existing client, a real CI quality job, a binary-safe publish path, and a docs sweep (KTD1–KTD5).
- **Authority:** ROADMAP.md v1.0 checklist is the scope source; AGENTS.md constraints (minimal deps, stdlib unittest, no committed runs/keys) are binding.
- **Stop conditions:** paid OpenRouter grid execution is out of scope; do not spend API budget to satisfy "example results."
- **Execution:** `ce-work` implements U1–U6 on a branch off `feat/v03-judgment`; LFG ships the PR.

## Product Contract

### Summary

v1.0 turns the research harness into a publishable open-source tool. The README currently documents flags that no longer exist, `pip install` produces a broken console script, image runs can't be published without corruption, tests never run in CI, and there is no way to point the harness at a non-OpenRouter API.

### Problem Frame

The project is ready to be announced publicly, but only once it reads and behaves like a real tool. Research found concrete gaps a first-time user would hit in the first five minutes: a console script that isn't packaged, a README showing a CLI shape from two versions ago, and no schema docs for the two YAML formats the whole workflow depends on.

### Key Decisions

- **v1.0 scope = the ROADMAP v1.0 checklist** (session-settled: user-directed — chosen over more v0.3 features or video tasks: roadmap order and the stated goal of announcing the project). Governs R1–R8.
- **Public-announcement readiness is the bar** (session-settled: user-directed — chosen over keeping a private research tool: user asked to be told when it's ready to post about). Governs R1, R6, R8.

### Requirements

**Docs & packaging**

- R1. README documents install (`pip install`, extras), quickstart, and every current subcommand/flag accurately — no stale `run --task tasks/x.yaml` examples.
- R2. `docs/` carries a task-spec schema reference covering every `TaskSpec` field, task `type`s, and the validation-check catalog.
- R3. `docs/` carries a model-config schema reference covering every `ModelConfig` field, roles, `metadata.modalities`/`vision`, and the `~` disable prefix.
- R7. `pip install .` yields a working `orchestral` console script; `pyproject.toml` has accurate license expression, classifiers, project URLs, and no unused runtime deps.

**CI**

- R4. A CI job runs the unittest suite, lint, and typecheck on pushes/PRs without needing `OPENROUTER_API_KEY`, plus at least one end-to-end integration test that exercises the runner against a mocked provider.

**Providers**

- R5. A model entry can name a provider other than OpenRouter (OpenAI-compatible base URL + per-provider API-key env var), with zero new runtime dependencies; OpenRouter remains the default and its behavior is unchanged.

**Publishing & hygiene**

- R6. `scrub`/`publish` produces a shareable `runs-pub/` tree that does not corrupt binary artifacts and carries a manifest suitable for publishing example results.
- R8. The repo carries no personal-tooling artifacts, stale absolute paths, or references to files that don't exist.

### Success Criteria

- Fresh-clone flow works end to end: `pip install .` → `orchestral --help` → dry-run a task → `report`/`dashboard`.
- `python -m unittest discover -s tests` passes locally and in CI; `ruff check` and `mypy` are green under committed config.
- A scrubbed image-task run round-trips: `artifact.png` bytes are identical before and after `runs-pub`.
- Docs let a user author a valid `tasks/*.yaml` and `models/*.yaml` without reading source.

### Scope Boundaries

- **In:** the ROADMAP v1.0 items above, plus the blocking defects they surfaced (binary scrub, broken console script, stale docs).
- **Deferred to follow-up work:** running the actual paid 100-page example grid (the pipeline + recipe ship; execution is an ops decision); GitHub App packaging of the eval action; per-vendor SDK providers (Anthropic, Gemini) beyond OpenAI-compatible endpoints.
- **Out:** video task type; new eval features; promotion/announcement content.

## Planning Contract

### Key Technical Decisions

- KTD1. **Provider seam via `typing.Protocol`, resolved per role.** Keep `OpenRouterClient` as the default implementation; add a `Provider` protocol (`chat`, `images`, `close`, ctx-manager, provider error) plus a `provider_for(model)` factory that resolves `provider`/`base_url`/`api_key_env` from `ModelConfig.metadata`. Providers are resolved **per role**: `Runner.run()` builds one client per distinct provider among orchestrator/worker/judge (cached by provider key — same-provider roles share the client), passes the right client to each plan/delegate/assemble/judge call site, and closes all of them in `finally`. This is required because cross-provider pairing is the point of R5: an OpenRouter orchestrator with a Together worker must not send one side's slugs to the other's endpoint. The generic OpenAI-compatible provider is **chat-only**: `images()` on a non-OpenRouter provider raises a clear unsupported-capability error (OpenRouter's `/images` endpoint is not the OpenAI `/images/generations` shape).
- KTD2. **Stdlib `unittest` only.** AGENTS.md forbids new deps; tests already use `MagicMock` on `client.client.post`. The CI integration test reuses that pattern rather than adding pytest/httpx-mock.
- KTD3. **`ruff` + `mypy` under a `dev` extra**, committed config, lenient enough to pass the current codebase (fix genuine issues, don't contort the code). CI job is separate from the dogfood eval workflow and needs no secrets.
- KTD4. **Publish = `runs-pub/` + manifest + recipe doc; check the ROADMAP box only for real output.** `privacy.py` copies known-binary extensions byte-for-byte, scrubs a known-filename allowlist, and widens credential patterns beyond `sk-or-`; a manifest indexes published runs. "Example results published" is checked only when a real scrubbed manifest is committed to a chosen location — running U4's pipeline over existing local `runs/` costs $0 and satisfies this if they exist; otherwise the box stays unchecked as deferred.
- KTD5. **Branch base: `feat/v03-judgment`** (PR #9 open at plan time). Docs cover judge/ablate/history/prompt-variant; rebase onto main when #9 merges.

### High-Level Technical Design

```mermaid
flowchart TB
  Y[models/*.yaml<br/>metadata: provider, base_url, api_key_env] --> F[provider_for per model]
  F -->|provider=openrouter, default| OR[OpenRouterClient<br/>OPENROUTER_API_KEY]
  F -->|provider=openai-compatible| OC[OpenRouterClient<br/>parametrized env var + base_url<br/>chat only]
  OR --> C[chat / images / close]
  OC --> C
  C --> R[Runner.run resolves one client<br/>per distinct provider, caches,<br/>closes all in finally]
```

### Assumptions

These are un-validated bets made in pipeline mode — flag any that are wrong:

- "Example results published" is satisfied by a real scrubbed `runs-pub/` + manifest committed to a chosen location — running U4's pipeline over the existing local `runs/` tree (hundreds of completed runs exist on the user's other machine) costs $0 and is the intended path; no fresh paid grid runs in this PR. If no usable runs exist anywhere, the ROADMAP box stays unchecked with an explicit deferral note.
- OpenAI-compatible config-driven providers satisfy "non-OpenRouter providers" for **chat/text tasks**; image tasks remain OpenRouter-only and fail fast elsewhere. Vendor-specific SDKs (Anthropic, Gemini) are deferred.
- `INDEX.md` and `scripts/luke-index-watcher.py` are personal tooling that don't belong in the public repo (INDEX.md references nonexistent `GUARDRAILS.md`/`SECURITY_GUIDELINES.md`); the plan removes them and fixes the macOS path in `AGENTS.md`.
- `pydantic` is imported nowhere and is dropped from dependencies (AGENTS.md's stack line and config.py's "Pydantic models" docstring are updated to match); the `ui` extra (fastapi/uvicorn with no FastAPI code) is dropped.
- `scripts/pull_swebench.py`'s undeclared `datasets` import gets a lazy import + clear error rather than a new dependency.
- `docs/plans/` stays in the public repo as design history; the R8 audit exempts it.
- The canonical public remote is `github.com/duketopceo/orchestral` (existing remote + HTTP-Referer); `project.urls` and the README's Action template point there.
- The dogfood eval workflow keeps running on this repo's PRs, but is gated to same-repo PRs so external forks don't get a guaranteed-red check.

### Open Questions (user decisions — non-blocking for implementation)

- **Publish destination for results:** commit `runs-pub/` to a separate public repo (e.g. `orchestral-results`), a gh-pages branch, or a subtree? Recorded as deferred; `docs/publishing.md` documents the recipe either way. Note: `runs/` doesn't exist on this machine — the ~482-run tree lives on the user's other machine, so publishing real results is a follow-up sync + `scrub`, not part of this PR.
- **Git-history exposure:** the repo history contains `/Users/lukekimball` paths in old commits (AGENTS.md). Going public exposes them; scrubbing history is a user decision, out of scope.
- **PyPI publication / v1.0 git tag:** deferred to announcement time; `version = "1.0.0"` ships now so the tag is a one-liner later.

## Implementation Units

### U1. Packaging and repo hygiene

**Goal:** installable package, honest metadata, no personal-tooling leftovers.
**Requirements:** R7, R8.
**Files:** `pyproject.toml`, `AGENTS.md`, `.gitignore`, `INDEX.md`, `orchestral/config.py`, `scripts/luke-index-watcher.py`, `scripts/pull_swebench.py`
**Approach:**

1. `pyproject.toml`: bump `version = "1.0.0"`; add `[build-system] requires = ["setuptools>=77"]` (PEP 639 SPDX license needs it); add `py-modules = ["harness"]` (or move the entry point into the package) so `orchestral = "harness:main"` resolves after install; switch `license = {text = "MIT"}` to the SPDX `license = "MIT"` + `license-files`; add classifiers/keywords/`project.urls` pointing at `github.com/duketopceo/orchestral`; drop `pydantic`; drop the `ui` extra (no FastAPI code exists).
2. `scripts/pull_swebench.py`: lazy-import `datasets` inside the function with a clear "pip install datasets" error.
3. `AGENTS.md`: replace the `/Users/lukekimball/...` path with a repo-relative instruction; update the "Keep the stack" line to the real dependency set (drop `pydantic`); remove the "web UI is in `ui/`" bullet (no such directory); keep the rest of the constraints.
4. `orchestral/config.py`: fix the module docstring ("Pydantic models" → dataclasses).
5. Remove `INDEX.md` and `scripts/luke-index-watcher.py` (personal tooling; INDEX references files that don't exist).
6. `.gitignore`: cover `reports/summary.txt` (or `reports/` broadly except keep-list).

**Test scenarios:**

- `pip install .` into a clean venv, then `orchestral --help` exits 0 (smoke expectation, not a unit test).
- `python -c "import harness"` does not import `pydantic`.
- `scripts/pull_swebench.py` without `datasets` installed fails with the friendly error, not `ModuleNotFoundError`.

**Verification:** clean-venv install smoke; `git grep -n "lukedaduke\|lukekimball\|/Users/\|GUARDRAILS\|SECURITY_GUIDELINES"` returns nothing outside `docs/plans/` (historical plans legitimately mention them).

### U2. Provider seam + OpenAI-compatible provider

**Goal:** one protocol, config-driven non-OpenRouter providers, OpenRouter unchanged.
**Requirements:** R5.
**Dependencies:** U1.
**Files:** `orchestral/providers.py` (new), `orchestral/openrouter.py`, `orchestral/runner.py`, `orchestral/config.py`, `harness.py`, `models/default.yaml` (comment documenting new fields), `tests/test_providers.py` (new)
**Approach:**

1. `orchestral/providers.py`: define `Provider` protocol (structural, `typing.Protocol`) matching the existing `chat`/`images`/`close`/`__enter__`/`__exit__` surface and return shapes (`usage`, `latency_ms`, `raw_response`); `provider_for(model)` resolves a client from `ModelConfig.metadata` keys `provider` (default `openrouter`), `base_url`, `api_key_env` (default `OPENROUTER_API_KEY`).
2. `OpenRouterClient` gets `api_key_env`/`base_url`/`referer`/`title` params so the generic path is one parametrized class; `openrouter` remains the default provider. Its `images()` raises a clear "image generation is OpenRouter-only for now" error when the resolved provider is not `openrouter` (chat-only generic provider).
3. `runner.py`: `Runner` accepts an optional `clients` mapping (role → Provider) for injection; absent that, `run()` resolves `provider_for` per role (orchestrator, worker, judge), caches clients by provider key so same-provider roles share one client, threads the right client into each call site (replacing the single `self.client`), and closes all resolved clients in `finally`.
4. `harness.py`: `_run_preamble` resolves required env vars from the selected models' providers and warns naming each missing env var (instead of the single hardcoded `OPENROUTER_API_KEY` check on main; the branch already centralizes pre-flight in `_run_preamble`).
5. Resolution transparency: log `provider → base_url → env var name` at run start (never the secret value); warn when `base_url` is `http://` with a non-loopback/non-private host (local providers like ollama legitimately use plain HTTP on loopback).
6. `config.py`: document `metadata.provider`/`base_url`/`api_key_env`; unknown provider names fail fast with a clear error.
7. No new dependencies.

**Test scenarios:**

- `provider_for` on a default model returns an `OpenRouterClient` pointed at the OpenRouter base URL.
- A model with `metadata: {provider: openai-compatible, base_url: "https://api.together.xyz/v1", api_key_env: "TOGETHER_API_KEY"}` resolves env var + base URL; missing env var raises a clear error naming the env var.
- Unknown `provider` value raises a clear error.
- Mixed-provider run: mock-inject distinct clients per role and assert the orchestrator's calls hit the orchestrator's client and the worker's hit the worker's client (proves per-role routing, not a shared client).
- `images()` invoked through a non-OpenRouter provider raises the clear unsupported error (not a 404).
- The generic provider's image-URL download keeps the existing SSRF guards (https-only, private-host blocklist, size cap) — reuse the `test_openrouter_images.py` download cases against a parametrized client.
- `http://non-private-host` base_url logs a warning; `http://localhost` does not.
- Harness pre-flight names the missing env var for a non-OpenRouter model.

**Verification:** new tests pass; existing suite unchanged and green; dry-run unaffected.

### U3. CI quality job + mock-provider integration test

**Goal:** tests/lint/types gate every PR without needing secrets.
**Requirements:** R4.
**Dependencies:** U1 (shared pyproject.toml edits land first), U2 (integration test uses the seam).
**Files:** `.github/workflows/ci.yml` (new), `.github/workflows/orchestral.yml`, `pyproject.toml` (`dev` extra, ruff/mypy config), `tests/test_integration_mock.py` (new)
**Approach:**

1. `pyproject.toml`: `[project.optional-dependencies] dev = ["ruff", "mypy"]`; `[tool.ruff]` + `[tool.mypy]` configs sized to pass the codebase (target-version py311, strict-optional on, ignore-missing-imports for optional deps like playwright).
2. `tests/test_integration_mock.py`: drive `Runner` non-dry-run with injected mock clients (U2's `clients` mapping; existing `client.client.post = MagicMock` pattern from `test_openrouter_images.py`) through plan → delegate → assemble → validate; assert artifact written, cost recorded, judge path exercised with a mocked judge response. Watch for the known judge-usage shape quirk (judge response usage arrives already `.to_dict()`-normalized) — assert judge cost lands, don't just assert a call happened.
3. `.github/workflows/ci.yml`: on push/PR — `pip install -e .[dev]`, `python -m unittest discover -s tests`, `ruff check .`, `mypy orchestral harness.py`. No `OPENROUTER_API_KEY`.
4. `.github/workflows/orchestral.yml`: gate the `eval` job to same-repo PRs (`if: github.event_name == 'workflow_dispatch' || github.event.pull_request.head.repo.full_name == github.repository`) so external fork PRs skip it instead of failing without secrets.
5. Fix whatever lint/typecheck surfaces; prefer minimal config relaxation over code contortions.

**Test scenarios:**

- Integration test: mocked chat responses for planner, worker, judge produce a run dir with `artifact.*`, `run.json` status `finished` (the literal the runner writes and `report --pairings` filters on), and non-zero usage totals — all without network.
- Lint/typecheck gate fails on a deliberately introduced unused import (verified once locally, not committed).

**Verification:** `ci.yml` green on this PR; local `ruff check`/`mypy`/unittest all pass.

### U4. Binary-safe scrub + publish manifest

**Goal:** `runs-pub/` is shareable, image artifacts intact, indexed by a manifest.
**Requirements:** R6.
**Dependencies:** none (parallel with U2/U3).
**Files:** `orchestral/privacy.py`, `harness.py` (`scrub` writes manifest by default), `tests/test_privacy.py` (new), `docs/publishing.md`
**Approach:**

1. `privacy.py`: known-binary extensions (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.mp4`, `.zip`, `.woff*`, `.db`) copy bytes verbatim; unknown/binary-detected files (NUL byte in first chunk) also copy bytes. Binary passthrough carries embedded metadata (PNG tEXt/EXIF) verbatim — `docs/publishing.md` tells users to strip metadata (e.g. exiftool) before publishing.
2. Widen `PATTERNS` beyond OpenRouter shapes — `sk-[A-Za-z0-9_-]{20,}` (OpenAI/generic), `sk-ant-`, `ghp_`/`github_pat_`, `AKIA[0-9A-Z]{16}`, `AIza…`, `gsk_`/`xai-`, PEM private-key blocks, and Windows paths (`C:\Users\…`) — since U2 multiplies the credential formats in play.
3. Redact provider endpoints: strip URL userinfo (`scheme://user:pass@host` → `scheme://[REDACTED_AUTH]@host`) and add a private/internal-hostname pattern, because U2's `metadata.base_url` is persisted into `run.json`/`events.jsonl` and would otherwise leak internal infrastructure to a public repo.
4. Scrub an allowlist of known run-artifact names (`run.json`, `events.jsonl`, `plan.json`, `worker-*`, `artifact.*`, `cost.json`, `report.json`, `screenshot.*`) rather than copying every file a user may have dropped into a run dir.
5. Guard the `.json` branch: `json.loads` failure falls back to `scrub_text` passthrough (matching the `.jsonl` branch) so one truncated `run.json` can't abort the whole publish.
6. `scrub_all` writes `runs-pub/manifest.json` by default: per run — orchestrator, worker, task, score, passes, cost, run dir relpath — enough for a static gallery or README table.
7. `docs/publishing.md`: the recipe — run grid → `scrub` → inspect `runs-pub/` → commit to a chosen location — plus the explicit caveats (scrub is conservative; eyeball output; binary metadata is not stripped).

**Test scenarios:**

- A run dir containing a real PNG round-trips through `scrub_run` byte-identical.
- A `.jsonl` containing an `sk-or-…` string, an `sk-…` OpenAI-style key, a `ghp_…` token, and `/home/user/...` + `C:\Users\...` paths is redacted; a `.png` is NOT rewritten (binary passthrough wins over pattern matching).
- `run.json` whose config carries `base_url: "https://user:pass@llm.internal.corp/v1"` publishes with userinfo stripped.
- A file not on the allowlist (e.g. `notes.txt`, `.env`) is absent from `runs-pub`.
- A malformed `.json` in a run dir doesn't abort `scrub_all`; it lands scrubbed-as-text.
- Manifest entries reflect `run.json` fields; missing fields degrade gracefully.
- `runs-pub` remains gitignored.

**Verification:** binary round-trip test passes; manifest validates against a real runs dir.

### U5. Documentation sweep

**Goal:** a stranger can use the tool without reading source.
**Requirements:** R1, R2, R3, R8.
**Dependencies:** U2 (provider docs reflect the seam), U4 (publishing doc referenced).
**Files:** `README.md`, `docs/task-spec.md`, `docs/model-config.md`, `docs/providers.md`, `docs/publishing.md`, `CONTRIBUTING.md`, `ROADMAP.md`
**Approach:**

1. `README.md`: badges-optional rewrite — install (`pip install`, `[shots]`/`[dev]` extras), quickstart (init → dry-run → live run → report/dashboard), accurate subcommand list covering **all** current subcommands (`init`, `run`, `grid`, `batch`, `ablate`, `history`, `report`, `dashboard`, `tui`, `shots`, `scrub`) with the shared flags (`--planner`, `--judge`, `--retry-limit`, `--prompt-variant`, `--jobs`, `--dry-run`, `--json`). Rewrite the GitHub Action section so it actually works externally: the documented template installs orchestral from the public repo (`pip install "orchestral @ git+https://github.com/duketopceo/orchestral"`) and invokes the `orchestral` console script — the current "copy orchestral.yml into your repo" text can't work because `pip install -e .` runs inside a repo with no orchestral source.
2. `docs/task-spec.md`: every `TaskSpec` field with type/default, task `type` values and which are implemented (`html`, `image`), the full validation-check catalog for text and image tasks, `metadata` conventions, a complete example.
3. `docs/model-config.md`: every `ModelConfig` field, roles, `metadata.modalities`/`vision`, `~` disable prefix, provider fields from U2, pricing semantics (`*_per_mtok`, `price_per_image` fallback).
4. `docs/providers.md`: provider config, env vars, adding a new provider.
5. `CONTRIBUTING.md`: setup, test command, style (stdlib unittest, minimal deps), PR expectations.
6. `ROADMAP.md`: check the v1.0 items this PR completes; leave video unchecked.

**Test scenarios:**

- Test expectation: none — docs unit; verified by link/command walkthrough instead.
- Every command shown in README/docs is executed once against the real CLI before commit (copy-paste correctness).

**Verification:** fresh-reader pass — a reader following README alone reaches a successful dry-run + report.

### U6. Final wiring check

**Goal:** the v1.0 checklist in ROADMAP reflects reality; PR is reviewable.
**Requirements:** R1–R8.
**Dependencies:** U1–U5.
**Files:** `ROADMAP.md`, PR body
**Approach:**

1. Re-walk ROADMAP v1.0; check only items actually delivered. Non-OpenRouter providers = config-driven OpenAI-compatible chat. "Example results published" is checked **only** if a real scrubbed `runs-pub/` + manifest from actual runs is committed to a chosen location (scrubbing existing local `runs/` costs $0); otherwise it stays unchecked with an explicit deferral note.
2. Confirm `python -m unittest discover -s tests`, `ruff check`, `mypy`, clean-venv install, dry-run smoke all green.

**Test scenarios:**

- Test expectation: none — verification gate unit.

**Verification:** Definition of Done below.

## Verification Contract

| Gate | Command |
|---|---|
| Unit tests | `python -m unittest discover -s tests` |
| Lint | `ruff check .` |
| Types | `mypy orchestral harness.py` |
| Install | clean venv `pip install .` → `orchestral --help` exits 0 |
| Dry-run smoke | `python harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --dry-run` |
| Scrub round-trip | binary artifact byte-identical through `scrub_all` |
| CI | `ci.yml` green on the PR; `orchestral.yml` unaffected |

## Definition of Done

- All units landed; `python -m unittest discover -s tests` green including new provider/publish/integration tests.
- `ruff check` and `mypy` green under committed config.
- Clean-venv install produces a working `orchestral` command.
- README, schema docs, provider doc, contributing guide committed; no stale commands or personal paths remain (`git grep` audit clean).
- `runs-pub/` scrub preserves binaries, redacts widened credential patterns and provider endpoint data, copies only allowlisted run artifacts, and emits `manifest.json`; `docs/publishing.md` documents the recipe, the eyeball-before-publish caveat, and the binary-metadata note.
- ROADMAP v1.0 checkboxes match delivered reality — "Example results published" is checked only when a real manifest lands; deferred items are explicitly noted.
- `orchestral.yml` skips fork PRs; `ci.yml` gates all PRs without secrets.
- No secrets, no `runs/` content, no dead-end experiment code in the diff.
