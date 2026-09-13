# Task spec schema

Tasks live in `tasks/*.yaml` (or nested dirs like `tasks/batch-100/`). Each file
is a single task.

```yaml
id: landing-page-coffee       # required; used in paths, filters, judge cache keys
type: html                    # required; "html", "image", and "video" are implemented
prompt: |                     # required; the task brief given to the orchestrator
  Build a landing page for a coffee subscription service.

validation: [html]            # optional; see the check catalog below
assets: []                    # optional; reserved for future file inputs
metadata: {}                  # optional free-form map (unused by the harness today)
```

## Fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | str | required | Unique across `tasks/`; becomes a path component (`runs/{orch}/{task}/{worker}/{run_id}/`) |
| `type` | str | required | `html`, `image`, `video` implemented; `api`, `multi-file` are reserved/planned |
| `prompt` | str | required | Full task brief; the orchestrator decomposes it into subtasks |
| `validation` | list[str] | `[]` | Check names; empty means the type's default set |
| `assets` | list[str] | `[]` | Reserved; not consumed by the runner yet |
| `metadata` | map | `{}` | Free-form; carried into run records. `video` tasks read `duration`, `resolution`, `aspect_ratio`, `generate_audio`, `seed` from here |

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

## Example

```yaml
id: landing-page-coffee
type: html
prompt: |
  Build a landing page for "BrewLoop", a coffee subscription service.
  Include a hero, pricing tiers, and a signup form.
validation: [html, has_cta, has_form, has_viewport, no_placeholder]
```
