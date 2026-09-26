# Task spec schema

Tasks live in `tasks/*.yaml` (or nested dirs like `tasks/batch-100/`). Each file
is a single task.

```yaml
id: landing-page-coffee       # required; used in paths, filters, judge cache keys
type: html                    # required; see the type list below — all types in this doc are implemented
prompt: |                     # required; the task brief given to the orchestrator
  Build a landing page for a coffee subscription service.

validation: [html]            # optional; see the check catalog below
assets: []                    # optional; reserved for future file inputs
metadata: {}                  # optional free-form map (video tasks read generation params here)
```

## Fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | str | required | Unique across `tasks/`; becomes a path component (`runs/{orch}/{task}/{worker}/{run_id}/`) |
| `type` | str | required | `html`, `image`, `video`, `multi-file`, `code`, `constraint`, `needle`, `sql`, `extract`, `api` — all implemented; see per-type sections below |
| `prompt` | str | required | Full task brief; the orchestrator decomposes it into subtasks |
| `validation` | list[str] | `[]` | Check names; empty means the type's default set |
| `assets` | list[str] | `[]` | Reserved; not consumed by the runner yet |
| `metadata` | map | `{}` | Free-form; carried into run records. `video` tasks read `duration`, `resolution`, `aspect_ratio`, `generate_audio`, `seed`; `multi-file` tasks read `expected_paths`; `code` tasks read `module`, `tests`, `timeout_seconds`, `expected_paths`, plus quality bounds `max_code_lines`, `max_functions`, `max_complexity_lite`, `no_unsafe`, `no_external_deps`, `forbidden_patterns` |
| `metadata.holdout` | bool | `false` | Marks the spec as part of the unpublished holdout arm. See below. |

## The holdout arm

`metadata.holdout: true` marks a spec as belonging to the **unpublished** arm —
the problems used to estimate whether a score is inflated by having seen the
published ones. It is honoured in three places:

- **Leaderboard** (`report --leaderboard`) — holdout runs are left out of every
  rate, median, and cost figure, and the count of excluded runs is printed. A
  pairing with only holdout runs is listed separately as unranked rather than
  dropped, so a measured pairing is never mistaken for an unmeasured one.
- **Publication** (`scrub`) — a holdout run is withheld whole and recorded in
  the published manifest under `withheld`. Nothing is copied, because there is no
  safe subset: the plan carries the prompt and a `needle` artifact *is* the
  answer. `scrub_run` raises `HoldoutRunError` rather than write a partial dir.
- **Audit** (`audit`) — the presence of a holdout arm clears `no_holdout_arm`.

Do not commit holdout specs. A spec in git is a published problem wearing a
holdout label, which is the exact thing the arm exists to avoid. Generate the arm
into a run-scoped directory instead:

```bash
python harness.py holdout --out runs-holdout --count 8 --seed 4242
```

Generated specs span several question shapes rather than one template — an arm
built from a single shape is one problem counted many times, which is what
`near_duplicate_family` reports for the shipped `html-batch-*` specs. The task id
is a slot name and is the same across seeds; the prompt is not, so two seeds give
two different problems under one id.

Read the result with `python harness.py report --contamination`, which prints
mean score per arm per task type plus the gap, and the `n` behind each mean.

## Task types

- **`html`** — workers write markup fragments; the orchestrator assembles a
  single `artifact.html`. Optional `screenshot.png` via the `shots` extra.
- **`image`** — workers generate images via the provider's image API;
  the orchestrator picks the best; `artifact.png` is stored. Image tasks
  require the `openrouter` provider.
- **`video`** — workers generate videos via OpenRouter's asynchronous
  Videos API (submit job, poll to completion, download MP4); the orchestrator
  picks the best; `artifact.mp4` is stored. Video tasks require the
  `openrouter` provider. Generation parameters come from `metadata`:
  `duration` (seconds), `resolution`, `aspect_ratio`, `generate_audio`,
  `seed`. Video judging is not implemented — `--judge` is skipped and the
  score stays null.
