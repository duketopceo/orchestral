# AGENTS.md — orchestral

This repo is intentionally isolated from personal agent tooling, LifeOS, custom
skills, and third-party MCPs. When you work here, use only the repo's own code
and commands.

## How to build and run

```bash
# run from the repo root
pip install -e .

python3 harness.py init
python3 harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --planner raw --dry-run
python3 harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --planner ce-plan --dry-run
python3 harness.py report
python3 harness.py report --html
python3 harness.py dashboard
python3 harness.py tui
python3 harness.py history
python3 harness.py ablate --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --sweep retry_limit=0,1,2 --dry-run
python3 harness.py scrub
```

## What to do

- Keep code in `orchestral/` and CLI in `harness.py`.
- Task specs go in `tasks/*.yaml`; model pricing/configs in `models/*.yaml`.
- Eval data goes to `runs/` (ignored by Git). Never commit `runs/`.
- Reports and scrubbed data go to `reports/` and `runs-pub/` (also ignored).
- Generated static reports live in `reports/`.

## What not to do

- Do not load or reference the user's LifeOS, TELOS, skills, or private rules.
- Do not add unrelated dependencies. Keep the stack: Python 3.11+,
  `pyyaml`, `httpx`, `rich`. Optional extras only: `textual` ([tui]),
  `playwright` ([shots]).
- Do not commit eval artifacts or API keys.

## Storage model

`runs/{orchestrator}/{task}/{worker}/{run_id}/` holds the full trace.
`runs/index.db` is the SQLite index for fast sorting and filtering.
Use `orchestral.storage.RunStore` for reads/writes and `orchestral.logger.EventLogger`
for per-action logging.

## Verification

Before committing, run:

```bash
python3 -m compileall orchestral harness.py
python3 harness.py init
python3 harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --dry-run
python3 harness.py report --html
```

## Merge gate

A merge needs **green CI plus an independent second agent's check report**. It does
not need a GitHub approving review, and no agent here can give one: every agent
authenticates to GitHub as the single `duketopceo` login, so GitHub reads any
agent reviewing another agent's PR as a self-review and refuses it.

```console
$ gh pr review 68 --approve
failed to create review: GraphQL: Review Can not approve your own pull request
```

The board adopted dropping the "require approving review" condition from `main`
while keeping branch protection and required status checks, so the second agent's
check report is the review. Your own green checks are not a substitute for it —
one agent does not close this gate.

**While that branch-protection change is still pending, expect this:**

```console
$ gh pr view 68 --json reviewDecision,mergeStateStatus,mergeable
{"reviewDecision":"REVIEW_REQUIRED","mergeStateStatus":"BLOCKED","mergeable":"MERGEABLE"}
```

`REVIEW_REQUIRED`/`BLOCKED` on a green, conflict-free PR is the known gate, not
a defect in the PR. The protection rule cannot be read or written from an
integration-class token (`branches/main/protection` returns 403), so no agent can
clear it. Do not work around the gate, do not add `--admin`, and do not force-push
around it.
