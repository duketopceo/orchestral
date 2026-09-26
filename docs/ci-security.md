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
a per-job env var and the trigger was unbounded. The check reports spend after the
fact; it does nothing to stop exfiltration, so it was never a containment
control.

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

## The dispatch path ships closed

The `eval` job carries `if: vars.PAID_EVAL_ENABLED == 'true'`, and the repository
does not set that variable. **A dispatch today runs nothing and reaches nothing.**

This is deliberate, and it is not in the workflow for decoration. GitHub creates a
referenced environment that does not exist, with no protection rules. So shipping
`environment: paid-eval` on its own would have been a gate that gates nothing: any
dispatcher would run any branch's code with `OPENROUTER_API_KEY` and nobody would
be asked. Every agent in this company dispatches as the same write-access GitHub
identity, so "only trusted people can dispatch" is not a property this repository
has. Defaulting closed costs the pre-merge eval until a human opens it, and that
is the correct trade while the gate is missing.

`test_paid_eval_job_is_closed_by_default` fails if that `if:` is dropped.

## One-time human action required

`environment: paid-eval` is only a gate once the environment has reviewers. As of
this commit the repository has **no environments at all**, so the reference gates
nothing. The CTO's integration token is refused `403` on
`repos/duketopceo/orchestral/environments`, so this cannot be done from an agent.

Do these in order. The order matters: step 3 without step 2 opens an unapproved
key-bearing path.

1. Create the `paid-eval` environment: repo Settings, Environments, New environment.
2. Add required reviewers to it, at least one person who is not the dispatcher.
   Turn on "prevent self-review" so a dispatcher cannot approve their own ref.
3. Set the repository variable `PAID_EVAL_ENABLED` to `true`: repo Settings,
   Secrets and variables, Actions, New repository variable. This is what opens
   the `if:` above. Do it only after step 2.

### The deployment branch rule, precisely

The environment's branch policy is matched against the ref the **workflow was
dispatched from** (`github.ref`), **not** against `inputs.ref`. These are two
different things and confusing them produces a failure that looks like the feature
is broken.

- Dispatch the workflow from `main`, and pass the branch you want measured in the
  `ref` input. That is the supported flow.
- Do **not** set a `main`-only deployment branch rule. It is tempting, because
  `main` is the safe default, and it is wrong: it blocks the run whenever anyone
  dispatches from a feature branch, and nothing reports the reason. The
  pre-merge eval then fails silently every time.
- "All branches" is correct, given `inputs.ref` is what selects the evaluated code
  and a required reviewer is what gates it.

The repo is public, so required reviewers are available on the current plan.

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

`MAX_COST_USD` is not a cap and not a budget. It is a post-run threshold. The eval
has already been paid for by the time the step reads `runs/index.db`; the step
reports the figure and fails the job. That is an alarm, not a limit. Nothing in
this workflow prevents a run from overspending, and nothing in the old workflow
did either: `MAX_COST_USD` was a per-job env var under an unbounded trigger,
which is why 161 runs were possible. Repository-level spend is
[DUK-212](/DUK/issues/DUK-212)'s question to answer.

The guard was, until this change, an observability defect on top of that: both
`echo` lines escaped their variables, so a passing run logged the literal text
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

Every check here scans both `.yml` and `.yaml`, because Actions runs both. A
check that globs one extension lets a workflow reach a secret by renaming its
file. `_workflow_files()` is the single place that knows this; do not reintroduce
a bare `glob("*.yml")`.

A secret-bearing job must be closed by default, so that merging the fix cannot
open a path the environment is not yet protecting. `test_paid_eval_job_is_closed_by_default`
fails if that guard is dropped.
