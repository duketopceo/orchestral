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
- **Fails closed for opaque content**: archives (`.zip`, `.gz`, `.tar`, `.7z`,
  `.bz2`, `.xz`, and `.rar`), archive signatures even when renamed, database
  files (`.db`, `.sqlite`, `.sqlite3`, `.mdb`, `.accdb`, and `.dbf`), unknown
  binary data, and non-text data are omitted rather than copied. Each blocked
  file is listed in the run's `scrub_blocked` and `scrub_omissions` entries
  with a reason and `status: "blocked"`.
- **Copies approved media and fonts verbatim**: images, videos, and font files
  remain byte-identical. This is an explicit exception, not a general binary
  allowance. `scrub_run` uses this same policy as `orchestral scrub`; use
  `orchestral scrub` when the manifest evidence is required.
- **Copies only allowlisted names**: `run.json`, `events.jsonl`, `plan.json`,
  `cost.json`, `report.json`, `metrics.json`, `worker-*`, `artifact.*`,
  `screenshot.*`, and `judge*`. Random files you dropped into a run directory
  stay behind.
- **Writes `manifest.json`**: one entry per run with run_id, orchestrator,
  task_id, worker, status, score, passes, cost, token totals, and the
  `runs-pub` path. Every entry sets `scrub_policy` to `fail_closed` and records
  `publication_review.manual_inspection_required` and
  `publication_review.second_scanner_required`.

## Publication gate

Do not publish `runs-pub/` immediately after scrubbing.

1. Inspect the complete output, including allowed media metadata.
2. Run a second, independent scanner over the output.
3. Confirm the manifest's blocked-file list and review requirements are
   understood by the publisher.
4. Publish only after the review is complete and the output is stored at the
   intended destination.

A clean `scrub` result is not proof that the output is safe to publish. The
manifest makes the required review visible; it does not replace it. `scrub`
rebuilds its destination so a blocked file from an earlier run cannot remain in
the publication tree.

## Caveats — read before publishing

- **Patterns are not exhaustive.** They cover common credential, endpoint, and
  path shapes. Extend `PATTERNS` in `orchestral/privacy.py` for your own data.
- **Approved media metadata passes through.** PNG `tEXt`/`iTXt` chunks, EXIF
  data, and equivalent metadata in other media are not scrubbed. Strip metadata
  before review when your pipeline can write it.
- **Omitted content is not scanned.** Archives, databases, and unknown binary
  content are intentionally excluded because opening or rewriting opaque files
  can change them or expose their contents. Do not copy them into the
  publication tree by hand.
- **`runs-pub/` is gitignored** in this repo — publish it deliberately, to
  wherever the results should live.
- **Malformed JSON** degrades to text-mode scrubbing rather than aborting, so a
  truncated `run.json` cannot silently skip the rest of the tree. Unreadable or
  non-UTF-8 content is blocked instead.
