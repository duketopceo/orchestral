"""Prepare a run directory for publication: redact credentials, withhold the answer key.

`scrub` is two gates, and only one of them is about secrets.

**Credential redaction.** API keys, emails, phone numbers, local filesystem
paths, provider endpoint credentials, and internal hostnames are redacted in
text, JSON, and JSONL. Extend `PATTERNS` with shapes specific to your data.

**Answer-key withholding.** The answer key does not survive a publish. A
benchmark whose key is public has already answered itself: the next run measures
retrieval, not reasoning. So the publish path drops the graded-answer keys listed
in `GRADED_KEYS` at every depth of every published JSON and JSONL file, and
replaces each `llm_call` message body in `events.jsonl` with a size-only record.
The published ledger keeps what makes a run worth reading — cost, latency, token
counts, model identity, the provider's usage block — and drops the prompt (which
carries the task text) and the completion (which is the candidate's answer, and
is already published as `artifact.*`). Every withheld field is named in the run's
manifest entry under `scrub_withheld`; nothing is dropped silently.

The stance is one-way and there is no flag to turn it off. The reason is in
`docs/publishing.md`: keyed hashes were considered and rejected, because the
per-field booleans the reports already carry verify an answer without revealing
it, so a hash buys no verification and costs a key to distribute.

What stays published on purpose: the candidate's own output, the score, the
pass/fail verdict, per-check and per-field booleans, and the row counts. Those
describe what the model did. The key describes what the right answer was.

Approved media and font artifacts are copied byte-for-byte — they contain no
scrubbable text, but embedded metadata (PNG `tEXt`/`iTXt` chunks, EXIF) passes
through verbatim, so strip it with `exiftool` if your toolchain writes it.
Archives, databases, and unknown binary content are blocked because they cannot
be safely scrubbed. Embedded media metadata still requires manual review before
publication.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

# Extend this list with patterns that match your own sensitive data.
PATTERNS = {
    "api_key": re.compile(
        r"\b(?:"
        r"sk-or-[a-zA-Z0-9_-]{24,}"          # OpenRouter
        r"|sk-ant-[a-zA-Z0-9_-]{20,}"        # Anthropic
        r"|sk-proj-[a-zA-Z0-9_-]{20,}"       # OpenAI project
        r"|sk-[a-zA-Z0-9_-]{20,}"            # generic sk-* (OpenAI, etc.)
        r"|gsk_[a-zA-Z0-9_-]{20,}"           # Groq
        r"|xai-[a-zA-Z0-9_-]{20,}"           # xAI
        r"|AIza[a-zA-Z0-9_-]{30,}"           # Google
        r"|ghp_[a-zA-Z0-9]{30,}"             # GitHub PAT
        r"|github_pat_[a-zA-Z0-9_]{30,}"     # GitHub fine-grained PAT
        r"|AKIA[0-9A-Z]{16}"                 # AWS access key
        r"|Bearer\s+[a-zA-Z0-9_-]{16,}"
        r")\b"
    ),
    "private_key": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    "url_auth": re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@"),
    "internal_host": re.compile(
        r"\b[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.(?:internal|corp|lan|local|home|intranet)\b"
    ),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "mac_path": re.compile(r"/Users/[^\s\"'<>]+"),
    "home_path": re.compile(r"/home/[^\s\"'<>]+"),
    "win_path": re.compile(r"\b[A-Za-z]:\\Users\\[^\s\"'<>]+"),
    "ip": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

# Filenames the scrubber will copy out of a run directory. Anything else a user
# dropped into a run dir (notes, .env, scratch files) stays behind.
ALLOWED_NAMES = {
    "run.json",
    "events.jsonl",
    "plan.json",
    "cost.json",
    "report.json",
    "metrics.json",
    "manifest.json",
}
ALLOWED_PREFIXES = ("worker-", "artifact.", "screenshot.", "judge")

# Extensions copied verbatim — binary formats carry no scrubbable text.
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".mp4", ".webm", ".mov",
    ".zip", ".gz", ".tar",
    ".woff", ".woff2", ".ttf", ".otf",
    ".db", ".sqlite", ".sqlite3",
}

# Archive formats are never published: redaction cannot see inside them, so a
# generated file could carry a secret straight into runs-pub/. They are
# omitted by default and the omission is recorded in the manifest.
ARCHIVE_EXTS = {".zip", ".gz", ".tar"}

# Filenames that are omitted from published output even though they are
# text-scrubbable: debug.jsonl is internal diagnostics (transport retries,
# poll internals, provider details) whose value is local, and raw/ dirs are
# never traversed (only files are iterated). Omissions are manifested.
OMIT_NAMES = {"debug.jsonl"}

# Per-file omission reasons for the manifest; archives get the generic reason.
_OMISSION_REASONS = {
    "debug.jsonl": "internal diagnostics — not part of the published record",
    "raw/": "unredacted raw provider payloads",
}

# JSON keys whose values are the benchmark's answer key, dropped from every
# published JSON and JSONL file at every depth. No part of a run file uses any
# of these names for anything else, so the strip cannot cost evidence.
#
# `expected_preview` earned its place the hard way: `sqlexec.run_sql_check`
# wrote the reference rows under that name until this gate landed, so every
# pre-existing run in a `runs/` tree still carries it. `expected` and
# `expected_answer` are the canonical task-spec answer keys, and
# `reference_sql` and `required_content` are the canonical reference-solution
# keys. Nothing emits those two today, which is the point — they are here for
# the writer that has not been written yet.
#
# The report writers already omit the values (see `extract.check_extraction`,
# `sqlexec.run_sql_check`, `apistub.check_api`), so on a current run this set
# finds almost nothing. It is the gate for runs written by an older harness and
# for any writer that slips past those three, not the primary defence.
GRADED_KEYS = frozenset({
    "expected",
    "expected_answer",
    "expected_preview",
    "reference_sql",
    "required_content",
})

# Answer-key names that are NOT unique, so they are only stripped inside a task
# `metadata` object, where the name can only be a task-spec key.
#
# `calls` is the case that matters: it is the api task's expected request plan
# (`metadata.calls`), and it is also the llm-call count in `metrics.json` and
# `cost.json`. Stripping it everywhere deleted the per-phase call counts out of
# a real run's published metrics to protect against a leak that has no path —
# `check_api` reports `calls_expected` and `calls_made` as counts, and task
# metadata is never written into a run file. A gate that destroys measurement
# evidence is a worse failure than a leak someone can grep for, so the narrow
# scope is the deliberate trade.
GRADED_METADATA_KEYS = frozenset({
    "calls",
})

_GRADED_KEY_REASON = "graded answer key — not publishable"
_CALL_BODY_REASON = (
    "llm_call prompt and completion text — carries the task prompt and the "
    "candidate's response; cost, latency, token counts and model identity are kept"
)


class HoldoutRunError(RuntimeError):
    """Raised when a single-run scrub is asked to publish a holdout run."""


def _is_binary(path: Path) -> bool:
    """Binary if a known binary extension or a NUL byte in the first chunk."""
    if path.suffix.lower() in BINARY_EXTS:
        return True
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return False


def _is_allowed(name: str) -> bool:
    return name in ALLOWED_NAMES or name.startswith(ALLOWED_PREFIXES)


def run_is_holdout(src: Path) -> bool:
    """Does this run belong to the unpublished holdout arm?

    Answered from the run's own manifest, which records the flag the runner set
    from the task's `metadata.holdout`. A run whose manifest is missing or
    unreadable is *not* treated as holdout: withholding is the fail-closed
    direction, and a malformed manifest is already a reason not to publish the
    run's contents on trust.
    """
    manifest = src / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("holdout"))


def _withhold_reason() -> str:
    # The withheld entry still names the run (id, task id, models, cost) so the
    # published record shows how much evidence was held back and a reader can
    # reconcile it against a local run. Those fields describe the measurement,
    # not the problem: a task id is a slot name and carries no task content, and
    # the prompt and key are what stay unpublished.
    return (
        "holdout arm — this run's task text and answer key must not be published, "
        "because a published key turns every future instance of the problem into a "
        "published problem. This entry names the run for reconciliation; it carries "
        "no task text."
    )


def scrub_text(text: str) -> str:
    """Redact any known patterns in a plain string."""
    for name, pattern in PATTERNS.items():
        if name == "url_auth":
            text = pattern.sub(r"\1[REDACTED_AUTH]@", text)
        else:
            def _redact(m: re.Match[str], n: str = name) -> str:
                return f"[REDACTED_{n}]"
            text = pattern.sub(_redact, text)
    return text


def scrub_dict(obj: Any) -> Any:
    """Recursively scrub all strings in a JSON-serializable structure."""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        return {k: scrub_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_dict(i) for i in obj]
    return obj


def withhold_graded_keys(
    obj: Any, found: set[str] | None = None, in_metadata: bool = False
) -> Any:
    """Drop answer-key keys at every depth. Returns the reduced object.

    `GRADED_KEYS` are dropped wherever they appear. `GRADED_METADATA_KEYS` are
    dropped only inside a `metadata` object, because those names are also used
    for measurement outside one (see `metrics.json`).

    `found` collects the key names that were removed so the caller can record
    them in the manifest. It is a set of names, not of values: the point of the
    manifest entry is to say what was taken out, and a value would republish it.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            nested = in_metadata or key == "metadata"
            if key in GRADED_KEYS or (nested and key in GRADED_METADATA_KEYS):
                if found is not None:
                    found.add(key)
                continue
            out[key] = withhold_graded_keys(value, found, nested)
        return out
    if isinstance(obj, list):
        return [withhold_graded_keys(item, found, in_metadata) for item in obj]
    return obj


