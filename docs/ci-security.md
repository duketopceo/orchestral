# CI secret containment

This repo has two workflows with different trust properties. Keep them apart.

| Workflow | Trigger | Reaches a secret | Runs whose code |
| --- | --- | --- | --- |
| `ci.yml` | PR and push to `main` | No | The PR head |
| `orchestral.yml` | `workflow_dispatch` only, through the `paid-eval` environment | `OPENROUTER_API_KEY`, on one step | The default branch, always |

`tests/test_workflow_security.py` enforces that table and runs in `ci.yml`, so
the shape cannot come back silently. Read the failure message as the rule.

## Why the paid eval is not pull-request triggered

`orchestral.yml` used to run on `pull_request` and skip forks. Fork-skipping is
not a boundary here. Any account that can push a branch to this repository can
open a pull request whose code then executes in a job holding the provider key
and a `pull-requests: write` token. The `MAX_COST_USD` cap limits spend; it does
nothing to stop exfiltration, so it was never a containment control.

The fix is not a better `if:` condition. It is removing the path: the paid eval
is manual, environment-gated, and checks out the default branch rather than the
dispatched ref. The key-bearing job therefore never executes unreviewed PR code.

The cost of that choice: a dispatched run always reports on `main`, not on the
branch you dispatched from. To evaluate a branch, land it first, or run the
harness locally with the key in your shell.

## One-time human action required

`environment: paid-eval` is only a gate once the environment has reviewers.

1. Create the `paid-eval` environment: repo Settings, Environments, New environment.
2. Add required reviewers to it, at least one person who is not the dispatcher.
3. Restrict it to the `main` branch, so a dispatch cannot be pointed at an
   unmerged ref by accident.

Until step 2 is done, the environment reference is a label, not a control.

## When to rotate `OPENROUTER_API_KEY`

Rotate the OpenRouter key when any of these is true. Each one means unreviewed
or unknown code may have executed with the key in scope.

- A pull-request-triggered workflow existed on `main` at any point, and was
  merged. This is the DUK-184 case: the exposure window ran from the commit that
  added the `pull_request` trigger, `7c1e221` (2026-09-12), until this fix
  landed. Treat the key as exposed for that whole window and rotate it.
- A fork or outside contributor opened a pull request during that window.
- A workflow, action, or pinned commit SHA referenced a secret and was changed by
  someone whose commit was never independently validated.
- A `pull-requests: write` or `contents: write` token appeared in a job that also
  read a secret.
- Provider-side usage looks unfamiliar: spend above `MAX_COST_USD`, or requests
  from a model slug the harness does not configure.

Rotation is: create a new key in the OpenRouter dashboard, update the
`OPENROUTER_API_KEY` repository secret, then revoke the old key. Revoking last
means a failed update does not leave the eval unable to run.

## Invariant to preserve

A pull-request-triggered workflow must never reference a `secrets.` context. If
a future change needs one, the shape has to change first: run the job on
`workflow_dispatch` against the default branch, or move the work behind an
environment with required reviewers. `test_no_pull_request_triggered_workflow_references_secrets`
fails the moment that rule is broken, in every workflow, not just this one.
