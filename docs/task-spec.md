# Task spec schema

Tasks live in `tasks/*.yaml` (or nested dirs like `tasks/batch-100/`). Each file
is a single task.

```yaml
id: landing-page-coffee       # required; used in paths, filters, judge cache keys
type: html                    # required; "html", "image", "video", "multi-file", "code" are implemented
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
| `type` | str | required | `html`, `image`, `video`, `multi-file`, `code` implemented; `api` is reserved/planned |
| `prompt` | str | required | Full task brief; the orchestrator decomposes it into subtasks |
| `validation` | list[str] | `[]` | Check names; empty means the type's default set |
| `assets` | list[str] | `[]` | Reserved; not consumed by the runner yet |
| `metadata` | map | `{}` | Free-form; carried into run records. `video` tasks read `duration`, `resolution`, `aspect_ratio`, `generate_audio`, `seed`; `multi-file` tasks read `expected_paths`; `code` tasks read `module`, `tests`, `timeout_seconds`, `expected_paths` |

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

`code` tasks ignore `validation:` — the check is execution:

| Check | Passes when |
|---|---|
| `expected_paths` | `metadata.module` (and any declared `expected_paths`) are present non-empty |
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
checks.

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
archive against the brief:

```yaml
id: multi-file-site
type: multi-file
prompt: |
  Build a small static site: a landing page and the stylesheet it depends on.
validation: [non_empty, zip_signature, has_paths]
metadata:
  expected_paths: [index.html, style.css]
```
