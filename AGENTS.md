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

Before committing, run all four CI gates in a throwaway venv built by
`scripts/bootstrap-venv.sh <dir>` — the same `.[dev,tui]` environment CI
installs. A shared `.venv` is mutated by every concurrent run, and a local run
that reports green is only meaningful if the `[tui]` extra was present.

```bash
scripts/bootstrap-venv.sh /tmp/gate-venv
/tmp/gate-venv/bin/python -m unittest discover -s tests
/tmp/gate-venv/bin/python -m ruff check .
/tmp/gate-venv/bin/python -m mypy orchestral harness.py
```

`python -m compileall` is not verification, and a skipped test is not a
passing test. To exercise a run end to end:

```bash
/tmp/gate-venv/bin/python harness.py init
/tmp/gate-venv/bin/python harness.py run --task landing-page-coffee --orchestrator deepseek/deepseek-v4-flash-0731 --worker z-ai/glm-5.3-flash --dry-run
/tmp/gate-venv/bin/python harness.py report --html
```

## Merge gate

The company reviews a merge by **green CI plus an independent second agent's
check report**. Your own green checks do not close it; one agent does not review
itself. That is the practice, and it is not yet what the repository enforces —
see below. A GitHub approving review is not obtainable here, because every agent
authenticates as the single `duketopceo` login and GitHub reads an agent review
of another agent's PR as a self-review:

```console
$ gh pr review <pr> --approve
failed to create review: GraphQL: Review Can not approve your own pull request
```

**Correction, measured 2026-09-27 (DUK-227).** An earlier version of this
section said an approving-review condition was still set on `main` and that
`REVIEW_REQUIRED`/`BLOCKED` on a green PR was that condition. **That was wrong,
and it was never verified.** It is replaced here with what the rules actually
are.

`main` has two active rulesets and no approving-review rule. Every rule that
applies to the branch, from `GET /repos/duketopceo/orchestral/rules/branches/main`:

| Rule | Setting |
|---|---|
| block deletion | ruleset `main-1` |
| block non-fast-forward | ruleset `main-1` |
| require status check | context `test`, from `github-actions`; `strict`; not enforced on PR creation |

Note that endpoint enumerates ruleset rules. It is readable where
`branches/main/protection` is not (403 from an integration-class token), and it
was missed for days because the unreadable endpoint was assumed to be the only
way in.

So a green PR reporting `BLOCKED` is **not** waiting on a reviewer. It is
waiting on a status check the workflow cannot produce: the `test` job is a
four-way matrix, GitHub reports matrix jobs as `test (3.11)` through
`test (3.14)`, and the required context is the bare name `test`. That context
is never emitted, so nothing merges.

```console
$ gh pr view <pr> --json reviewDecision,mergeStateStatus,mergeable
{"reviewDecision":"","mergeStateStatus":"BLOCKED","mergeable":"MERGEABLE"}
```

Empty `reviewDecision` on a PR with no reviews is the tell. GitHub reports
`REVIEW_REQUIRED` when a required review is unmet; an empty value means no rule
is asking for one. 40+ PRs have merged into `main` with zero reviews.

Two things follow, and both still hold:

- **Do not route around a block.** No `--admin`, no force-push, no weakening a
  check to get past it. Report the block and name who can clear it. That
  practice is unchanged and is not what caused this.
- **A branch-protection rule must be checked against what CI emits.** A required
  context no job can produce wedges the branch silently.
  `tests/test_required_check_contract.py` now fails if the two drift, and
  `.github/required-checks.json` records the contexts protection requires.
  Update that file in the same change that alters the rule.

Whether `main` *should* require an approving review is an open board decision,
not a repository fact. It is tracked in DUK-227 (card `852b1cdb`, `human_only`);
DUK-210 is closed. Do not assume either answer, and do not act on the
`REVIEW_REQUIRED` story this section used to tell.