def _withhold_call_body(event: dict[str, Any]) -> bool:
    """Strip an `llm_call` event's message bodies, keeping the measurement.

    Returns True when something was withheld. The published event keeps cost,
    latency, token counts, model identity, the provider's usage block and the
    response id; it drops `input.messages` (the task prompt) and
    `output.content` (the completion). The completion is not lost evidence — the
    candidate's own text is published as `artifact.*` — and the prompt is the
    task, which is the thing a published answer key contaminates.
    """
    withheld = False
    request = event.get("input")
    if isinstance(request, dict) and isinstance(request.get("messages"), list):
        messages = request.pop("messages")
        request["messages_withheld"] = {
            "count": len(messages),
            "chars": sum(
                len(str(m.get("content", ""))) for m in messages if isinstance(m, dict)
            ),
        }
        withheld = True
    response = event.get("output")
    if isinstance(response, dict) and isinstance(response.get("content"), str):
        response["content_withheld"] = {"chars": len(response.pop("content"))}
        withheld = True
    return withheld


def _prepare_event(event: Any, found: set[str]) -> tuple[Any, bool]:
    """One published `events.jsonl` record: keys withheld, call body withheld.

    Returns the record and whether an `llm_call` body was stripped from it.
    """
    event = withhold_graded_keys(event, found)
    stripped = (
        isinstance(event, dict)
        and event.get("type") == "llm_call"
        and _withhold_call_body(event)
    )
    return event, stripped


