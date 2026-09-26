# Publishing results

`orchestral scrub` makes a run shareable **as a result artifact, not as a
re-runnable one**: the answer key does not survive a publish. A published key
means the next run measures retrieval, not reasoning, so `scrub` drops the graded
answer (`metadata.expected`, `metadata.expected_answer`), the reference solution
(`metadata.reference_sql`, `metadata.required_content`) and the expected request
plan (`metadata.calls`) from every published JSON and JSONL file, and replaces
each `llm_call` message body in `events.jsonl` with a size-only record. What
stays is the score, the pass/fail verdict, the per-check and per-field booleans,
the row counts, the candidate's own output, and the cost, latency, token counts
and model identity behind them. There is no flag that turns this off, and there
is no keyed-hash alternative: the per-field booleans in a report already let a
grader confirm an answer was correct without learning it, so a hash would add a
key to distribute and buy nothing — while an unhashed low-entropy key like
`{"line_items": 3}` falls to a dictionary attack. The reasoning is in the
`orchestral/privacy.py` module docstring; the omissions and withheld fields are
named per run in `manifest.json`.

`scrub` also redacts credentials — API keys, emails, paths, hostnames — and that
is the half people usually mean when they say "scrubbed". Redaction alone is not
contamination control. Read both halves before you publish.

## Recipe

```bash
# 1. Run evals (data lands in runs/)
orchestral grid --task landing-page-coffee --jobs 4

# 2. Scrub into runs-pub/
orchestral scrub                      # or --runs-dir X --scrub-dir Y

# 3. INSPECT the output before publishing — scrubbing is conservative,
#    not exhaustive. Grep for anything you don't want public.
grep -rniE "key|token|secret|/home/|/Users/" runs-pub/ | less

# 4. Commit runs-pub/ wherever you want the results hosted
#    (a separate results repo, a gh-pages branch, a docs subtree — your call)
```

## Cheap release verification

Run these checks from the repository root after installing the development
dependencies. They use dry runs and do not require a provider API key.

```bash
python3 -m compileall orchestral harness.py
python3 harness.py init
python3 harness.py run \
  --task landing-page-coffee \
  --orchestrator deepseek/deepseek-v4-flash-0731 \
  --worker z-ai/glm-5.3-flash \
  --planner raw \
  --dry-run
python3 harness.py run \
  --task landing-page-coffee \
  --orchestrator deepseek/deepseek-v4-flash-0731 \
  --worker z-ai/glm-5.3-flash \
  --planner ce-plan \
  --dry-run
python3 harness.py report --html
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests
```

Record the command, exit status, and concise output for every check. Release
verification passes only when:

- compilation completes without errors;
- initialization finds or creates the run store;
- both planner commands exit successfully and record `dry_run: true` runs with
  their requested planner values;
- `reports/index.html` exists after HTML report generation; and
- pytest reports no failures.

These checks prove that the checkout runs. They do not prove that example
results are publishable. Publishing still requires an approved set of real runs,
a freshly generated `runs-pub/manifest.json`, review of `scrub_omissions`, and a
sensitive-data inspection of the complete scrubbed tree. Never use dry-run data
as release evidence.

## What scrub does

- **Withholds the answer key.** `GRADED_KEYS` in `orchestral/privacy.py` are
  dropped at every depth of every published JSON and JSONL file. `calls` is
  dropped only inside a `metadata` object, because that name is also the
  llm-call count in `metrics.json` and `cost.json`, and a gate that deletes
  measurement evidence is a worse failure than a leak. The report writers already
  omit these values, so this is the gate for runs written by an older harness.
- **Replaces `llm_call` bodies.** `input.messages` and `output.content` are
  dropped from published events; `messages_withheld` and `content_withheld`
  record the size that was removed. Cost, latency, token counts, model identity,
  the provider's usage block and the response id stay. The completion is not lost
  evidence — the candidate's own text is published as `artifact.*`. Other event
  types (`evaluation.completed`, `artifact.saved`, …) keep their payloads.
- **Names what it took.** Every withheld field is listed in the run's
  `manifest.json` entry under `scrub_withheld`, with the file, the key names, and
  the count of stripped call bodies. Omissions (`scrub_omissions`) and withheld
  fields are separate lists: an omitted file is absent from the tree, a withheld
  field is a hole inside a file that was published.
- **Redacts** in text/JSON/JSONL: OpenRouter/OpenAI/Anthropic/Groq/xAI/Google/
  GitHub/AWS-shaped keys, `Bearer` tokens, PEM private keys, URL userinfo
  (`https://user:pass@host`), internal hostnames (`.internal`, `.corp`, `.lan`,
  `.local`, `.home`, `.intranet`), emails, phone numbers, `/Users/…`,
  `/home/…`, `C:\Users\…` paths, and IPv4 addresses.
- **Archives are omitted**: `artifact.zip` and other archive members are
  skipped, because redaction cannot see inside an archive — a generated file
  could carry a secret straight into `runs-pub/`. Each omission is recorded in
  the run's manifest entry under `scrub_omissions`, with a warning naming the
  run. Publishing multi-file results needs inner-file redaction first.
- **Video runs**: `artifact.mp4`/`worker-*.mp4` copy verbatim; `events.jsonl`
  records the prompt and job id but never the video payload or job URLs.
- **Copies verbatim**: known binary artifacts (`.png`, `.mp4`, fonts,
  databases, or anything with NUL bytes) — except archives, which are omitted
  as described above.
- **Copies only allowlisted names**: `run.json`, `events.jsonl`, `plan.json`,
  `cost.json`, `report.json`, `metrics.json`, `worker-*`, `artifact.*`,
  `screenshot.*`,
  `judge*`. Random files you dropped into a run dir stay behind.
- **Writes `manifest.json`**: one entry per run with run_id, orchestrator,
  task_id, worker, status, score, passes, cost, token totals, and the
  `runs-pub` path — enough to build a gallery or results table.

## Caveats — read before publishing

- **A published prompt is still a published prompt.** Withholding the key stops
  a published artifact from *grading* a future run, but `plan.json` and
  `worker-*.json` still carry the orchestrator's subtask descriptions. For a task
  whose prompt contains its own answer — an extraction task over a quoted source
  document, for example — the published tree is a worked example and must be
  treated as a retired task, not a benchmark. Scrub cannot tell those tasks
  apart; you know which ones they are.
- **`reports/` is a different door.** `orchestral report --html` reads the local
  `runs/` tree, not `runs-pub/`, so the HTML report carries the full run record.
  The answer key is no longer in it (the report writers omit it), but it carries
  everything else. Publish `runs-pub/`, not `reports/`.
- **Eyeball the output.** Patterns cover common shapes; your custom env vars or
  internal URLs may not match. Extend `PATTERNS` in `orchestral/privacy.py` for
  your own sensitive data.
- **Binary metadata passes through.** PNG `tEXt`/`iTXt` chunks and EXIF data
  are not scrubbed. If your image pipeline embeds prompts or paths in metadata,
  strip it first (`exiftool -all= runs-pub/**/*.png`).
- **`runs-pub/` is gitignored** in this repo — publish it deliberately, to
  wherever the results should live.
- **Malformed JSON** degrades to text-mode scrubbing rather than aborting, so a
  truncated `run.json` can't silently skip the rest of the tree. A malformed JSON
  file gets no key-level withholding, because the file could not be parsed to
  find the keys. That is a reason to inspect the output, not a reason to trust
  it.

