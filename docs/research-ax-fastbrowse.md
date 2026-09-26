# Research: google/ax and the "fastbrowse" browser agent

Date: 2026-09-20. Question: could either project plug into orchestral as a task
type, a worker/model, or a comparison baseline?

## 1. google/ax

- Repo: https://github.com/google/ax — "Google's open agentic orchestrator"
- Go, Apache-2.0, ~3.7k stars, actively developed (warning banner: pre-stable,
  breaking changes expected).
- **It is not an eval framework.** Despite the name resembling eval tooling, ax
  is a Kubernetes-shaped *infrastructure* layer for running agent workloads at
  scale ("billions of tasks per cluster") on top of Agent Substrate.

### Architecture

Four declarative primitives, all `ax.io/v1alpha1` YAML applied via an
`ax apply` CLI (kubectl-shaped: get/describe/watch/ssh/suspend/resume):

| Primitive | What it does |
|---|---|
| `Task` | One isolated sandboxed execution: image, command, CPU/memory limits, env |
| `Workspace` | Pre-wired Git repos, MCP servers, skill packages; optional `goal` that an agent completes on first boot |
| `Gateway` | Network boundary: exposed listeners + egress host allowlist |
| `Model` | Named model config (provider, model id, params) with API key from a K8s secret |

Runs on a Kubernetes cluster (needs `ko`, a container registry, and an Agent
Substrate Control API). Tasks are cheap, suspendable/checkpointable, and
composable into trees (an agent spawns sub-tasks). Agents compose the
primitives; ax deliberately does not model orchestration logic itself.

### What it evaluates

Nothing. No judge, no scoring, no cost accounting beyond cluster resource
limits. It is the layer *under* agents, not a harness over them.

### Overlap with orchestral

None at the eval layer. Orchestral measures orchestrator×worker pairing
quality-per-dollar; ax is execution plumbing for where those agents run. The
closest framing: ax is what you'd reach for *after* orchestral tells you which
pairing wins and you want to run that winner at scale with network fencing and
checkpoints.

### Integration possibilities

- (a) Task type: **no** — wrong layer entirely; ax has no task/eval format.
- (b) Worker/model: **no** — ax doesn't expose models as endpoints; its `Model`
  resource is config for ax's own components, not a serving surface.
- (c) Baseline: **no** — nothing to compare against; incomparable units.
- (Speculative, not recommended) orchestral-as-a-Task: run `orchestral run` as
  an ax Task for CI-at-scale. Surface = `ax apply` YAML + gRPC control plane.
  Cost: a K8s cluster, registry, and the whole Agent Substrate dependency for
  what orchestral currently does with a subprocess. Skip unless orchestral
  ever needs cluster-scale parallel grids.

**Verdict: filed for awareness. No integration surface worth building.**

## 2. "fastbrowse" — identified

**Repo: https://github.com/agent-labs-dev/fastbrowse** (high confidence).

Identification evidence:

- Name matches exactly; GitHub search for `fastbrowse` surfaces it as the only
  browser-*agent* among the hits (others are file explorers, readers, apps).
- Description: "A fast browser agent: Jev picks each action from what is on the
  page, an LLM reads and plans, and every claim in an answer cites a quote from
  the page." The Jev choice-model ecosystem also appears in Luke's tooling
  (`jev-router` skill: "call Jev" for fast structured decisions), which is the
  half-remembered association.
- Fresh: created 2026-09-17, 43 stars, MIT, Python 3.13+, pre-alpha, pip
  package `fastbrowse`, runs via `uvx fastbrowse "<question>" --start <url>`.
  There is also a related `browser-use/jev-ultrafast` (same core technique,
  navigation-only) and `romaluev/jev-ego` / `jdorado/ez-fast-browser` in the
  same Jev orbit.

### What it is

A browser agent built on a different action-selection principle: instead of an
LLM generating each action from a screenshot, the page is indexed into
candidate controls and **Jev** (typesafe.ai choice model) *picks* one — a
classification per step, not a generation. An LLM plans and reads; code owns
verification, safety, and secrets (sign-ins never show a model the password).
Answers must cite verbatim quotes from the page.

### Its eval — this is the interesting part

fastbrowse ships its own benchmark (`docs/evals.md`): 14 answer tasks (lookups,
sign-ins, checkout, Google Flights), 3 passes each, vs Browser Use (hosted),
same cloud browser, same day, **uncapped** on both sides:

| | passed | median cost/task | median time |
|---|---|---|---|
| fastbrowse | 41/42 | $0.0057 | 21.0s |
| Browser Use (hosted) | 42/42 | $0.41 | 24.8s |

(71x cheaper, comparable reliability; gap grows with task complexity — 182x on
sign-ins.) Notably, the README publishes a correction of its own earlier
"2.9x more passes" claim, admitting the original $0.25 cost cap measured its
budget, not Browser Use's ability. That uncapped cost-per-pass methodology is
*exactly* orchestral's shape: fixed task suite, pass/fail, quality-per-dollar.

### Integration possibilities

- (a) **Task type — plausible, medium effort.** A `browser-answer` task type:
  workers receive a web question + start URL, return an answer with citations;
  grading could be quote-citation validation (deterministic-ish) plus judge.
  Caveats: (i) orchestral workers are OpenAI-compatible chat endpoints — a
  browser agent isn't, so this needs a worker-adapter (see b); (ii) live-web
  tasks are non-deterministic and flaky across runs — needs replicates and
  either pinned practice sites (fastbrowse's own suite uses saucedemo.com-style
  practice targets) or a local fixture server like the API-integration stub;
  (iii) subtask contract fits OK: orchestrator decomposes a research question
  into sub-queries, each worker browses, orchestrator assembles a cited answer.
- (b) **Worker/model — not directly.** fastbrowse is an agent stack (Jev +
  planner LLM), not a model, and not an OpenAI-compatible endpoint. Options:
  wrap it as a local HTTP shim exposing the chat-completions shape orchestral
  workers expect (fastbrowse is `uvx`-runnable with stdout/`--json` answers —
  a thin bridge); or add a non-LLM "tool worker" worker class to orchestral.
  Keys: `TYPESAFE_API_KEY` (Jev) + `OPENROUTER_API_KEY` (planner); no infra
  beyond Chrome or a `--cloud` hosted browser. Cost per task is pennies.
- (c) **Comparison baseline — best fit, lowest effort.** fastbrowse's published
  uncapped methodology is a prior-art benchmark for cost-per-pass agent evals:
  cite it in orchestral's docs as the web-task analog of the pairing grid. If a
  browser task type lands, fastbrowse vs Browser Use-hosted is the natural
  first two arms, scored by the same pass-rate × cost machinery (`report
  --leaderboard`, successes-per-dollar).

**Verdict: worth tracking. Best near-term value is (c) as a baseline/methodology
reference; (a) via pinned-site fixtures is a realistic future task type; (b)
needs a bridge and only makes sense after (a).**

## Summary table

| | google/ax | fastbrowse |
|---|---|---|
| What it is | K8s-style agent orchestrator infra | Choice-model browser agent |
| Eval/overlap | None — different layer | Ships a 14-task cost-per-pass suite |
| Task type | No | Possible (browser-answer, needs fixtures) |
| Worker/model | No | Possible via HTTP bridge, pennies/task |
| Baseline | No | Yes — strongest fit |
