---
title: Cloudflare-hosted observatory — payload-mirror sync, R2 artifacts, Access-gated Worker, AI Gateway inference telemetry
created: 2026-10-02
status: draft (deepened 2026-10-02)
plan_kind: feature
---

# Cloudflare-hosted observatory + AI Gateway telemetry

## Problem frame

The observatory and the run index live entirely on one laptop:

- `runs/index.db` is a local SQLite file; `harness.py serve` reads it and
  serves the SPA on `127.0.0.1:8787`.
- The "public" URL is a `tailscale funnel` on the same laptop — it dies on
  sleep/reboot, exposes the **internal** observatory to anyone with the
  link, and cannot be opened from this machine itself (local :443 is
  squatted by cube-sandbox nginx).
- Eval/debug telemetry is well-separated *inside* the harness, but there is
  no second, independent record of model traffic: if the harness's own
  accounting is wrong (it has been — the empty-output falsification fix),
  nothing external notices.

The operator's Cloudflare account already has Access
(`duketopceo.cloudflareaccess.com`), the `shippedit.dev` zone, paid R2 and
Teams subs, and an unused Analytics Engine beta sub. Approved scope
(2026-10-02): observatory + index + artifacts move to Cloudflare; inference
stays on OpenRouter but routed through AI Gateway; the driver and
CubeSandbox microVMs stay local.

## Architecture: payload mirror, not data port

`orchestral/web/state.py` is not a SQL view layer — it is ~2,300 lines of
derivation over SQLite **plus the filesystem** (run-dir `report.json`/
`manifest.json`/`plan.md`/`events.jsonl`, `tasks/`/`groups.yaml`/
`experiments/`/`models/` YAMLs, `provider-catalog.json`, `reports/`
calibration state) with honesty semantics and statistics
(`stats.pairing_leaderboard`, `wilson_interval`, `diff_ci`,
`experiment.cell_state`, judge-state taxonomy) layered on top. Porting it
to a D1-backed Worker means rewriting all of it in TypeScript — the second
observability engine this plan explicitly forbids, with permanently
drifting parity.

**The sync unit is the rendered payload, not the row.** `harness.py sync`
computes the existing payload functions locally (parity by construction —
the same code path the local server runs) and pushes a snapshot tree to R2:

```
api/overview.json  api/runs.json  api/leaderboard.json  api/groups.json
api/pairings.json  api/cards.json (+lens variants)
api/experiment.<matrix>.json  api/models-catalog.json  api/flags.json
api/run/<id>.json  api/run/<id>/evidence.json
runs/<run_id>/<scrubbed files>   cards/<file>.png   publish/<bundle>
```

The Worker maps `/api/*` → R2 keys and serves SPA static assets. Query-param
filtering/sorting (`?group/status/q`, `?sort`) degrades to client-side
operations over base payloads — the local `filter_runs` is substring match
and `sort_leaderboard` is a column sort, cheap in JS. `/api/compare` either
precomputes known group pairs or is deferred to v2. Non-mirrorable
endpoints are absent by design: `/api/run/<id>/live`, `/api/shot.png`
(playwright-local), `/api/run` POST (launch), cancel, `/api/thread`
(model call), `/api/flag` POST.

D1 earns its keep only for the **reconciliation plane** (R5): a scrubbed
projection of `runs` + `calls` aggregates + `annotations` that gateway
logs can be diffed against. It is not the observatory's read store.

## Requirements

