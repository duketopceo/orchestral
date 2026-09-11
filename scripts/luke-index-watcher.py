#!/usr/bin/env python3
"""
Luke Grounding Index Watcher

Regenerates INDEX.md from the live repo. Preserves manually-entered
"What it is", "Why it's here", and "Depends on" columns.

Modes:
  python scripts/luke-index-watcher.py          # generate INDEX.md
  python scripts/luke-index-watcher.py --check  # exit 1 if INDEX.md is stale
  python scripts/luke-index-watcher.py --install-hook  # add pre-commit hook

Temp / generated / vendored paths are ignored. See IGNORED_GLOBS.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPO_ROOT / "INDEX.md"
VERSION_PATH = REPO_ROOT / "VERSION"

# Per-file/dependency extraction ---------------------------------------------

IGNORED_GLOBS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".DS_Store",
    "*.tmp",
    "*.log",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "archive",
    ".codegraph",
    ".git/modules",
    ".git/objects",
    ".git/refs",
}

MAX_FILES_BEFORE_DELEGATE = 200  # leeway for enormous structures

COLUMNS = ["Path", "Version", "Last updated", "What it is", "Why it's here", "Depends on"]


def is_ignored(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    parts = set(rel.parts)
    if any(part in IGNORED_GLOBS for part in rel.parts):
        return True
    if any(part.startswith(".") and part in IGNORED_GLOBS for part in rel.parts):
        return True
    for ext in (".tmp", ".log", ".lock", ".pyc", ".pyo", ".class"):
        if path.name.endswith(ext):
            return True
    return False


def git_tracked_files() -> List[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    files = [REPO_ROOT / line for line in out.stdout.splitlines() if line]
    return [p for p in files if p.is_file() and not is_ignored(p)]


def build_file_mtime_index(targets: List[Path]) -> Dict[str, str]:
    """One git log pass: newest-first, assign first-seen commit date to each file."""
    rel_set = {str(p.relative_to(REPO_ROOT)) for p in targets}
    mtimes: Dict[str, str] = {}
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--name-only", "--format=%ct"],
        capture_output=True,
        text=True,
    )
    current_ts = ""
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            current_ts = datetime.datetime.fromtimestamp(int(line), tz=datetime.timezone.utc).isoformat()[:19]
            continue
        if line in rel_set and line not in mtimes:
            mtimes[line] = current_ts
        if len(mtimes) == len(rel_set):
            break
    return mtimes


def repo_version() -> str:
    if VERSION_PATH.exists():
        return VERSION_PATH.read_text().strip() or "1.0.0"
    return "1.0.0"


# Legacy header labels seen in older indexes -> current schema keys
LEGACY_HEADERS = {
    "File": "Path",
    "What It Does": "What it is",
    "Why It's There": "Why it's here",
    "Inter-File Dependencies": "Depends on",
}


def _split_row(line: str) -> List[str]:
    """Split a markdown table row on unescaped pipes; unescape \\| in cells."""
    parts = re.split(r"(?<!\\)\|", line)
    return [p.strip().replace("\\|", "|") for p in parts[1:-1]]


def parse_existing_index() -> Dict[str, Dict[str, str]]:
    """Parse the current INDEX.md for hand-maintained per-file metadata.

    Column mapping is driven by each table's own header row so legacy
    schemas ("File | What It Does | ...") still round-trip correctly.
    """
    existing: Dict[str, Dict[str, str]] = {}
    if not INDEX_PATH.exists():
        return existing

    # Collect tables as (header, data rows) pairs
    tables: List[Tuple[List[str], List[str]]] = []
    in_table = False
    block: List[str] = []
    for raw in INDEX_PATH.read_text().splitlines() + [""]:
        if raw.startswith("|") and " | " in raw:
            in_table = True
            block.append(raw)
        elif in_table:
            in_table = False
            if len(block) >= 2:
                tables.append((_split_row(block[0]), block[2:]))
            block = []

    for header, data_lines in tables:
        keys = [LEGACY_HEADERS.get(h, h) for h in header]
        for line in data_lines:
            cells = _split_row(line)
            if not cells or not cells[0]:
                continue
            mapping: Dict[str, str] = {}
            path = cells[0]
            for i, key in enumerate(keys):
                if key in ("Path", "File"):
                    if i == 0:
                        continue
                    path = cells[i] if i < len(cells) else path
                    continue
                if key == "Last updated":
                    continue
                if i < len(cells):
                    mapping[key] = cells[i]
            existing[path] = mapping
    return existing


def discover_files() -> List[Path]:
    files = git_tracked_files()
    files.sort()
    return files


def files_by_directory(files: List[Path]) -> Dict[Path, List[Path]]:
    buckets: Dict[Path, List[Path]] = {}
    for f in files:
        rel = f.relative_to(REPO_ROOT)
        if rel.parts:
            top = Path(rel.parts[0])
        else:
            top = Path(".")
        buckets.setdefault(top, []).append(f)
    return buckets


def humanize(cell: str, max_len: int = 80) -> str:
    cell = cell.replace("\n", " ").strip()
    if len(cell) > max_len:
        return cell[: max_len - 3] + "..."
    return cell


def build_index_content() -> str:
    version = repo_version()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    files = discover_files()
    existing = parse_existing_index()
    mtimes = build_file_mtime_index(files)

    # Map every file to its metadata
    rows: List[Tuple[str, str, str, str, str, str]] = []
    for f in files:
        rel = str(f.relative_to(REPO_ROOT))
        meta = existing.get(rel, {})
        last = mtimes.get(rel, "1970-01-01T00:00")
        what = humanize(meta.get("What it is", ""))
        why = humanize(meta.get("Why it's here", ""))
        depends = humanize(meta.get("Depends on", ""))
        rows.append((rel, version, last, what, why, depends))

    # Group by top-level directory
    top_dirs: Dict[str, List[Tuple[str, str, str, str, str, str]]] = {}
    for row in rows:
        parts = row[0].split("/")
        top = parts[0] if len(parts) > 1 else "."
        top_dirs.setdefault(top, []).append(row)

    out = [
        "# INDEX.md — Luke Grounding Index",
        "",
        f"> **Index version:** `v2.0.0`",
        f"> **Repo version:** `{version}`",
        f"> **Last synced:** `{now}`",
        "> **Schema:** `Path | Version | Last updated | What it is | Why it's here | Depends on`",
        ">",
        "> **This file is auto-generated by `scripts/luke-index-watcher.py`.**",
        "> Hand-maintained `What it is`, `Why it's here`, and `Depends on` values are preserved.",
        "",
        "---",
        "",
        "## 1. Security & Leak Risk",
        "",
        "> [!IMPORTANT]",
        "> Run `python scripts/luke-index-watcher.py` after any non-trivial repo change to keep this index current.",
        "> Never commit live keys, tokens, or PII. See `GUARDRAILS.md` §6 and `SECURITY_GUIDELINES.md`.",
        "",
        "---",
        "",
        "## 2. Root-level files",
        "",
        make_table([r for r in rows if "/" not in r[0]]),
        "",
    ]

    for top in sorted(top_dirs):
        if top == ".":
            continue
        entries = top_dirs[top]
        out.append(f"## 3. `{top}/`")
        out.append("")
        if len(entries) > MAX_FILES_BEFORE_DELEGATE:
            out.append(
                f"Directory `{top}/` contains {len(entries)} files. Listing the first 50; full drill-down is delegated to sub-indexes or `git ls-files {top}`."
            )
            out.append("")
            entries = entries[:50]
        out.append(make_table(entries))
        out.append("")

    return "\n".join(out) + "\n"


def make_table(rows: List[Tuple[str, str, str, str, str, str]]) -> str:
    if not rows:
        return "_No files in this section._"
    esc = lambda c: c.replace("|", "\\|")
    header = "| " + " | ".join(COLUMNS) + " |"
    sep = "|" + "|".join([" --- " for _ in COLUMNS]) + "|"
    body = ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    return "\n".join([header, sep] + body)


def generate() -> None:
    content = build_index_content()
    INDEX_PATH.write_text(content)
    print(f"Wrote {INDEX_PATH} ({len(content)} bytes)")


_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?")


def _normalize_for_check(content: str) -> str:
    """Strip volatile metadata so --check compares stable content only.

    The "Last synced" wall-clock line and per-file git timestamps always
    differ between the commit that generated the index and a later check,
    so they are excluded from equality. File presence and preserved
    metadata still must match.
    """
    lines = []
    for line in content.splitlines():
        if line.startswith("> **Last synced:**"):
            continue
        if line.lstrip().startswith("|"):
            line = _TS_RE.sub("<ts>", line)
        lines.append(line)
    return "\n".join(lines)


def check() -> bool:
    if not INDEX_PATH.exists():
        print("INDEX.md is missing. Run: python scripts/luke-index-watcher.py")
        return False
    generated = _normalize_for_check(build_index_content())
    existing = _normalize_for_check(INDEX_PATH.read_text())
    if generated == existing:
        print("INDEX.md is up to date.")
        return True
    print("INDEX.md is stale. Run: python scripts/luke-index-watcher.py")
    return False


def install_hook() -> None:
    # Resolve via git so worktrees (where .git is a file) work too
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-path", "hooks/pre-commit"],
        capture_output=True,
        text=True,
        check=True,
    )
    hook_path = Path(out.stdout.strip())
    if not hook_path.is_absolute():
        hook_path = REPO_ROOT / hook_path
    hook_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = "python3 scripts/luke-index-watcher.py && git add INDEX.md"
    if hook_path.exists():
        text = hook_path.read_text()
        if cmd in text:
            print(f"pre-commit hook already installed: {hook_path}")
            return
        lines = text.splitlines()
        # Insert before a terminal `exit` so our command isn't unreachable
        insert_at = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if not stripped:
                continue
            if stripped == "exit" or stripped.startswith("exit "):
                insert_at = i
            break
        lines.insert(insert_at, cmd)
        hook_path.write_text("\n".join(lines) + "\n")
    else:
        hook_path.write_text("#!/bin/sh\n" + cmd + "\n")
    os.chmod(hook_path, 0o755)
    print(f"Installed pre-commit hook: {hook_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Luke Grounding Index Watcher")
    parser.add_argument("--check", action="store_true", help="Exit 1 if INDEX.md is stale")
    parser.add_argument("--install-hook", action="store_true", help="Add pre-commit hook")
    args = parser.parse_args()

    if args.check:
        return 0 if check() else 1
    if args.install_hook:
        install_hook()
        return 0
    generate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
