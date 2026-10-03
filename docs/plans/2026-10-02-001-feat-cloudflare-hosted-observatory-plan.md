---
title: Cloudflare-hosted observatory — D1 index sync, R2 artifacts, Access-gated Worker, AI Gateway inference telemetry
created: 2026-10-02
status: draft
plan_kind: feature
---

# Cloudflare-hosted observatory + AI Gateway telemetry

## Problem frame

The observatory and the run index live entirely on one laptop:

- `runs/index.db` is a local SQLite file; `harness.py serve` reads it and
  serves the SPA on `127.0.0.1:8787`.
- The "public" URL is a `tailscale funnel` on the same laptop — it dies on
  sleep/reboot, exposes the **internal** observatory (full run index,
  costs, task internals) to anyone with the link, and cannot be opened
  from this machine itself (local :443 is squatted by cube-sandbox nginx).
- Eval/debug telemetry is well-separated *inside* the harness, but there is
  no second, independent record of model traffic: if the harness's own
  accounting is wrong (it has been — see the empty-output falsification
  fix), nothing external notices.

The operator's Cloudflare account already has everything needed: Access
org (`duketopceo.cloudflareaccess.com`), the `shippedit.dev` zone, paid R2
and Teams subs, and an unused Analytics Engine beta sub. Approved scope
(2026-10-02): observatory + index + artifacts move to Cloudflare; inference
stays on OpenRouter but routed through AI Gateway; the driver and
CubeSandbox microVMs stay local (Workers cannot host them — seconds-scale
CPU limits, no long-lived processes, no microVM spawns).

## Requirements

- **R1 — observatory Worker.** A single Worker serves the SPA (static
  assets via wrangler `[assets]`) plus read-only JSON endpoints that mirror
  the current `state.py` payload surface (overview, runs, run detail,
  leaderboard, compare, models, cards, experiment). Hosted at
  `obs.shippedit.dev` behind **Cloudflare Access** — same pattern as the
  Kurultai lanes. The worker reads D1 + R2 only; it never calls model
  providers and never executes anything. Thin observability layer, not a
  second orchestration engine — the existing rule stands.
- **R2 — index.db → D1.** Port the `runs` table (and its small satellites:
  `calls` if/when needed) to a D1 schema. SQLite dialect differences to
  handle: D1 supports standard CREATE TABLE/INDEX, has no `PRAGMA
  journal_mode` (always WAL-equivalent), no `sqlite_master` quirks the code
  depends on; JSON columns stay TEXT. `RunStore` gets a thin backend split:
  `sqlite3` locally, D1 remote — shared row-mapping, no behavior change to
  local runs.
- **R3 — sync protocol, push-based.** The local driver pushes run rows +
  artifact refs to D1 after each run completes (`harness.py sync --push`
  and an automatic post-run hook in the experiment driver). Incremental
  via `run_id` + `finished_at` watermark; idempotent upsert on `run_id`
  PRIMARY KEY. Sync failures are non-fatal to the driver (warn, retry next
  run). Pull direction exists only to verify (`sync --verify` diffs row
  counts + spend sums), never to hydrate local state — local is the source
  of truth while the driver runs locally.
- **R4 — artifacts → R2.** `orchestral-artifacts` bucket, layout
  `runs/<run_id>/<artifact>` mirroring `runs/` on disk. Card exports and
  publish bundles also land under `cards/` and `publish/`. The scrub
  contract is unchanged: **only post-scrub artifacts push** — raw event
  logs containing model-visible prompts never leave the laptop (they are
  exactly what publication must never leak). The worker serves artifacts
  through a signed-ish read path (Access already gates auth; no public R2
  domain).