- **R1 — thin Access-gated mirror.** Worker at `obs.shippedit.dev` serves
  `ui/` static assets + `/api/*` reads from R2 keys, behind Cloudflare
  Access (Kurultai-lane pattern). `workers_dev = false` — the default
  `*.workers.dev` route bypasses Access and must be disabled; the Worker
  also rejects `Host` ≠ `obs.shippedit.dev` (same instinct as
  `server.py`'s `_same_origin` check). Hosted SPA runs read-only: flag /
  launch / cancel / thread / shot affordances show a "local-only" badge.
  Artifact bytes serve with `_ARTIFACT_HEADERS` ported verbatim
  (`Content-Security-Policy: sandbox allow-scripts`,
  `X-Content-Type-Options: nosniff`) — model-authored HTML is adversarial
  content; Access authenticates the viewer, not the bytes.
- **R2 — push sync via Worker ingest.** The driver POSTs payload snapshots
  + scrubbed artifact trees + scrubbed row projections to `POST /ingest`
  on the observatory Worker, authenticated by an Access **service token**
  (omaseal `cloudflare/obs-ingest`, never the account-level `cf` token —
  a laptop-side leak must not carry Workers-deploy/DNS/Access scope). The
  Worker validates payload shape server-side and writes D1 via `batch()`
  (atomic) + R2 via binding. Chosen over the D1 REST API: narrower
  credential, no global rate limit, and the endpoint is a second scrub
  gate (rejects bodies/`input_json` on sight). Sync failures are non-fatal
  to the driver: warn, retry next run.
- **R3 — dirty-set detection, not watermark.** `finished_at` misses real
  post-finish mutation: judge backfill (`judge.py` `update_meta` +
  `report.json` rewrite + appended `calls`), `revalidate` (score/passes/
  `failure_reason` + appended event), `backfill_calls` (delete+reinsert),
  and `set_annotation` (operator flags, `publish-mark`, driver aborts —
  watermarkable via `annotations.updated_at`). Add a sync journal —
  `update_meta`/`index_meta`/`set_annotation`/`backfill_calls` append
  `run_id`s to a `sync_dirty` table (four call sites, all already funneled
  through `RunStore`) — and a per-run file-manifest hash for the R2 side
  (`report.json` and `events.jsonl` legitimately change post-finish).
  The hook must cover the failure path too: `Runner.run` re-raises on
  failure after `store.update_meta`, so hook at `index_meta`/a
  `Runner.on_run_finished` callback mirroring `on_run_created`, not at
  the `launch` call site.
- **R4 — scrub boundary applies to every egress channel.**
  - *Artifacts*: push input is the **output of `privacy.scrub_run`**
    (`_scrub_dir` output tree + its omitted/withheld record) — never a
    walk of the raw run dir. `HoldoutRunError` → push nothing; optionally
    sync a name-only withheld entry per `privacy.py`'s withheld-entry
    policy. Decision (operator, 2026-10-02): **Access-gating replaces the
    manual `publication_review` step for synced artifacts** — hosting ≠
    publishing; external publication remains a separate decision.
    Known deltas to name in the runbook: `plan.md` is not allowlisted
    (the hosted plan panel needs it added or the payload adjusted);
    `artifact.zip` is withheld (archives never publish) so
    `/api/run/<id>/artifact/<member>` serves nothing for multi-file tasks;
    `runs.run_dir` is stripped on push (`to_public_dict` semantics).
  - *Rows*: a stated publication projection for D1 — explicit column
    allowlist (ids, models, status, timestamps, cost/tokens, score/passes,
    `judge_*`, `run_group`, `replicate`, `dry_run`, `latency_ms`,
    `holdout` as derived bool); `run_dir` dropped; `privacy.scrub_dict`
    applied over retained free-text (`config`, `env`, `failure_reason`).
  - *Calls*: `input_json`/`output_json` hold full prompt/completion bodies
    (the DUK-290 scar already exists for exactly this). No body column
    ever syncs — the D1 calls projection is ledger-only (model/role/
    tokens/cost/pricing_source/sequence/run_id) or, better, derived from
    the scrubbed `events.jsonl` shape (`messages_withheld` markers).
    `error` is internal too (provider-controlled text). `judge_cache` is
    judge internals — never syncs.
  - *Hosted payloads* serve the scrubbed record: call rows as size-only
    ledger — `call_previews` (2KB cap) is a bound, not a redaction, and
    does not go to the cloud. No live endpoint (raw `events.jsonl` never
    exists server-side).
  - *In-flight runs*: v1 accepts frozen `running` rows until post-finish
    push — stated limitation.
- **R5 — AI Gateway `orchestral-eval` in front of OpenRouter** —
  - **Payload logging OFF.** Gateway request/response logging is ON BY
    DEFAULT and stores bodies — every prompt (including holdout task
    text) and completion would persist in CF logs. Provision with payload
    collection disabled AND send `cf-aig-collect-log-payload: false` on
    every request (belt + suspenders; the per-request override survives
    misconfiguration). Acknowledge: prompts still transit CF memory —
    the decision is about persistence, and it is explicit.
  - `base_url` → `gateway.ai.cloudflare.com/v1/<acct>/orchestral-eval/compat`
    via existing per-model `metadata.base_url` or a global env override in
    `provider_key`/`provider_for` — no new Provider option needed.
    `provider` stays `"openrouter"` or the provider gates in
    `openrouter.py` reject.
  - `cf-aig-metadata` carries ≤5 entries (API cap): `run_id`,
    `cell` (the `Cell.key` `task:orch:worker` format), `arm`, `role`,
    `replicate`. Identifiers only — never `run_dir`, prompts, config.
    Labels travel **per call** (new kwarg or contextvar) through the ~20
    `chat` call sites — clients are deduplicated across roles by
    `provider_key` and judge backfill shares clients across runs, so a
    client-level attribute would mislabel/race.
  - **`decide()` cannot traverse `/compat`** — it builds
    `{host}/api/alpha/decisions` and discards the base path. Decision
    (operator, 2026-10-02): `decide` routes **direct to OpenRouter**;
    gateway labels cover `chat`/`images`/`videos` only. The judge/jev
    telemetry gap is documented, not hidden. U0 probes whether
    `/api/alpha/decisions` forwards through the gateway — if it does,
    revisit.
  - Authenticated Gateway (`cf-aig-authorization`) — foreign traffic
    through an open gateway poisons the reconciliation ledger. While
    touching `_headers`: fix the retry path sending `Authorization`
    cross-host on redirects (`follow_redirects=True` + unguarded
    `_headers()`).
  - Caching OFF (identical prompts returning cached responses poison
    measurements), dynamic routing/fallback OFF (a silent provider swap
    inside a measured cell invalidates it), guardrails/DLP OFF (eval
    payloads are adversarial by design), spend limit as a second brake
    under `--budget`/`--daily-cap`.
- **R6 — deploy + secrets.** `wrangler.jsonc` committed (account_id,
  database_id, bucket names are identifiers — fine); secrets never
  committed (token, service-token secret → `wrangler secret`/omaseal).
  `cf` wrapper for operator CLI. Repo-rule collisions to amend: AGENTS.md's
  "Python 3.11+, pyyaml, httpx, rich" stack rule needs a scoped exception
  for the Worker (wrangler/TS); any new CI job must update
  `.github/required-checks.json` (the required-check contract test
  enforces this).
- **R7 — observability.** Workers Observability for the mirror's own
  logs. Analytics Engine (already-subscribed beta) reserved for later
  high-cardinality eval time series — noted, not built.

## Non-goals

- Driver/CubeSandbox on Cloudflare — long-lived Python + microVM spawns
  cannot live in a Worker. Stays local.
- Public unauthenticated surface — Access-gated only; a public scrubbed
  Pages bundle is a separate decision.
- Local observatory changes — `harness.py serve` keeps working against
  local sqlite; laptop remains source of truth. `sync --verify` diffs
  counts/sums; nothing local reads remote.
- `decide()` via gateway (documented gap), Logpush, AE pipelines,
  dynamic routing — v2 at best.
- Live/in-flight run mirroring.

## Task list

| # | Task | Files | Verify |
|---|------|-------|--------|
| U0 | Provision: D1 `orchestral-runs`, R2 `orchestral-artifacts`, gateway `orchestral-eval` (cache off, **payload logging off**, spend limit, auth'd gateway), Access app `obs.shippedit.dev` + service token → omaseal `cloudflare/obs-ingest`. **Probe**: does `/api/alpha/decisions` forward through the gateway? | `wrangler.jsonc`, `infra/cloudflare/` | `cf d1 list`; probe call lands with metadata + no payload log; Access denies unauthenticated |
| U1 | `orchestral/cf.py` push client + `harness.py sync --push/--verify`: payload render, R2 upload of `scrub_run` output, D1 row projection; `sync_dirty` journal table + per-run file-manifest hash | `orchestral/cf.py`, `orchestral/storage.py` (journal hooks), `harness.py`, `tests/test_sync.py` | dry-run + live push of one finished run; `--verify` diffs zero |
| U2 | Post-run hook covering finish + failure paths (`index_meta` or `Runner.on_run_finished`); holdout skip via `scrub_run`/`HoldoutRunError` | `orchestral/experiment.py` or `runner.py`, `orchestral/cf.py` | a run's payload lands in R2/D1; a holdout run pushes nothing |
| U3 | Worker `cf-observatory/`: `[assets]` serves `ui/`; `/api/*` → R2 keys; `POST /ingest` (Access service-token auth, schema validation, D1 batch + R2 put); artifact serving with `_ARTIFACT_HEADERS`; `workers_dev=false` + Host check; SPA read-only badge | `cf-observatory/` (new), `ui/app.js` (badge) | `wrangler dev`; browser-verified parity for overview/runs/leaderboard; unauth'd request denied; artifact CSP header present |
| U4 | Gateway path in `providers.py`/`openrouter.py`: base_url override, `cf-aig-authorization`, `cf-aig-collect-log-payload:false`, per-call `cf-aig-metadata` (kwarg/contextvar through ~20 call sites); `decide` stays direct; redirect auth-guard fix | `orchestral/providers.py`, `orchestral/openrouter.py`, call sites in `planners.py`/`judge.py`/`jevassist.py`/`review.py`, `tests/test_providers.py` | unit asserts headers + labels; one live run shows labeled metadata-only gateway logs |
| U5 | Hardening tests + docs: canary-prompt egress test (sentinel in no pushed object/D1 row/hosted payload); calls-body-column invariant test; `required-checks` contract if CI changes; `docs/runbooks/cloudflare-obs.md`; AGENTS.md stack amendment; ROADMAP tick | `tests/test_sync.py`, `tests/test_cf_egress.py`, docs | full gate + canary passes |

## Constraints and risks

- **Three egress channels, one boundary.** Scrub is file-tree-only
  (`DATABASE_EXTS` — `index.db` is explicitly non-publishable). This plan
  extends the boundary: file tree (scrub_run) → R2; row projection → D1;
  wire labels → gateway. Each channel gets its own allowlist; none may
  carry prompt/completion bodies, `run_dir`, or holdout material.
- **Reconciliation is the D1 justification.** Without the gateway-ledger
  diff use case, D1 is overbuild for 150 runs — keep it because the R5
  telemetry story wants SQL, not because the observatory needs it.
- **Failed-run path.** `Runner.run` re-raises after `update_meta`; a hook
  placed after `launch` returns never sees failures. Hook placement is
  pinned to `index_meta` or a runner callback for this reason.
- **`_row_to_meta` is positional** (`SELECT *` by index) — the D1
  projection must never feed back into `RunStore`; it is a one-direction
  sink. (Nothing local reads remote — confirmed.)
- **Positional risk carried over**: `budget.py` and `wave-a-resume.sh`
  bypass `RunStore` with raw sqlite — they see local state only, which is
  correct by design.
- Cost: free-tier or already-paid; Workers AI (clef arm, later) bills
  separately.

## Verification gates

```bash
scripts/bootstrap-venv.sh /tmp/gate-venv
/tmp/gate-venv/bin/python -m unittest discover -s tests
/tmp/gate-venv/bin/python -m ruff check .
/tmp/gate-venv/bin/python -m mypy orchestral harness.py
/tmp/gate-venv/bin/python harness.py selfcheck --execute
# plus: wrangler deploy dry-run; Access-gated fetch of obs.shippedit.dev;
# unauth'd /ingest rejected; canary-prompt egress test; gateway probe
# shows metadata labels with no payload bodies
```
