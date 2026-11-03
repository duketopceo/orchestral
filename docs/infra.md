# Infrastructure

What this repository actually runs, where it runs, and how to check each piece.
The runbook for the hosted observatory is
[docs/hosted-observatory.md](hosted-observatory.md) — this page is the map, not
the manual. Provisioning detail for the original Cloudflare build-out (resource
IDs, verified-at-provisioning notes) is
[infra/cloudflare/resources.md](../infra/cloudflare/resources.md).

Last verified end to end: **2026-11-03**.

## The short version

| Piece | Runs where | Public? |
|---|---|---|
| Harness (`orchestral/`, `harness.py`) | your machine, as a Python package / CLI | source only |
| Eval data (`runs/`, `reports/`, `runs-pub/`) | your machine only — never committed, never synced except as scrubbed payloads | no |
| Hosted observatory | Cloudflare Worker + R2 + D1 on `obs.shippedit.dev` | behind Cloudflare Access |
| CI | GitHub Actions (`.github/workflows/ci.yml`) | logs are public |
| Paid eval | GitHub Actions, manual dispatch, environment-gated | logs are public |
| Error triage | GitHub Actions cron + GlitchTip | issues are public |

This repo is **not** on Pages, Railway, Vercel, Fly, or any PaaS. There is no
deploy-on-push: the Worker is deployed by hand with `wrangler`.

## Cloudflare (account `1661907b2d7e4a20800306e6a57844c5`)

| Resource | Name / ID | Role |
|---|---|---|
| Worker | `orchestral-observatory` | serves the SPA + JSON snapshots; no derivation logic |
| Custom domain | `obs.shippedit.dev` | `workers_dev = false`, plus a host check in code |
| R2 bucket | `orchestral-artifacts` | `api/*.json` snapshots and `runs/<id>/*` scrubbed files |
| D1 database | `orchestral-runs` (`0b6f162d-26a0-4428-ba4a-eaecac0a003a`) | reconciliation ledger only — not a read store |
| Access app | Orchestral Observatory (`4461aeb7-4d75-4110-b69a-8d9b07897210`) | gates the domain; allows humans, and the `obs-ingest` token as `non_identity` |
| Access service token | `obs-ingest` (`84a17dd7-fe66-49e2-98cb-033872d3c419`) | `/ingest/*` writes and authenticated reads |
| AI Gateway | `orchestral-eval` | inference telemetry: metadata + cost, `zdr`, no payload logs |

Two wrangler configs exist on purpose:
[`infra/cloudflare/observatory/wrangler.toml`](../infra/cloudflare/observatory/wrangler.toml)
is canonical (production deploys + local dev), and the repo-root
[`wrangler.toml`](../wrangler.toml) exists only because Workers Builds PR
previews resolve their config from the repository root. Keep the bindings in
step when either changes.

### Credentials (names only — values live in omaseal)

| omaseal entry | Used by |
|---|---|
| `cloudflare/obs-ingest` | `ORCHESTRAL_OBS_TOKEN` — `client_id:client_secret` for `sync --push` and authenticated reads |
| `cloudflare/orchestral-aig-run` | `CLOUDFLARE_AIG_TOKEN` — AI Gateway Run permission only |
| `Cloudflare_duketopceo Personal` | `CLOUDFLARE_API_TOKEN` for `wrangler deploy` / `d1 execute` |
| `openrouter/orchestral` | the eval spend key (`orchestral-eval-v3`, $50/mo cap). Not the harness key |
| `glitchtip/orchestral-ci-poller` | `GLITCHTIP_TOKEN` for the error-triage workflow (read-only) |

Never put any of these in a file in the repo, a command line argument, or a
run's environment. The scrubber redacts declared env values at capture time,
which is a backstop, not a licence.

## Deploy, sync, verify

```bash
# Worker (bundle check first, then upload) — runs scripts/build-hosted-assets.py
cd infra/cloudflare/observatory
export CLOUDFLARE_API_TOKEN=$(omaseal get Cloudflare_duketopceo Personal)
wrangler deploy --dry-run --outdir /tmp/obs-dry
wrangler deploy

# D1 schema — once, or after schema.sql changes. Run it from the observatory
# directory: schema.sql is resolved relative to the current directory.
wrangler d1 execute orchestral-runs --file schema.sql --remote

# data: snapshots + scrubbed run payloads. harness.py lives at the repo root,
# so come back before syncing or every command below is a missing-file error.
cd ../../..
export ORCHESTRAL_OBS_TOKEN=$(omaseal get cloudflare obs-ingest)
python3 harness.py sync                 # dry run: what is dirty
python3 harness.py sync --push          # push the dirty set
python3 harness.py sync --push --all    # full backfill / repair (--all alone is still a dry run)
python3 harness.py sync --verify        # local vs hosted run count + total cost
```

