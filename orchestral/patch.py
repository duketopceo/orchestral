"""Unified-diff task type — `swe-patch`: workers produce a patch, not files.

A swe-patch task ships a repo snapshot in `metadata.files` and a hidden
`metadata.tests` suite. The worker's artifact is a unified diff; the harness
applies it in memory (pure Python — no `patch` binary, no subprocess) over a
materialized copy of the repo, then runs the same unittest machinery as
`code`/`bugfix`.

`patch applies cleanly` is its own check — a model that can't produce valid
diffs fails before tests run, which is itself the metric (SWE-bench-style
work is diff-shaped, not file-set-shaped).

Contract: worker returns `{"patch": "<unified diff>"}`.
"""

from __future__ import annotations

import re
from typing import Any

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    """The diff is malformed or a hunk's context doesn't match the file."""


def _strip_prefix(path: str) -> str:
    """Drop a git-style a//b/ prefix; leave bare paths alone."""
    return path[2:] if path[:2] in ("a/", "b/") else path


def _parse_diff(diff: str) -> list[dict[str, Any]]:
    """Parse a unified diff into per-file hunk lists."""
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            current = {"old": _strip_prefix(line[4:].strip()), "hunks": []}
            hunk = None
        elif line.startswith("+++ ") and current is not None:
            current["new"] = _strip_prefix(line[4:].strip())
            files.append(current)
        elif line.startswith("@@"):
            if current is None:
                raise PatchError("hunk before any ---/+++ header")
            m = _HUNK_RE.match(line)
            if not m:
                raise PatchError(f"malformed hunk header: {line[:80]}")
            hunk = {"old_start": int(m.group(1)), "lines": []}
            current["hunks"].append(hunk)
        elif hunk is not None:
            if line.startswith((" ", "+", "-")):
                hunk["lines"].append(line)
            elif line == "\\ No newline at end of file":
                continue
            else:
                hunk = None
        # anything else (index/diff headers) is skipped
    return files


def apply_unified_diff(files: dict[str, str], diff: str) -> dict[str, str]:
    """Apply `diff` to `files` (path -> content); return a new dict."""
    out = dict(files)
    for f in _parse_diff(diff):
        path = f["new"]
        old_lines = (out.get(f["old"]) or "").splitlines()
        new_lines: list[str] = []
        pos = 0
        for hunk in f["hunks"]:
            # @@ -0,0 means "insert at top of a new/empty file"
            start = max(hunk["old_start"] - 1, 0)
            if start < pos or start > len(old_lines):
                raise PatchError(f"{path}: hunk at line {hunk['old_start']} out of range")
            new_lines.extend(old_lines[pos:start])
            pos = start
            for hl in hunk["lines"]:
                tag, text = hl[0], hl[1:]
                if tag == " ":
                    if pos >= len(old_lines) or old_lines[pos] != text:
                        got = old_lines[pos] if pos < len(old_lines) else "<eof>"
                        raise PatchError(f"{path}: context mismatch at line {pos + 1}: expected {text!r}, got {got!r}")
                    new_lines.append(text)
                    pos += 1
                elif tag == "-":
                    if pos >= len(old_lines) or old_lines[pos] != text:
                        got = old_lines[pos] if pos < len(old_lines) else "<eof>"
                        raise PatchError(f"{path}: delete mismatch at line {pos + 1}: expected {text!r}, got {got!r}")
                    pos += 1
                else:  # "+"
                    new_lines.append(text)
        new_lines.extend(old_lines[pos:])
        out[path] = "\n".join(new_lines) + ("\n" if new_lines else "")
        if f["old"] != path and f["old"] in out:
            del out[f["old"]]
    return out


def extract_patch(text: str) -> str | None:
    """Pull the diff out of worker output: {"patch": ...} or raw diff text."""
    import json

    raw = str(text or "")
    stripped = raw.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and isinstance(data.get("patch"), str):
            return data["patch"]
    except json.JSONDecodeError:
        pass
    if "--- " in stripped and "@@" in stripped:
        return raw
    return None
