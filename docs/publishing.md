# Publishing results

`orchestral scrub` turns local `runs/` into a shareable `runs-pub/` tree plus a
`manifest.json` index — the intended way to publish example eval results.

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

- **Eyeball the output.** Patterns cover common shapes; your custom env vars or
  internal URLs may not match. Extend `PATTERNS` in `orchestral/privacy.py` for
  your own sensitive data.
- **Binary metadata passes through.** PNG `tEXt`/`iTXt` chunks and EXIF data
  are not scrubbed. If your image pipeline embeds prompts or paths in metadata,
  strip it first (`exiftool -all= runs-pub/**/*.png`).
- **`runs-pub/` is gitignored** in this repo — publish it deliberately, to
  wherever the results should live.
- **Malformed JSON** degrades to text-mode scrubbing rather than aborting, so a
  truncated `run.json` can't silently skip the rest of the tree.