- **R5 — AI Gateway in front of OpenRouter.** One gateway `orchestral-eval`:
  - `Provider` gets a `base_url` option pointing at
    `gateway.ai.cloudflare.com/v1/<acct>/orchestral-eval/compat` instead of
    `openrouter.ai/api/v1` — same request shape, OpenRouter keeps serving.
  - Per-request `cf-aig-metadata` (or the gateway's metadata header)
    carries `run_id`, `task`, `orchestrator`, `worker`, `arm`, `seed` —
    every model call independently labeled at the wire. This is the
    external telemetry plane: gateway logs reconcile against `calls`
    metering and would have caught the runaway-token incident from outside
    the harness.
  - **Caching OFF** (eval poison), guardrails/DLP OFF (eval payloads are
    adversarial by design; screening them is a measurement error), spend
    limit set as a second brake at the harness `--budget` × daily-cap
    level.
  - The OpenRouter eval key stays the upstream credential; gateway auth is
    a CF token stored in omaseal (`cloudflare/ai-gateway`), never in repo.
- **R6 — deploy + secrets.** `wrangler.toml` (or `.jsonc`) checked in;
  `cf` wrapper (`~/bin/cf`, omaseal `Cloudflare_duketopceo/Personal`) for
  all CLI ops. No secrets in repo — Access policy + tunnel/DNS via API,
  documented in the runbook. CI deploy is optional v2; `wrangler deploy`
  from the operator machine is v1.
- **R7 — observability of the observatory.** Workers Observability /
  Analytics Engine (already-subscribed beta) for request logs; this is
  also where per-cell eval time series can land later if the SQL index
  can't carry cardinality — noted, not built now.

## Non-goals

- Running the experiment driver or CubeSandbox on Cloudflare. The driver
  is a long-lived Python process spawning microVMs — it stays local.
  (Cloudflare Sandbox/Containers are a different product family; revisiting
  them is a separate plan.)
- A public unauthenticated surface. Access-gated only, until/unless a
  scrubbed public Pages bundle is carved out as a separate decision.
- Migrating the *local* observatory away — `harness.py serve` keeps
  working against local sqlite; the Worker is the hosted mirror.
- Analytics Engine pipelines, Logpush, AI Gateway dynamic-routing/fallback
  for the eval path (all noted as v2 candidates; routing in particular
  must not silently alter which model serves an eval call).

## Task list

| # | Task | Files | Verify |
|---|------|-------|--------|
| U0 | Provision: `orchestral-runs` D1, `orchestral-artifacts` R2, `orchestral-eval` AI Gateway (cache off, spend limit), `obs.shippedit.dev` Access app + route | `wrangler.toml`, `infra/cloudflare/` | `cf d1 list`, gateway accepts a probe call, Access denies unauthenticated |
| U1 | D1 schema + `RunStore` backend split (`SqliteStore`, `D1Store` shim via REST or worker-side query endpoint) | `orchestral/store.py`, `orchestral/cf.py`, `infra/cloudflare/schema.sql` | unit tests against local sqlite unchanged; schema applies clean on D1 |
| U2 | `harness.py sync --push/--verify` + driver post-run hook | `harness.py`, `orchestral/cf.py`, `orchestral/experiment.py` | dry-run + live push of one completed run; verify diffs zero |
| U3 | Worker observatory: static SPA + `/api/*` endpoints reading D1/R2 | `cf-observatory/` (worker src), reuse `ui/` assets | `wrangler dev` serves; browser-verified parity for overview/runs/leaderboard vs local |
| U4 | AI Gateway `base_url` path in `Provider` + metadata headers | `orchestral/provider.py`, `orchestral/openrouter.py` | unit test asserts headers + base_url; one live run shows labeled logs in gateway |
| U5 | Runbook + docs: `docs/runbooks/cloudflare-obs.md`, ROADMAP + INTEGRATIONS cross-refs | docs | doc review |

## Constraints and risks

- **Scrub boundary is load-bearing.** R2 push must reuse the existing
  publication-scrub path; a convenience "push everything" flag is a leak
  vector and is explicitly not in scope. Internal eval/RL telemetry
  (prompts, judge internals) never syncs — same rule as model-visible
  prompts.
- **D1 limits** (per-query size, write throughput) are fine at eval scale
  (~150 runs, small artifacts) but the sync should batch upserts to stay
  polite.
- **AI Gateway is a proxy, not failover.** Dynamic routing/fallback must
  NOT be enabled on `orchestral-eval` — a silent provider swap inside a
  measured cell invalidates the datapoint.
- **Access policy** mirrors Kurultai lanes (email-gated to the operator).
- Cost: everything listed is free-tier or already-paid on this account;
  Workers AI (clef arm, if wired later) bills per-token separately.

## Verification gates

```bash
scripts/bootstrap-venv.sh /tmp/gate-venv
/tmp/gate-venv/bin/python -m unittest discover -s tests
/tmp/gate-venv/bin/python -m ruff check .
/tmp/gate-venv/bin/python -m mypy orchestral harness.py
/tmp/gate-venv/bin/python harness.py audit --strict
# plus: cf wrangler deploy --dry-run, live Access-gated fetch of
# obs.shippedit.dev, gateway probe call visible with metadata labels
```