def _scrub_file(src: Path, dst: Path) -> dict[str, Any]:
    """Scrub one file to dst. Returns what was withheld from it.

    `withheld["graded_keys"]` names the answer-key keys dropped,
    `withheld["llm_call_bodies"]` counts the `llm_call` events whose prompt and
    completion were replaced by size-only records. Empty dict means nothing was
    withheld, which is the normal case for a run written by a current harness.
    """
    withheld: dict[str, Any] = {}
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _is_binary(src):
        shutil.copyfile(src, dst)
        return withheld
    found: set[str] = set()
    call_bodies = 0
    if src.suffix == ".jsonl":
        out_lines = []
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                out_lines.append(scrub_text(line))
            else:
                prepared, stripped = _prepare_event(data, found)
                call_bodies += int(stripped)
                out_lines.append(json.dumps(scrub_dict(prepared), default=str))
        dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    elif src.suffix == ".json":
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            dst.write_text(
                scrub_text(src.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8",
            )
        else:
            dst.write_text(
                json.dumps(scrub_dict(withhold_graded_keys(data, found)), indent=2, default=str),
                encoding="utf-8",
            )
    else:
        dst.write_text(scrub_text(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    if found:
        withheld["graded_keys"] = sorted(found)
    if call_bodies:
        withheld["llm_call_bodies"] = call_bodies
    return withheld


def _scrub_dir(src: Path, dst: Path) -> tuple[Path, list[str], list[dict[str, Any]]]:
    """Copy the allowlisted files of a run dir to dst, scrubbing text.

    Returns the destination, the names omitted because their contents cannot be
    redacted (archives), and the answer-key material withheld from the files that
    were published. Withheld and omitted are separate lists: an omitted file is
    absent from the published tree, a withheld field is a hole inside a file that
    was published, and a reader needs to be able to tell those apart.
    """
    omitted: list[str] = []
    withheld: list[dict[str, Any]] = []
    for f in src.iterdir():
        if f.is_file() and f.name in OMIT_NAMES:
            omitted.append(f.name)
            continue
        if f.is_dir() and f.name == "raw":
            omitted.append(f.name + "/")
            continue
        if f.is_file() and _is_allowed(f.name):
            if f.suffix.lower() in ARCHIVE_EXTS:
                omitted.append(f.name)
                continue
            found = _scrub_file(f, dst / f.name)
            if found:
                withheld.append({"file": f.name, **found})
    return dst, omitted, withheld


def scrub_run(src: Path, out_dir: Path) -> Path:
    """Scrub a single run directory and write it under out_dir.

    Archives are omitted (see `ARCHIVE_EXTS`); single-run scrubbing has no
    manifest to record that in, so use `scrub_all` when the omission or the
    withheld-field record matters.

    Raises `HoldoutRunError` for a holdout run rather than writing a partial
    directory. There is no subset of a holdout run that is safe to publish — the
    plan carries the prompt and the artifact can be the answer — so "scrub it
    but leave out the key" is not an option this scrubber can honour.
    """
    if run_is_holdout(src):
        raise HoldoutRunError(str(src))
    dst, _, _ = _scrub_dir(src, out_dir / src.name)
    return dst


def _manifest_entry(run_json: Path, runs_dir: Path, out_dir: Path) -> dict[str, Any]:
    rel = run_json.parent.relative_to(runs_dir)
    entry: dict[str, Any] = {"run": str(out_dir / rel)}
    try:
        meta = json.loads(run_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return entry
    for key in ("run_id", "orchestrator", "task_id", "worker", "status",
                "score", "passes", "total_cost_usd",
                "total_input_tokens", "total_output_tokens",
                "started_at", "finished_at"):
        if key in meta:
            entry[key] = meta[key]
    return entry


def scrub_all(runs_dir: Path = Path("runs"), out_dir: Path = Path("runs-pub")) -> list[Path]:
    """Scrub every run in runs_dir into out_dir and write a manifest.json.

    Holdout runs are withheld whole and recorded in the manifest as withheld, so
    the count of runs that were not published is visible in the published output
    rather than being a silent hole in it. Answer-key material withheld from a
    published file is recorded per file under `scrub_withheld`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    manifest: list[dict[str, Any]] = []
    warned: set[str] = set()
    withheld = 0
    for run_json in runs_dir.rglob("run.json"):
        src = run_json.parent
        rel = src.relative_to(runs_dir)
        dst = out_dir / rel
        entry = _manifest_entry(run_json, runs_dir, out_dir)
        if run_is_holdout(src):
            withheld += 1
            # `run` would dangle: nothing was written, so name the source instead.
            entry["run"] = None
            entry["withheld"] = {
                "reason": _withhold_reason(),
                "status": "holdout_not_published",
                "source_run": str(rel),
            }
            manifest.append(entry)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        _, omitted, withheld_fields = _scrub_dir(src, dst)
        if omitted:
            entry["scrub_omissions"] = [
                {"file": name, "reason": _OMISSION_REASONS.get(
                    name, "contents cannot be redacted")}
                for name in sorted(omitted)
            ]
            warned.add(str(rel))
        if withheld_fields:
            # Named, not silent: a reader of the published tree has to be able to
            # see that a file is missing fields, and which fields.
            entry["scrub_withheld"] = [
                {
                    "file": item["file"],
                    "reason": _GRADED_KEY_REASON if "graded_keys" in item else _CALL_BODY_REASON,
                    "status": "withheld",
                    **{k: v for k, v in item.items() if k != "file"},
                }
                for item in sorted(withheld_fields, key=lambda i: i["file"])
            ]
        manifest.append(entry)
        copied.append(dst)
    if warned:
        print(
            "warning: omitted files from "
            f"{len(warned)} run(s) ({', '.join(sorted(warned))}): archives cannot "
            "be redacted and debug output is internal-only. Multi-file run data "
            "needs inner-file redaction before it can ship.",
            file=sys.stderr,
        )
    if withheld:
        print(
            f"withheld: {withheld} holdout run(s) not published — their task text and "
            "answer key stay local. They are listed in manifest.json under `withheld`.",
            file=sys.stderr,
        )
    (out_dir / "manifest.json").write_text(
        json.dumps(scrub_dict(manifest), indent=2, default=str), encoding="utf-8"
    )
    return copied
