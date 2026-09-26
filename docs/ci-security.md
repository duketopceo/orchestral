# CI secret containment

This repo has two workflows with different trust properties. Keep them apart.

| Workflow | Trigger | Reaches a secret | Runs whose code |
| --- | --- | --- | --- |
| `ci.yml` | PR and push to `main` | No | The PR head |
| `orchestral.yml` | `workflow_dispatch` only, through the `paid-eval` environment | `OPENROUTER_API_KEY`, on one step | The default branch, unless a dispatcher names a `ref` |

`tests/test_workflow_security.py` enforces that table and runs in `ci.yml`, so
the shape cannot come back silently. Read the failure message as the rule.

## Why the paid eval is not pull-request triggered

`orchestral.yml` used to run on `pull_request` and skip forks. Fork-skipping is
not a boundary here. Any account that can push a branch to this repository can
open a pull request whose code then executes in a job holding the provider key
and a `pull-requests: write` token. It also ran 161 times across 41 branches with
no approver and no way to bound repository-wide spend, because `MAX_COST_USD` is
a per-job env var and the trigger was unbounded. The cap limits spend; it does
nothing to stop exfiltration, so it was never a containment control.

The fix is not a better `if:` condition. It is removing the trigger. The paid eval
is manual and environment-gated.

## The `ref` input, and the risk it reintroduces

A dispatcher may name a `ref` to evaluate, so a branch can be measured before it
merges. That is the normal loop for an eval harness, so it is supported.

It does weaken the containment. Read this before you approve a dispatch.

- The `ref` input is optional and defaults to empty, which resolves to the
  default branch. **The safe target is what happens by default**; evaluating a
  branch takes a deliberate choice.
- With no `ref`, the key-bearing job cannot execute unreviewed PR code. That was
  a structural property, and it is gone by choice. What replaces it is the
  `paid-eval` required reviewer.
- **Approving a ref name is not a code review.** The approver sees a branch name
  and the run's actor. They do not see the diff. Anyone with write access can put
  arbitrary content on a branch with an innocent-looking name, and the approver
  cannot tell that from the approval dialog. The approver must open the diff
  before approving, and must not treat the approval prompt as a review.
- Only write-access holders can dispatch, and the environment blocks the job until
  a reviewer approves, so an outside contributor still cannot reach the key. The
  residual risk is an approval given on a name rather than on code.
- The `Record evaluated commit` step prints the resolved commit SHA and the
  requested ref before anything else runs. Use it to audit a run after the fact.
  A dispatch whose SHA you cannot account for is a rotation trigger below.
- A dispatcher input must reach the shell through `env:`, never interpolated into
  a `run:` line. Otherwise a crafted ref executes before any reviewer sees the
  run. `test_run_blocks_do_not_interpolate_workflow_inputs` enforces this.

## One-time human action required

`environment: paid-eval` is only a gate once the environment has reviewers. As of
this commit the repository has **no environments at all**, so the reference gates
nothing. The CTO's integration token is refused `403` on
`repos/duketopceo/orchestral/environments`, so this cannot be done from an agent.

1. Create the `paid-eval` environment: repo Settings, Environments, New environment.
2. Add required reviewers to it, at least one person who is not the dispatcher.
   Turn on "prevent self-review" so a dispatcher cannot approve their own ref.
3. Add a deployment branch rule. Do **not** restrict it to `main`: the `ref`
   input is the point of the feature, and a `main`-only rule would silently break
   every pre-merge eval.

The repo is public, so required reviewers are available on the current plan.
Until step 2 is done, the environment reference is a label, not a control, and
this workflow has no human gate on provider spend.

## When to rotate `OPENROUTER_API_KEY`

Rotate the OpenRouter key when any of these is true. Each one means unreviewed
or unknown code may have executed with the key in scope.

- A pull-request-triggered workflow existed on `main` at any point, and was
  merged. This is the DUK-184 case: the exposure window ran from the commit that
  added the `pull_request` trigger, `7c1e221` (2026-09-12), until this fix
  landed. Treat the key as exposed for that whole window and rotate it.
- A fork or outside contributor opened a pull request during that window.
- A dispatch named a `ref` and the approver cleared it without reading the diff.
  The commit SHA is in the run log; if you cannot account for a dispatched SHA,
  treat the key as exposed.
- A workflow, action, or pinned commit SHA referenced a secret and was changed by
  someone whose commit was never independently validated.
- A `pull-requests: write` or `contents: write` token appeared in a job that also
  read a secret.
- A dispatcher input was ever interpolated into a `run:` line rather than passed
  through `env:`.
- Provider-side usage looks unfamiliar: spend above `MAX_COST_USD`, or requests
  from a model slug the harness does not configure.

Rotation is: create a new key in the OpenRouter dashboard, update the
`OPENROUTER_API_KEY` repository secret, then revoke the old key. Revoking last
means a failed update does not leave the eval unable to run.

## Reading the cost guard

`MAX_COST_USD` is a per-run cap, not a budget. A cap on one job cannot bound a
repository with an unbounded number of dispatches, which is what made the old
push trigger expensive. Repository-level spend is [DUK-212](/DUK/issues/DUK-212)'s
question to answer.

The guard itself was, until this change, an observability defect: both `echo`
lines escaped their variables, so a passing run logged the literal text
`${MAX_COST_USD}` and a failing run logged the literal `${cost}`. The comparison
was always correct, and the cost was always recorded in the run and in
`harness.py`; the log just did not say so. `test_run_blocks_do_not_escape_variable_references`
now fails the moment a `run:` line escapes a variable again.

## Invariant to preserve

A pull-request-triggered workflow must never reference a `secrets.` context. If
a future change needs one, the shape has to change first: run the job on
`workflow_dispatch` behind an environment with required reviewers.
`test_no_pull_request_triggered_workflow_references_secrets` fails the moment
that rule is broken, in every workflow, not just this one.

A `workflow_dispatch` input must never be interpolated into a `run:` line. Pass
it through `env:`. `test_run_blocks_do_not_interpolate_workflow_inputs` fails the
moment that rule is broken.
