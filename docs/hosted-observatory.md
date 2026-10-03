# Hosted observatory runbook

`https://obs.shippedit.dev` — a read-only, Access-gated mirror of the
local observatory. The Cloudflare Worker serves JSON snapshots and
scrubbed run artifacts out of R2; it contains **no derivation logic** —
`harness.py sync` renders the same payload functions `serve` uses, so
hosted and local agree by construction.

## Daily operation

```bash
# see what would move (dirty journal), no credentials needed
python3 harness.py sync

# push the dirty set + refresh global snapshots
ORCHESTRAL_OBS_TOKEN=$(omaseal get cloudflare obs-ingest) \
  python3 harness.py sync --push

# one-time backfill or repair
ORCHESTRAL_OBS_TOKEN=$(omaseal get cloudflare obs-ingest) \
  python3 harness.py sync --push --all

# diff local vs hosted counts/cost
ORCHESTRAL_OBS_TOKEN=$(omaseal get cloudflare obs-ingest) \
  python3 harness.py sync --verify

# push run rows + ledgers automatically as runs finish
export ORCH_CF_SYNC=1 ORCHESTRAL_OBS_TOKEN=<id:secret>
```

Journal semantics: every `update_meta`, `backfill_calls`, and run
annotation marks the run dirty; entries clear only after a clean push.
Holdout and missing-dir runs are terminal skips — they clear without
uploading anything.

## AI Gateway inference telemetry

```bash
export ORCHESTRAL_AIG_GATEWAY="1661907b2d7e4a20800306e6a57844c5/orchestral-eval"
export CLOUDFLARE_AIG_TOKEN=$(omaseal get cloudflare orchestral-aig-run)
```

With both set, default-OpenRouter calls route through the gateway's
`/compat` path with `cf-aig-authorization`,
`cf-aig-collect-log-payload: false`, and per-call `cf-aig-metadata`
(run_id, task, role, group, seed). `decide()` and `images()` stay direct —
they don't exist behind `/compat`. Custom `base_url` models are never
rewritten.

Gateway logs (metadata + cost, **no bodies** — `zdr` + per-request flag):
Cloudflare dashboard → AI → AI Gateway → `orchestral-eval`, or the
GraphQL `aiGatewayLog` API.

## Architecture

```
local                              Cloudflare
─────                              ──────────
harness.py sync ──────────────►  /ingest/run|state  (Access service token)
  global_payloads()                  │
  scrub_run() per run                ▼
  d1_projection()              Worker (worker.js)
                                   ├─ R2: api/*.json snapshots,
                                   │      runs/<id>/* scrubbed files
                                   └─ D1: runs/calls/annotations ledger
browser ──► Access login ──►  GET /, /api/*  (read-only; mutations → 501)
```

Boundaries enforced at both ends:

- `calls.input_json` / `output_json` never sync — column allowlists in
  `cf.py` AND re-checked server-side; the ingest endpoint recursively
  rejects those key names.
- Run payloads render against the **scrubbed** tree (`_ScrubbedStore`),
  so hosted `detail`/`evidence`/`live` read redacted events, not raw.
- `workers_dev = false` + a Worker-side host check — the default
  `*.workers.dev` route would bypass Access.
- Artifact bytes carry `Content-Security-Policy: sandbox allow-scripts`
  (model output is adversarial content).
- `sync --verify` compares run counts and total cost; a deeper diff is
  `r2 object list orchestral-artifacts` vs `sync` dry-run output.

## Redeploying the Worker

```bash
cd infra/cloudflare/observatory
export CLOUDFLARE_API_TOKEN=$(omaseal get Cloudflare_duketopceo Personal)
wrangler deploy
# D1 schema (once, or after schema.sql changes):
wrangler d1 execute orchestral-runs --file schema.sql --remote
```

Provisioned resource IDs and the Access policy correction live in
`infra/cloudflare/resources.md`. The plan doc is
`docs/plans/2026-10-02-001-feat-cloudflare-hosted-observatory-plan.md`.

## What the hosted build cannot do

Launch runs, start threads, flag stories, cancel jobs, estimate cost,
render `shot.png` captures — every mutating or live-state endpoint
returns an explicit 501 pointing back at `harness.py`. If a view looks
stale, run `sync --push`; if it's still stale, `sync --verify` will say
which side drifted.
