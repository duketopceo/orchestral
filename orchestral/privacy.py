"""Scrub sensitive data from a run before it is shared or published.

This is intentionally conservative: it redacts API keys, emails, phone numbers,
local filesystem paths, provider endpoint credentials, and internal hostnames.
You can add patterns specific to your data.

Binary artifacts (screenshots, generated images) are copied byte-for-byte —
they contain no scrubbable text, but note that embedded metadata (PNG tEXt
chunks, EXIF) passes through verbatim; strip it with exiftool before publishing
if your toolchain writes metadata.
"""

from __future__ import annotations

import json
import re
import shutil
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


def scrub_text(text: str) -> str:
    """Redact any known patterns in a plain string."""
    for name, pattern in PATTERNS.items():
        if name == "url_auth":
            text = pattern.sub(r"\1[REDACTED_AUTH]@", text)
        else:
            text = pattern.sub(lambda m: f"[REDACTED_{name}]", text)
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
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _is_binary(src):
        shutil.copyfile(src, dst)
        return
    if src.suffix == ".jsonl":
        out_lines = []
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                out_lines.append(scrub_text(line))
            else:
                out_lines.append(json.dumps(scrub_dict(data), default=str))
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
            dst.write_text(json.dumps(scrub_dict(data), indent=2, default=str), encoding="utf-8")
    else:
        dst.write_text(scrub_text(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


def _scrub_dir(src: Path, dst: Path) -> Path:
    """Copy the allowlisted files of a run dir to dst, scrubbing text."""
    for f in src.iterdir():
        if f.is_file() and _is_allowed(f.name):
            _scrub_file(f, dst / f.name)
    return dst


def scrub_run(src: Path, out_dir: Path) -> Path:
    """Scrub a single run directory and write it under out_dir."""
    return _scrub_dir(src, out_dir / src.name)


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
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    manifest: list[dict[str, Any]] = []
    for run_json in runs_dir.rglob("run.json"):
        src = run_json.parent
        rel = src.relative_to(runs_dir)
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _scrub_dir(src, dst)
        manifest.append(_manifest_entry(run_json, runs_dir, out_dir))
        copied.append(dst)
    (out_dir / "manifest.json").write_text(
        json.dumps(scrub_dict(manifest), indent=2, default=str), encoding="utf-8"
    )
    return copied
