---
title: "feat: Agent quickstart — a self-contained eval onboarding doc for agents"
type: feat
date: 2026-10-08
---

# feat: Agent quickstart — a self-contained eval onboarding doc for agents

## Summary

One agent-facing document — `docs/agent-quickstart.md` — that takes a
stranger's agent from "never heard of orchestral" to a finished judged run
with no prior context, modeled on OpenRouter's `spawn-ori-eval` skill
pattern. Plus a one-line pointer in `README.md` ("Tell your agent: fetch
this file") so the doc is reachable from any agent session, not just a
repo clone.

## Problem Frame

OpenRouter's Ori Eval solves cold-start the way agents actually onboard:
the landing page is one instruction — *tell your agent: fetch this URL and
follow it* — and the fetched document assumes zero context, walks a fixed
step list, and handles every branch the agent will hit (missing binary,
missing credential, existing state).

orchestral's docs are written for the repo owner, not a visiting agent:
`README.md` assumes you've decided to clone; `docs/task-spec.md` and
`docs/model-config.md` are references, not a path; `AGENTS.md` is a
contributor's gate list. An agent asked "evaluate these two orchestrator/
worker combos" currently has to assemble the path itself from four
documents — the exact friction a spawn-style doc removes.

This is the last uncovered cell of the project's own goal: *a stranger can
clone, install, write a task spec and model config from the docs alone,
and run an eval.* The docs exist; the *path* doesn't.

What this plan does NOT do (deliberately):

- **No new code.** `--dry-run` already provides the zero-key validation
  path; `init`, `run`, `report` already cover the journey.
- **No provenance work.** `manifest.json` already pins `harness_version`,
  `git_commit`, `python_version`, `platform`, `task_hash`, and
  `orchestrator_prompt_hash` — Ori's "score change means your agent
  changed" invariant is already structural here.

---

## Requirements

- R1. The doc assumes zero context: a reader who has never seen the repo,
  does not have a clone, and may or may not hold a provider key can finish
  the walkthrough.
- R2. Every command in it is verified — the plan's implementation walk is
  a cold-venv execution of the doc, literally, end to end.
- R3. It names the two honest paths up front: key-free (`--dry-run`,
  validates the harness, produces sample data) vs keyed (real run, real
  spend — with the expected cost of one run stated plainly).
- R4. It includes a minimal but complete task spec and model config
  inline (not just links), with links to `docs/task-spec.md` /
  `docs/model-config.md` for the full schema.
- R5. It teaches result-reading honestly: where `report.json`/
  `manifest.json`/`artifact.*` land, what `passed`/`score`/`inconclusive`
  mean, and that a `failed` run is an exception class to inspect, not a
  model verdict (the outcome-taxonomy vocabulary from
  `2026-10-08-002` applies when that ships).
- R6. It is fetchable standalone: plain markdown at a stable repo path,
  readable via `raw.githubusercontent.com`, no build step, no
  repo-context dependencies (no "read AGENTS.md first").
- R7. Frontmatter uses the agent-skill convention (`name:`,
  `description:`) so the file can also install as a personal skill
  without restructuring.

## Key Technical Decisions

- **One doc, not a skill directory.** The Ori pattern's real insight is
  the single self-contained fetch. A `skills/` package with references is
  over-structure for a doc that must survive copy-paste and plain
  `webfetch`. SKILL-convention frontmatter keeps the door open.
- **The walkthrough leads with dry-run.** A stranger's agent should
  verify the harness end-to-end before asking its human for a key —
  `run --dry-run` produces real artifacts from sample data, proving the
  install before spend is possible.
- **Steps mirror the Ori shape, scaled down:** ordered one-action steps
  (install → init → dry-run → write spec → write model config → real run
  → read the verdict), a short troubleshooting table at the end, and an
  explicit "what just happened" closing that names the evidence files.
  ~100 lines, not ~300.
- **README gets one line, not a section.** The pointer earns its place at
  the top of Install: `Agents: docs/agent-quickstart.md is a
  self-contained walkthrough you can fetch and follow verbatim.`

## Implementation Units

### U1. `docs/agent-quickstart.md` + README pointer + cold walk

- **Goal:** The doc exists, is fetchable, and every step in it is proven.
- **Requirements:** R1–R7
- **Files:** `docs/agent-quickstart.md` (new), `README.md` (one-line
  pointer)
- **Approach:** Write the doc to the contract above, then execute it
  cold: fresh venv (`scripts/bootstrap-venv.sh /tmp/aq-venv` or plain
  `pip install -e .` — whichever the doc claims), `orchestral init`,
  `orchestral run --dry-run` on a stock task, then the minimal spec +
  model config examples verified for schema validity
  (`orchestral validate` if it accepts a spec path — confirm flag during
  the walk; otherwise a real `--dry-run` against the example spec).
  Fix the doc where the cold walk lies.
- **Test scenarios:**
  - Doc's install + init + dry-run sequence completes in a fresh venv
    with no provider key.
  - The inline task-spec example loads (no schema error on a dry run).
  - The inline model-config example resolves (`--model` lookup or a
    dry-run naming it).
  - `webfetch`-style read of the raw file yields a complete document
    (no relative-render dependencies).
- **Verification:** cold walk reproduced twice — once authoring, once
  clean — with the second walk touching nothing but the doc's commands.

---

## Scope Boundaries

### Deferred to Follow-Up Work

- **Hosting at a vanity URL** (`obs.shippedit.dev/...` or a redirect) —
  the raw GitHub URL works today; a prettier URL is marketing, not
  function.
- **An interview-style `orchestral init --wizard` or spec generator** —
  Ori's differentiator is the agent *writing* the eval; ours is teaching
  the agent to write a task spec. A generator is a real feature idea for
  later; a doc is today's fix.
- **Publishing to agent-skill marketplaces** — frontmatter keeps it
  installable; distribution channels are a separate decision.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Doc drifts from CLI reality on the next flag change | R2's cold walk at write time; doc states the harness version it was verified against |
| Dry-run behavior differs from real-run paths the doc describes | The walk runs both paths where a key exists; keyed-path claims get verified against the real store's shape |
| Doc leaks internal-only workflows (omaseal, eval key) | Doc treats keys as generic `OPENROUTER_API_KEY` env — no personal infra references |

## Verification

Cold-venv end-to-end pass of the doc's commands (dry-run proven, keyed
path verified where a key is present), README pointer lands, file renders
standalone via raw URL. Standard gate: `ruff check .`, `mypy orchestral
harness.py`, `unittest discover -s tests` (doc-only change — gate is
hygiene, not coverage).

## Open Questions

- Does `orchestral validate` accept a spec path for the spec-check step,
  or is a `--dry-run` against the example spec the honest validation?
  Resolved during the cold walk — the doc states whichever is true.
- Should the doc mention cost expectations for a first real run
  (e.g. "~$0.01–0.05 for a simple task")? Include a conservative range
  derived from stored runs during the walk.