- **`multi-file`** — workers return a *set* of files instead of one document,
  and the runner merges the sets into `artifact.zip`. Worker output is a JSON
  object:

  ```json
  {"files": [{"path": "index.html", "content": "<!doctype html>..."}], "notes": "optional"}
  ```

  Paths are **canonicalized** (percent escapes decoded, `\` normalized to `/`,
  leading `./` dropped, case-folded) and then validated: relative only, no
  `..`/absolute/UNC/drive paths, no reserved device names, no control
  characters, ASCII only, no residual `%`, no segment starting or ending with a
  dot or space, and no two paths that collide once case-folded. Canonicalizing
  means a worker's `Index.HTML` lands as `index.html` — declare
  `expected_paths` in the same lowercase form. Anything that cannot be reduced
  to a safe relative path fails the run rather than being renamed or dropped.
  Assembly is a deterministic merge by path — later
  subtasks win, and every overwrite is listed in `report.json` under
  `merge_conflicts`. The zip is byte-reproducible, so identical file sets hash
  identically. File *contents* stay inside the archive: run traces record
  paths, sizes, and hashes only. Declare the files the task must produce in
  `metadata.expected_paths` for the `has_paths` check.
- **`code`** — same file-set contract as `multi-file` (workers return
  `{"files": [...]}`, merged into `artifact.zip`), but validation executes
  hidden tests: the file set plus the task's `metadata.tests` (a unittest
  source string, never sent to workers) are materialized into a temp dir and
  run via `python -Es -m unittest` in a subprocess. `metadata.module` names
  the required file (default `solution.py`; also the `expected_paths`
  default). `metadata.timeout_seconds` caps execution (default 30). `passes`
  requires every expected file present *and* the suite green; `score` is the
  fraction of tests passed (0.0 when the suite crashes, errors on import, or
  times out — `None` only when the suite never ran). Replicates give pass@k.
  The subprocess runs `-Es` with a stripped environment in a fresh temp dir —
  containment, not a security sandbox: generated code still runs with your OS
  privileges, so only pair trusted models with this task type. Dry runs skip
  execution and compile-check `.py` files instead (`executed: false`).
- **`constraint`** — workers produce candidate text per subtask; the
  orchestrator picks the best (same selection flow as `image`/`video`); the
  chosen text is stored as `artifact.txt` and checked against hard
  constraints: size budgets, required tokens, forbidden tokens, and regex
  patterns — all deterministic, no judge. The constraint checks live in the
  generic validator and are composable onto any text task. Optional
  `metadata.reference_text` is a compliant example: dry runs feed it through
  the pipeline so `--dry-run` proves the spec is self-consistent. See
  `tasks/constraint-product-blurb.yaml`.
- **`needle`** — long-context retrieval: `metadata.document` (the haystack)
  rides inside each subtask payload, workers return candidate answers, the
  orchestrator picks one, and `artifact.txt` is checked with the constraint
  checks — `has_required` for `metadata.required` (the true needle) and
  `no_forbidden` for `metadata.forbidden` (decoys). An answer that names the
  right token but also mentions a decoy fails — the checks measure whether
  the model actually found it, not whether it can recite the options.
  `metadata.expected_answer` feeds the dry-run path. See
  `tasks/needle-deploy-token.yaml`.
- **`sql`** — workers produce candidate SQL queries; the orchestrator picks
  one (same candidate-selection flow as `image`/`video`); the harness executes
  the chosen query **read-only** against a fixture SQLite database built from
  `metadata.schema` + `metadata.seed` and compares its result to
  `metadata.reference_sql`. The selected query is stored as `artifact.sql`.

  ```yaml
  metadata:
    ordered: true          # row order must match; default is multiset compare
    schema: [...]          # CREATE TABLE statements (list or one string)
    seed: [...]            # INSERT statements (list or one string)
    reference_sql: |       # the expected answer — defines truth
      SELECT ...
  ```

  Score is `1.0` when the candidate's rows match the reference's, `0.0` when
  the query runs but returns wrong rows or fails (including write attempts —
  the connection is `mode=ro`), and null when the task spec itself is broken
  or the worker produced no SQL. A progress-handler step cap aborts runaway
  queries. This is deterministic validation, not a sandbox: candidate SQL
  executes on this machine, so only run sql tasks against workers you trust
  not to produce hostile queries (read-only mode blocks writes, but queries
  can still burn CPU until the step cap trips). `validation:` entries are
  unused — the check set is fixed (`executed`, `matches_reference`). See
  `tasks/sql-monthly-revenue.yaml`.
- **`extract`** — workers extract a JSON object per subtask; the orchestrator
  picks the best candidate (same selection flow as `image`/`video`); the
  chosen extraction is stored as `artifact.json` and graded deterministically.
  The contract lives in `metadata`:

  ```yaml
  metadata:
    fields:               # schema-lite: presence, type, enum
      name: {type: str, required: true}
      tier: {type: str, enum: [gold, silver, bronze]}
    expected:             # deep-equality graded keys — defines truth
      name: "Ada"
    pass_threshold: 1.0   # min score to pass; required+type checks always apply
  ```

  Score is the fraction of `expected` keys that match (partial credit);
  with no `expected`, score is 1.0 when all `fields` checks pass. `passes`
  requires every `required` field present, all type/enum checks green, and
  `score >= pass_threshold`. Unparseable artifacts score null. See
  `tasks/extract-invoice.yaml`.
- **`api`** — workers produce a JSON *request plan* per subtask (a list of
  `{method, path, json?, params?}` calls); the orchestrator picks the best;
  the harness starts a real loopback `http.server` stubbed from
  `metadata.stub`, replays the chosen plan over real HTTP, and the stub
  records what actually arrived. The plan is stored as `artifact.json`.

  ```yaml
  metadata:
    strict: true           # unexpected calls fail the run (default true)
    stub:                  # canned routes the local server answers
      - {method: GET, path: /users/42, status: 200, json: {id: 42}}
    calls:                 # the expected request sequence — defines truth
      - {method: GET, path: /users/42}
      - {method: POST, path: /orders, json: {user_id: 42}}
      - {method: GET, path: /orders, params: {user_id: 42}}
  ```

  Matching is per-call: method + path always, `json` body and query `params`
  when declared. Score is the fraction of expected calls that arrived
  correctly; `passes` requires every expected call and — under `strict` —
  no unexpected ones. The stub binds 127.0.0.1 on an ephemeral port and is
  shut down before validation returns; nothing leaves the loopback
  interface. `validation:` entries are unused. See
  `tasks/api-order-lookup.yaml`.

  Every run also records a `report["quality"]` block — static analysis of the
  generated file set, measured unconditionally so pairings can be compared on
  lean-ness and safety even when no bound is declared:

  | Field | Meaning |
  |---|---|
  | `files` / `total_lines` / `code_lines` | file count; lines; non-blank non-comment lines |
  | `functions` / `max_function_lines` | function count; longest function span |
  | `complexity_lite` | lightweight branch count (AST if/for/while/except/assert/boolop/comprehension — not cyclomatic complexity) |
  | `imports` / `external_imports` | all imported roots; those outside the stdlib |
  | `unsafe_hits` | builtin unsafe-pattern hits (eval/exec, os.system, subprocess, pickle/marshal loads, `__import__`, ctypes, raw sockets, shell-out, hard deletes, hardcoded secrets) with file/line |
  | `unparseable` | `.py` files that failed `ast.parse` |
  | `violations` | declared bounds that were exceeded |

  Bounds are **opt-in gates** — declared in `metadata`, each producing a
  violation that fails the run even when tests pass:

  | Metadata key | Violates when |
  |---|---|
  | `max_code_lines` | `code_lines` exceeds it (bloat cap) |
  | `max_functions` | `functions` exceeds it |
  | `max_complexity_lite` | `complexity_lite` exceeds it |
  | `no_unsafe` | any builtin unsafe-pattern hit |
  | `no_external_deps` | any non-stdlib import |
  | `forbidden_patterns` | list of regexes — any hit always violates |

  Caveats: regex-based unsafe detection has false positives *and* negatives;
  a clean `unsafe_hits` is not proof of safety. `complexity_lite` and
  `code_lines` are lean-ness proxies, not performance measurements. The
  `code-*` task specs form a difficulty ladder (fizzbuzz → slugify →
  lru-cache → expr-parser) so a pairing's breakpoint shows up as the first
  task where `score` drops below 1.0 or `passes` flips false.

## Validation checks

`html` tasks (default set: `html_parses`, `non_empty`, `has_title`):

| Check | Passes when |
|---|---|
| `html` | shorthand; expands to `html_parses` + `non_empty` |
| `html_parses` | the artifact parses without HTML errors |
| `non_empty` | the artifact is non-blank |
| `has_title` | it contains `<title>` |
| `has_cta` | it contains a call-to-action token (cta, sign up, subscribe, get started, buy now, learn more) |
| `has_form` | it contains `<form` |
| `has_viewport` | it has a viewport meta tag |
| `no_placeholder` | no lorem ipsum / placeholder / TODO text |

`image` tasks (default set: `non_empty`, `png_signature`):

| Check | Passes when |
|---|---|
| `non_empty` | the artifact has bytes |
| `png_signature` | it has PNG magic bytes and an IEND trailer |

`video` tasks (default set: `non_empty`, `mp4_signature`):

| Check | Passes when |
|---|---|
| `non_empty` | the artifact has bytes |
| `mp4_signature` | the first box is `ftyp` (ISO-BMFF container check; does not verify codecs or playability) |

`multi-file` tasks (default set: `non_empty`, `zip_signature`):

| Check | Passes when |
|---|---|
| `non_empty` | the artifact has bytes |
| `zip_signature` | the archive opens as a zip |
| `has_paths` | every path in `metadata.expected_paths` is present as a non-empty regular file |
| `has_content` | every token in `metadata.required_content[path]` appears in that file's body (case-insensitive); scoped per path, so one file cannot vouch for another |

`code` tasks ignore `validation:` — the check is execution:

| Check | Passes when |
|---|---|
| `expected_paths` | `metadata.module` (and any declared `expected_paths`) are present non-empty |
| `quality_ok` | no declared quality bound was violated |
| `compiles` | (dry-run only) every `.py` file compiles |
| `tests_pass` | `python -Es -m unittest task_tests` exits 0 with ≥1 test run |
Constraint checks — for `constraint` tasks, or composable onto any text task.
Each fails closed when requested but its metadata key is missing:

| Check | Passes when | Metadata |
|---|---|---|
| `within_budget` | every declared bound holds | `min_chars`, `max_chars`, `min_words`, `max_words` |
| `has_required` | every token appears (case-insensitive) | `required: [...]` |
| `no_forbidden` | no token appears (case-insensitive) | `forbidden: [...]` |
| `matches_pattern` | the regex matches | `pattern` |
| `no_pattern` | the regex does not match | `forbidden_pattern` |

Unknown check names fail the run — including in a list that also contains known
checks. This holds for `html`, `constraint`, `needle`, `image`, `video`, and
`multi-file`. The `code`, `sql`, `extract`, and `api` types never read
`validation:` at all: they compute a fixed check set from `metadata`, so
anything declared there is a phantom gate and the run still reports a pass.
`python harness.py audit --strict` is the gate for that case and for the first —
it fails CI on a spec that asks for a check which cannot run. See
[docs/task-audit.md](task-audit.md).

## Example

```yaml
id: landing-page-coffee
type: html
prompt: |
  Build a landing page for "BrewLoop", a coffee subscription service.
  Include a hero, pricing tiers, and a signup form.
validation: [html, has_cta, has_form, has_viewport, no_placeholder]
```

A multi-file task declares the files it expects, so `has_paths` can check the
archive against the brief. `has_paths` alone accepts a one-byte file per name,
so a spec that wants the artifact graded on content adds `has_content` and maps
each path to the tokens that file must contain:

```yaml
id: multi-file-site
type: multi-file
prompt: |
  Build a small static site: a landing page and the stylesheet it depends on.
validation: [non_empty, zip_signature, has_paths, has_content]
metadata:
  expected_paths: [index.html, style.css]
  required_content:
    index.html: [hero, pricing, email]
    style.css: [pricing, form]
```
