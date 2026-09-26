"""Scrub sensitive data from a run before it is shared or published.

This is intentionally conservative: it redacts API keys, emails, phone numbers,
local filesystem paths, provider endpoint credentials, and internal hostnames.
You can add patterns specific to your data.

Publication is a fail-closed allow, not a deny list. A named media or font
format is copied byte-for-byte; everything else — archives, databases, unknown
binary, non-UTF-8 text, and anything whose leading bytes identify it as a
container — is withheld, and the withholding is recorded so a reader can see
which artifacts are missing and why. Signature checks are anchored at the start
of a file, so a renamed archive is still withheld, while an approved image that
happens to embed archive bytes is not. Embedded media metadata still requires
manual review before publication.
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

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".mp4", ".webm", ".mov",
    ".woff", ".woff2", ".ttf", ".otf",
}

ARCHIVE_EXTS = {
    ".zip", ".gz", ".tar", ".7z", ".bz2", ".xz", ".rar",
}

DATABASE_EXTS = {".db", ".sqlite", ".sqlite3", ".mdb", ".accdb", ".dbf"}

_ARCHIVE_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",
    b"\xfd7zXZ\x00",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
)
# bzip2 needs its own pattern: the bare `BZh` opening is three ASCII characters,
# and a plain-text log or transcript may legitimately begin with them. The real
# stream header is `BZh` + a 1-9 block-size digit + the `1AY&SY` magic, which no
# prose file starts with by accident.
_BZIP2_SIGNATURE = re.compile(rb"BZh[1-9]1AY&SY")
_SQLITE_SIGNATURE = b"SQLite format 3\x00"

# Filenames that are omitted from published output even though they are
# text-scrubbable: debug.jsonl is internal diagnostics (transport retries,
# poll internals, provider details) whose value is local, and raw/ dirs are
# never traversed (only files are iterated). Omissions are manifested.
OMIT_NAMES = {"debug.jsonl"}

_OMISSION_REASONS = {
    "debug.jsonl": "internal diagnostics — not part of the published record",
    "raw/": "unredacted raw provider payloads",
}


class _BlockedPublicationFile(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_allowed(name: str) -> bool:
    return name in ALLOWED_NAMES or name.startswith(ALLOWED_PREFIXES)


def _read_prefix(path: Path) -> bytes:
    if path.is_symlink():
        raise _BlockedPublicationFile("symbolic links are not approved for publication")
    try:
        with path.open("rb") as fh:
            return fh.read(8192)
    except OSError as exc:
        raise _BlockedPublicationFile("file could not be inspected") from exc


def _has_archive_signature(prefix: bytes) -> bool:
    """Does this file's leading bytes identify it as an archive container?

    Every check is anchored at offset 0 (tar's `ustar` at its fixed header
    offset 257). Nothing scans deeper into the file: an approved PNG may embed
    a ZIP local header in a chunk, and a substring scan would withhold it.
    """
    return (
        prefix.startswith(_ARCHIVE_SIGNATURES)
        or prefix[257:262] == b"ustar"
        or _BZIP2_SIGNATURE.match(prefix) is not None
    )


def _publication_block_reason(path: Path, prefix: bytes) -> str | None:
    suffix = path.suffix.lower()
    if suffix in ARCHIVE_EXTS:
        return "archive contents cannot be redacted and are not approved for publication"
    if _has_archive_signature(prefix):
        return "archive contents cannot be redacted and are not approved for publication"
    if suffix in DATABASE_EXTS or prefix.startswith(_SQLITE_SIGNATURE):
        return "database contents are not approved for publication"
    if suffix in BINARY_EXTS:
        return None
    if b"\0" in prefix:
        return "unknown binary content is not approved for publication"
    try:
        prefix.decode("utf-8")
    except UnicodeDecodeError:
        return "non-text content is not approved for publication"
    return None


def _remove_output(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


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


def _scrub_file(src: Path, dst: Path) -> None:
    prefix = _read_prefix(src)
    reason = _publication_block_reason(src, prefix)
    if reason:
        _remove_output(dst)
        raise _BlockedPublicationFile(reason)

    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in BINARY_EXTS:
        shutil.copyfile(src, dst)
        return

    try:
        text = src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _remove_output(dst)
        raise _BlockedPublicationFile("text content could not be safely scrubbed") from exc

    suffix = src.suffix.lower()
    if suffix == ".jsonl":
        out_lines = []
        for line in text.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                out_lines.append(scrub_text(line))
            else:
                out_lines.append(json.dumps(scrub_dict(data), default=str))
        dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    elif suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            dst.write_text(scrub_text(text), encoding="utf-8")
        else:
            dst.write_text(json.dumps(scrub_dict(data), indent=2, default=str), encoding="utf-8")
    else:
        dst.write_text(scrub_text(text), encoding="utf-8")


def _scrub_dir(src: Path, dst: Path) -> tuple[Path, list[dict[str, str]]]:
    if src.resolve() == dst.resolve():
        raise ValueError("scrub output must differ from the source run directory")
    _remove_output(dst)
    dst.mkdir(parents=True, exist_ok=True)
    omitted: list[dict[str, str]] = []
    for f in sorted(src.iterdir(), key=lambda path: path.name):
        if f.is_file() and f.name in OMIT_NAMES:
            _remove_output(dst / f.name)
            omitted.append({
                "file": f.name,
                "reason": _OMISSION_REASONS[f.name],
                "status": "omitted",
            })
            continue
        if f.is_dir() and f.name == "raw":
            _remove_output(dst / f.name)
            omitted.append({
                "file": f.name + "/",
                "reason": _OMISSION_REASONS["raw/"],
                "status": "omitted",
            })
            continue
        if f.is_file() and _is_allowed(f.name):
            try:
                _scrub_file(f, dst / f.name)
            except _BlockedPublicationFile as exc:
                omitted.append({"file": f.name, "reason": exc.reason, "status": "blocked"})
    return dst, omitted


def scrub_run(src: Path, out_dir: Path) -> Path:
    """Scrub a single run directory and write it under out_dir.

    A file that cannot be safely redacted is withheld rather than copied, the
    same fail-closed policy `scrub_all` applies. Single-run scrubbing has no
    manifest, so every withheld file is named on stderr: a file that vanishes
    without a record is an invisible hole in the published record. Use
    `scrub_all` when the reason for each withholding has to be preserved.
    """
    dst, omitted = _scrub_dir(src, out_dir / src.name)
    blocked = sorted(item["file"] for item in omitted if item["status"] == "blocked")
    if blocked:
        print(
            f"warning: {len(blocked)} file(s) withheld from {src.name} because their "
            f"contents cannot be redacted ({', '.join(blocked)}); use `orchestral "
            "scrub` to record the reason for each in a manifest.",
            file=sys.stderr,
        )
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
    """Scrub every run in runs_dir into out_dir and write a manifest.json."""
    source = runs_dir.resolve()
    destination = out_dir.resolve()
    if destination == source or source.is_relative_to(destination):
        raise ValueError("scrub output must not contain the source runs directory")
    _remove_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    manifest: list[dict[str, Any]] = []
    warned: set[str] = set()
    for run_json in runs_dir.rglob("run.json"):
        src = run_json.parent
        rel = src.relative_to(runs_dir)
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _, omitted = _scrub_dir(src, dst)
        entry = _manifest_entry(run_json, runs_dir, out_dir)
        entry["scrub_policy"] = "fail_closed"
        entry["publication_review"] = {
            "manual_inspection_required": True,
            "second_scanner_required": True,
            "status": "required",
        }
        if omitted:
            ordered_omissions = sorted(omitted, key=lambda item: item["file"])
            entry["scrub_omissions"] = ordered_omissions
            blocked = [item for item in ordered_omissions if item["status"] == "blocked"]
            if blocked:
                entry["scrub_blocked"] = blocked
            warned.add(str(rel))
        manifest.append(entry)
        copied.append(dst)
    if warned:
        print(
            "warning: publication review is required for omitted content in "
            f"{len(warned)} run(s) ({', '.join(sorted(warned))}); inspect the output "
            "manually and run a second scanner before publication.",
            file=sys.stderr,
        )
    (out_dir / "manifest.json").write_text(
        json.dumps(scrub_dict(manifest), indent=2, default=str), encoding="utf-8"
    )
    return copied
