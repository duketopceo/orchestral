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
  snapshot.build_snapshot()          │
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

## Key tree

`orchestral/web/snapshot.py` is the only writer of `api/` keys; the Worker's
`infra/cloudflare/observatory/keys.js` accepts exactly those shapes and nothing
wider (a request `/api/<rest>` reads the object `api/<rest>.json`):

| shape | example |
| --- | --- |
| `<name>` | `meta`, `overview`, `runs`, `groups`, `matrix`, `leaderboard`, `flags`, `pairings`, `models-catalog`, `experiments`, `experiment.<name>`, `pairings.<group>`, `cards.<lens>`, `compare.<a>.<b>` |
| `run/<id>`, `run/<id>/live`, `run/<id>/evidence` | per-run payloads |
| `card/<kind>/<target>.<lens>` | one report card |

Every name inside a key is percent-encoded with the dot escaped as `%2E`
(`snapshot.enc`, `keyEnc` in `ui/js/data.js`). Each segment is `[A-Za-z0-9._~-]`
or `%XX`, never empty, never `.`/`..`, no `..` anywhere (also after decoding),
at most 300 characters; whole key at most 400, no leading slash. Anything else
is a 400 and never reaches R2. Ingest applies the same grammar plus
`runs/<id>/<file>` for scrubbed artifact files. Filtering, sorting and
pagination are client-side, so query strings never pick a key. `api/meta.json`
carries `mode: "hosted"`, `synced_at`, `source_commit` and every capability
false. `harness.py sync --push` sends the state tree in chunks with `meta.json`
last, so `synced_at` only advances after the data it describes landed.

Stale keys are not deleted: a group or experiment that disappears locally keeps
its old keys in R2 until removed by hand (`wrangler r2 object delete`).

## Asset version (`?v=`)

The local server substitutes `__V__` in `app.html` per request. The Worker
serves assets as stored, so `wrangler.toml` runs
`scripts/build-hosted-assets.py` as its `[build] command`: it copies `ui/` to
`infra/cloudflare/observatory/.build/ui` (gitignored) with `__V__` replaced by a
content hash of the tree, and `[assets]` serves that copy. `wrangler deploy`,
`wrangler dev` and `--dry-run` all run it, so there is no step to forget. Never
point `[assets] directory` at `ui/` directly.

## Local end to end (no Cloudflare)

```bash
BROWSER=1 scripts/bootstrap-venv.sh /tmp/orch-venv
node --test infra/cloudflare/observatory/test/keys.test.mjs
/tmp/orch-venv/bin/python scripts/e2e-hosted.py --shots <dir>
```

`e2e-hosted.py` builds the fixture corpus, renders what `sync --push` would send,
loads it into `wrangler dev --local` (Miniflare R2) through the test-only
`/__seed` route of `wrangler.e2e.toml`, and drives the SPA through every route.
It never logs in to or contacts Cloudflare.

## Redeploying the Worker

```bash
cd infra/cloudflare/observatory
export CLOUDFLARE_API_TOKEN=$(omaseal get Cloudflare_duketopceo Personal)
wrangler deploy --dry-run --outdir /tmp/obs-dry   # bundle check, no upload
wrangler deploy
# D1 schema (once, or after schema.sql changes):
wrangler d1 execute orchestral-runs --file schema.sql --remote
# then the data, keys and the new SPA together:
ORCHESTRAL_OBS_TOKEN=$(omaseal get cloudflare obs-ingest) python3 harness.py sync --push --all
ORCHESTRAL_OBS_TOKEN=$(omaseal get cloudflare obs-ingest) python3 harness.py sync --verify
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

## Error tracking

Browser errors on `obs.shippedit.dev` report to the project GlitchTip
(`errors.pacehq.io`, project `orchestral-observatory`) through
`ui/js/errors.js` — a ~90-line Sentry-envelope reporter, no SDK
dependency. It is silent everywhere else (local dev, clones), dedupes
per fingerprint, and caps at five events per session. The DSN key in
the source is ingest-only.

A scheduled workflow (`.github/workflows/error-triage.yml`, every
30 min) runs `scripts/glitchtip_to_issues.py`: it lists unresolved
GlitchTip issues, files a GitHub issue per new one (dedup marker
`<!-- glitchtip:<id> -->` in the body), and scrubs URLs, query strings,
and run-id-shaped tokens first — the repository is public while the
observatory is Access-private. Filing caps at ten issues per run. The
`GLITCHTIP_TOKEN` repo secret is a read-only GlitchTip API token
(omaseal `glitchtip/orchestral-ci-poller`).
