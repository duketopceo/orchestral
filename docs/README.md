# Docs index

What each document is for, and whether it is current. "Reference" means it
describes how the harness works today and should be updated with the code it
describes. Nothing here is a duplicate of the root files — for the operating
manual read [../AGENTS.md](../AGENTS.md), for direction read
[../ROADMAP.md](../ROADMAP.md).

## Start here

| Doc | What it is |
|---|---|
| [agent-quickstart.md](agent-quickstart.md) | self-contained walkthrough for an agent picking this repo up cold |
| [task-spec.md](task-spec.md) | the task spec format and every task type's grading contract |
| [infra.md](infra.md) | what runs where, credentials, deploy/sync/verify, verified state |
| [hosted-observatory.md](hosted-observatory.md) | runbook for `obs.shippedit.dev` |

## Reference

| Doc | Covers |
|---|---|
| [providers.md](providers.md) | non-OpenRouter providers via model `metadata` |
| [model-config.md](model-config.md) | model config fields, pricing, roles |
| [task-audit.md](task-audit.md) | what `harness.py audit` checks and why each rule exists |
| [publishing.md](publishing.md) | what may be published, how `scrub` withholds answer keys |
| [ci-security.md](ci-security.md) | why the paid eval workflow is dispatch-only and environment-gated |
| [v3-task-family.md](v3-task-family.md) | pinned real-repo fixtures, `verify.command`, verifier-owned oracles |
| [v3-curation.md](v3-curation.md) | how v3 tasks are chosen and what makes one admissible |
| [model-roster.yaml](model-roster.yaml) | the roster the model-config doc and catalog read from |

## Design notes and decisions

| Doc | Status |
|---|---|
| [tui-observability-design.md](tui-observability-design.md) | design record for the TUI's event stream |
| [solutions/validation-contracts.md](solutions/validation-contracts.md) | how validators fail closed |
| [solutions/publish-surface-evidence-contract.md](solutions/publish-surface-evidence-contract.md) | what a published card may claim |
| [analysis/code-census-2026-10.md](analysis/code-census-2026-10.md) | the baseline the negative-diff reduction program measures against |
| [analysis/dead-code-report-2026-10.json](analysis/dead-code-report-2026-10.json) | that program's first collection |

## Runbooks

| Doc | Use |
|---|---|
| [runbooks/tonight-ab.md](runbooks/tonight-ab.md) | launching and babysitting the Jev A/B experiment overnight |

## Explicitly historical — do not read as current

These are point-in-time audits and critiques kept for their reasoning. They
describe the repo as it was, not as it is.

- [audit-kimi-k3.md](audit-kimi-k3.md) — 2026-09-20 model audit.
- [spec-critique-gpt-5.6-sol.md](spec-critique-gpt-5.6-sol.md) — 2026-09-26 critique of the spec format.
- [research-ax-fastbrowse.md](research-ax-fastbrowse.md) — 2026-09-20 research note.
- [laya.md](laya.md) — 2026-09-20 notes on an external tool.
- [design/research/](design/research/) — design references.

## Plans

`plans/` holds dated design plans, one per feature: `YYYY-MM-DD-NNN-<type>-<slug>-plan.md`.
They are decision records, not status. The **program ledger** — which unit is in
progress, blocked, or done, with evidence — is
[plans/2026-10-05-001-docs-program-roadmap-plan.md](plans/2026-10-05-001-docs-program-roadmap-plan.md).
A plan whose feature has shipped is history: read it for why, not for what is
true now.
