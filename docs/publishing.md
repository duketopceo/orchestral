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

## What scrub does

Publication is a fail-closed allow, not a deny list. A file is copied only if it
is allowlisted by name *and* its type is approved for verbatim copying;
everything else is withheld and recorded.

- **Redacts** in text/JSON/JSONL: OpenRouter/OpenAI/Anthropic/Groq/xAI/Google/
  GitHub/AWS-shaped keys, `Bearer` tokens, PEM private keys, URL userinfo
  (`https://user:pass@host`), internal hostnames (`.internal`, `.corp`, `.lan`,
  `.local`, `.home`, `.intranet`), emails, phone numbers, `/Users/…`,
  `/home/…`, `C:\Users\…` paths, and IPv4 addresses.
- **Fails closed for opaque content.** These are withheld rather than copied,
  because redaction cannot see inside them and a generated file could carry a
  secret straight into `runs-pub/`:
  - archives by extension — `.zip`, `.gz`, `.tar`, `.7z`, `.bz2`, `.xz`, `.rar`;
  - archives by content, so a renamed archive is still withheld — ZIP, gzip,
    bzip2, xz, 7z, RAR, and POSIX tar headers;
  - databases — `.db`, `.sqlite`, `.sqlite3`, `.mdb`, `.accdb`, `.dbf`, plus any
    file whose first bytes are the SQLite header;
  - unknown binary content (a NUL byte in the first 8 KiB) and non-UTF-8 text;
  - symbolic links.
- **Content checks are anchored at the start of a file.** An approved image
  that embeds archive bytes deeper in the file is still published; a container
  disguised with an approved extension is not. See the limitations below.
- **Copies approved media and fonts verbatim**: `.png`, `.jpg`, `.jpeg`, `.gif`,
  `.webp`, `.bmp`, `.ico`, `.mp4`, `.webm`, `.mov`, `.woff`, `.woff2`, `.ttf`,
  `.otf`. This is an explicit exception, not a general binary allowance.
- **Video runs**: `artifact.mp4`/`worker-*.mp4` copy verbatim; `events.jsonl`
  records the prompt and job id but never the video payload or job URLs.
- **Copies only allowlisted names**: `run.json`, `events.jsonl`, `plan.json`,
  `cost.json`, `report.json`, `metrics.json`, `manifest.json`, `worker-*`,
  `artifact.*`, `screenshot.*`, `judge*`. Random files you dropped into a run
  directory stay behind.
- **Writes `manifest.json`**: one entry per run with run_id, orchestrator,
  task_id, worker, status, score, passes, cost, token totals, and the
  `runs-pub` path — enough to build a gallery or results table. Every entry
  records `scrub_policy: "fail_closed"` and a `publication_review` block.
  Withheld files appear in that run's `scrub_omissions` with a reason and
  `status: "omitted"` (internal, e.g. `debug.jsonl`) or `status: "blocked"`
  (unapproved content); the blocked subset is repeated under `scrub_blocked`.
  A warning naming each affected run goes to stderr.
- **Clears the destination first.** `scrub` rebuilds its output tree, so a file
  that an earlier run published and this run withholds cannot survive in the
  publication tree. It refuses a destination that is, or contains, its source.

## Publication gate

Do not publish `runs-pub/` immediately after scrubbing.

1. Read `manifest.json` and account for every entry in `scrub_blocked` — each
   one is a run artifact that is *not* in the published tree.
2. Inspect the complete output, including allowed media metadata.
3. Run a second, independent scanner over the output.
4. Publish only after the review is complete and the output is stored where it
   is intended to live.

A clean `scrub` result is not proof that the output is safe to publish. The
manifest makes the required review visible; it does not replace it.

`orchestral scrub` writes the manifest. `orchestral.privacy.scrub_run` scrubs a
single run with the same policy but has nowhere to record reasons, so it names
each withheld file on stderr instead. Prefer `scrub` when the record matters.

## Caveats — read before publishing

- **Patterns are not exhaustive.** They cover common shapes; your custom env
  vars or internal URLs may not match. Extend `PATTERNS` in
  `orchestral/privacy.py` for your own sensitive data.
- **Approved media metadata passes through.** PNG `tEXt`/`iTXt` chunks and EXIF
  data are not scrubbed. If your image pipeline embeds prompts or paths in
  metadata, strip it first (`exiftool -all= runs-pub/**/*.png`).
- **Withheld content is not scanned.** Archives, databases, and unknown binary
  content are excluded precisely because opening or rewriting opaque files can
  expose or alter them. The withholding says "not scanned", not "clean". Do not
  copy them into the publication tree by hand.
- **A disguised container with a valid media header is not detected.** Content
  checks read the first 8 KiB. A file that starts with a real PNG or MP4 header
  and carries a ZIP payload after it is copied verbatim. Detecting that needs
  full format parsing, which this scrubber does not do. Treat the media allow
  as the trust boundary it is: only publish runs whose artifacts you produced.
- **Non-UTF-8 text is withheld, not mangled.** A log in a legacy encoding used
  to publish with replacement characters; it is now withheld, because
  redaction cannot be trusted over content it cannot read.
- **`runs-pub/` is gitignored** in this repo — publish it deliberately, to
  wherever the results should live.
- **Malformed JSON** degrades to text-mode scrubbing rather than aborting, so a
  truncated `run.json` cannot silently skip the rest of the tree.