Publishing after each run is opt-in: `ORCH_CF_SYNC=1` (plus the token) makes a
finished run push itself. Active runs stay dirty until they reach a terminal
state; holdouts are skipped.

Rollback for the Worker is `wrangler rollback` from the same directory. R2
snapshots are not versioned by the harness — a bad snapshot is repaired by
fixing the local data and running `sync --push`, and a stale key that no longer
exists locally must be deleted by hand (`wrangler r2 object delete`).

## GitHub Actions

| Workflow | Trigger | Needs secrets | What it does |
|---|---|---|---|
| [`ci.yml`](../.github/workflows/ci.yml) | PR, push to `main` | no | `unittest discover`, `ruff check .`, `mypy`, plus `audit --strict` and `selfcheck --execute`; a non-matrix aggregate job named `test` reports the gates |
| [`orchestral.yml`](../.github/workflows/orchestral.yml) | `workflow_dispatch` only | yes | one paid eval run, environment-gated. Never reachable from a PR — see [docs/ci-security.md](ci-security.md), and `tests/test_workflow_security.py` fails if a PR trigger returns |
| [`error-triage.yml`](../.github/workflows/error-triage.yml) | cron every 30 min | yes | lists unresolved GlitchTip issues, files one GitHub issue per new one (dedup marker `<!-- glitchtip:<id> -->`) |

`main` protection: rulesets block deletion and non-fast-forward, and require the
status context `test`. `.github/required-checks.json` mirrors that rule and
`tests/test_required_check_contract.py` fails when the two drift. The merge
policy in [AGENTS.md](../AGENTS.md) (green CI plus an independent second agent's
check report) is the working practice; see that file for what the rules do and
do not enforce.

## Error tracking

The hosted SPA reports browser errors to GlitchTip at `errors.pacehq.io`
(project `orchestral-observatory`) through `ui/js/errors.js` — a small
Sentry-envelope reporter with no SDK dependency. It is silent outside the
hosted build, dedupes per fingerprint, and caps at five events per session.
`error-triage.yml` turns new ones into GitHub issues.

## Where the pieces live

| Question | File |
|---|---|
| how do I operate the hosted observatory day to day? | [docs/hosted-observatory.md](hosted-observatory.md) |
| what are the Cloudflare resource IDs and what was verified at provisioning? | [infra/cloudflare/resources.md](../infra/cloudflare/resources.md) |
| how does the Worker decide what a key may be? | `infra/cloudflare/observatory/keys.js` + `orchestral/web/snapshot.py` |
| what does `sync` send, and what is refused? | `orchestral/cf.py` |
| what may be published at all? | [docs/publishing.md](publishing.md) |
| why can CI change nothing about the eval key? | [docs/ci-security.md](ci-security.md) |
| what is the observatory's local-only equivalent? | `harness.py serve` |

## Verified state (2026-11-03)

- `https://obs.shippedit.dev/` returns the Cloudflare Access sign-in page
  unauthenticated, and `api/meta.json` / `api/runs.json` return 200 with the
  `obs-ingest` service token — the Worker, Access app, and R2 are all live.
- `api/meta.json` reports `mode: "hosted"`, `source_commit: 045f7c4`,
  `synced_at: 2026-11-01T01:25:54Z`, with every mutating capability `false`.
- `harness.py sync --verify` on this machine: local 1508 runs / $9.0887 vs
  hosted 1507 / $9.0885 — the one-run gap was an uncommitted local dry run,
  not drift.

## Known gaps

- The AI Gateway **spend-limit rule** is still unset in the dashboard. The
  harness `--budget` flag is the primary brake today.
- `api/meta.json`'s `synced_at` only advances when someone syncs; there is no
  scheduler, so a quiet week is expected and not a fault.
- Stale R2 keys survive group/experiment churn until deleted by hand.
