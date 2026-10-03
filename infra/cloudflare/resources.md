# Provisioned Cloudflare resources (2026-10-03)

Account `1661907b2d7e4a20800306e6a57844c5` (Duketopceo@gmail.com).

| Resource | Name / ID | Notes |
|---|---|---|
| R2 bucket | `orchestral-artifacts` | payload snapshots + scrubbed run trees |
| D1 database | `orchestral-runs` (`0b6f162d-26a0-4428-ba4a-eaecac0a003a`) | reconciliation ledger only — not the observatory read store |
| AI Gateway | `orchestral-eval` | `zdr: true`, `authentication: true`, `cache_ttl: 0`, `logpush: false`, `log_management` 10000 / STOP_INSERTING |
| Access app | Orchestral Observatory (`4461aeb7-4d75-4110-b69a-8d9b07897210`) | `obs.shippedit.dev`; policies: khan personal (allow emails), agent service token (bypass) |
| Access service token | `obs-ingest` (`84a17dd7-fe66-49e2-98cb-033872d3c419`) | secret in omaseal `cloudflare/obs-ingest` as `client_id:client_secret` |
| Scoped API token | `orchestral-aig-run` | omaseal `cloudflare/orchestral-aig-run`; AI Gateway Run permission only, used as `cf-aig-authorization` |

## Verified at provisioning

- `POST /compat/chat/completions` proxied to OpenRouter and logged with
  `metadata`, `cost`, tokens, and **empty `request`/`response` bodies**
  (zdr + `cf-aig-collect-log-payload: false` confirmed working).
- `POST /api/alpha/decisions` through the gateway returns
  `Invalid provider` (2008) — `decide()` cannot traverse the gateway and
  stays direct to OpenRouter, as planned.
- `User-Agent` must be set to a real product string: the default
  `python-urllib`/`python-httpx` UA is rejected with error 1010.

## Not yet provisioned

- Worker `orchestral-observatory` + custom domain `obs.shippedit.dev`
  (U3).
- Gateway spend limit rule — set in dashboard or via update call once a
  sane monthly figure is chosen; harness `--budget` is the primary brake.
