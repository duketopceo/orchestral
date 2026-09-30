# Runbook — Jev A/B experiment run

Launch checklist for `harness.py experiment`. Steps are ordered so a
misconfiguration fails before any spend lands.

## 1. Environment contract

```bash
source scripts/cube-env.sh          # ORCHESTRAL_CODE_RUNTIME + E2B_* + SSL_CERT_FILE
export OPENROUTER_API_KEY="$(omaseal get openrouter orchestral)"
```

Assert key provenance — eval spend bills the dedicated orchestral key,
never the default:

```bash
# must print a value; if it prints nothing, the export above failed
test -n "$OPENROUTER_API_KEY" && echo "eval key loaded"
```

> **Do not** source `scripts/wave-a.sh` — its `keys.json` line exports the
> *default* OpenRouter key and would bill the wrong account.

For code-type cells (`code-fizzbuzz`, `v3-*`), also verify the sandbox
contract: `E2B_DOMAIN`, `E2B_API_KEY`, `SSL_CERT_FILE` must all be set
(the driver refuses code cells without them — it will not burn spend on
"no runtime" verdicts). Fixtures must be fetched first:

```bash
python harness.py fixtures check    # all rows 'ok'
```

## 2. Dry-run the plan

```bash
python harness.py experiment --matrix experiments/jev-ab.yaml \
    --budget 12.00 --daily-cap 15.00 --dry-run
```

Read the priced plan: per-cell est/pair, rep target, current state. Cells
with no billing history print `(calib)` — they run one calibration pair
then reprice from what actually billed.

## 3. Launch

```bash
python harness.py experiment --matrix experiments/jev-ab.yaml \
    --budget 12.00 --daily-cap 15.00 --seed 7 --jobs 2
```

- `--budget` sizes per-cell rep targets *and* aborts on the live
  calls-table meter — it is a real bound, not just a sizing hint.
- `--jobs` parallelizes cells only; replicates stay serial inside a cell
  so pair interleave and spend accounting hold.
- Safe to interrupt (`SIGINT`) and re-run the same command — `done` and
  `aborted` cells are skipped on resume.

## 4. Watch

- **Observatory:** `python harness.py serve` → overview renders the
  `Experiment — jev-ab` section (per-arm pass rates, diff CI, verdicts).
- **Ledger:** `python harness.py coverage --matrix experiments/jev-ab.yaml
  --budget 12.00` — same data in the terminal.
- **CubeOps:** http://127.0.0.1:12088 for sandbox fleet state during
  code-cell batches.

## 5. After the run

```bash
python harness.py coverage --matrix experiments/jev-ab.yaml --budget 12.00
python harness.py report --experiment experiments/jev-ab.yaml
python harness.py publish-mark --target <task:orch:worker> --url <post-url>
```

Brake stack, honestly stated: omaseal key cap ($50/mo, provider-side) +
`--daily-cap` (recorded spend) + `--budget` abort (live calls meter) +
omaseal key rotation is the nuclear option. Worst-case blind spend ≈ one
batch × `--jobs` × pair cost.
