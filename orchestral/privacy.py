"""Scrub sensitive data from a run before it is shared or published.

This is intentionally conservative: it redacts API keys, emails, phone numbers,
and local filesystem paths. You can add patterns specific to your data.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any


# Extend this list with patterns that match your own sensitive data.
PATTERNS = {
    "api_key": re.compile(r"\b(?:sk-or-[a-zA-Z0-9_-]{24,}|Bearer\s+[a-zA-Z0-9_-]{16,})\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "mac_path": re.compile(r"/Users/[^\s\"'<>]+"),
    "home_path": re.compile(r"/home/[^\s\"'<>]+"),
    "ip": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


def scrub_text(text: str) -> str:
    """Redact any known patterns in a plain string."""
    for name, pattern in PATTERNS.items():
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
        data = json.loads(src.read_text(encoding="utf-8"))
        dst.write_text(json.dumps(scrub_dict(data), indent=2, default=str), encoding="utf-8")
    else:
        dst.write_text(scrub_text(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


def scrub_run(src: Path, out_dir: Path) -> Path:
    """Scrub a single run directory and write it under out_dir."""
    out = out_dir / src.name
    for f in src.iterdir():
        if f.is_file():
            _scrub_file(f, out / f.name)
    return out


def scrub_all(runs_dir: Path = Path("runs"), out_dir: Path = Path("runs-pub")) -> list[Path]:
    """Scrub every run in runs_dir and write to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for subtask_dir in runs_dir.rglob("run.json"):
        src = subtask_dir.parent
        rel = src.relative_to(runs_dir)
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file():
                _scrub_file(f, dst / f.name)
        copied.append(dst)
    return copied
