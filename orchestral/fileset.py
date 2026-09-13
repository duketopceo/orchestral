"""File-set contract for multi-file tasks: parse, sanitize, merge, zip.

A multi-file worker returns a JSON file set (`{"files": [{"path", "content"}]}`).
Everything in this module is a pure function over data — no runner or client
coupling — because these rules are the security-sensitive core of the task
type: a path that escapes the archive, a set that collides with itself, or an
unbounded response are all failures this module must catch before bytes exist.

Contents never leave this module except inside the archive itself: callers
trace `summarize_fileset` (paths, sizes, hashes), never file bodies.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from typing import Any
from urllib.parse import unquote

# Caps, enforced before parsing and while building. A worker completion is
# bounded upstream only by the model's max_tokens, which is far larger than a
# sane file set; these make the failure explicit instead of exhausting memory.
MAX_WORKER_RESPONSE_BYTES = 1_000_000
MAX_FILES_PER_SET = 50
MAX_FILE_CONTENT_BYTES = 500_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 2_000_000
MAX_PATH_LENGTH = 256
MAX_TOTAL_PATH_BYTES = 4096
MAX_ZIP_OUTPUT_BYTES = 2_000_000

# Characters that are illegal or dangerous in a path segment: control chars,
# the Windows-reserved set, colon (drive/ADS syntax), and percent (an encoded
# separator or traversal must never survive into a member name).
_ILLEGAL_CHARS = re.compile(r"[\x00-\x1f\x7f<>\"|?*:%]")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Pinned zip header fields — identical file sets must produce identical bytes.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_COMPRESSLEVEL = 6
_ZIP_EXTERNAL_ATTR = 0o100644 << 16  # regular file (S_IFREG), rw-r--r--


class FilesetError(Exception):
    """Raised when a file set is malformed, unsafe, or over a cap."""


def check_response_size(content: str) -> None:
    """Reject an oversized worker response before it is parsed."""
    size = _byte_len(content)
    if size > MAX_WORKER_RESPONSE_BYTES:
        raise FilesetError(
            f"Worker response is {size} bytes, over the "
            f"{MAX_WORKER_RESPONSE_BYTES}-byte cap"
        )


def parse_fileset(data: Any) -> dict[str, str]:
    """Turn parsed worker JSON into a sanitized {path: content} mapping.

    Accepts the documented `{"files": [{"path", "content"}]}` shape, a `files`
    map, and a bare `{path: content}` map. Entries without a usable path are
    skipped; unsafe paths raise (never silently dropped or renamed).
    """
    files: dict[str, str] = {}
    total_bytes = 0
    for raw_path, raw_content in _entry_pairs(data):
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        body = raw_content if isinstance(raw_content, str) else str(raw_content)
        body_bytes = _byte_len(body)
        if body_bytes > MAX_FILE_CONTENT_BYTES:
            raise FilesetError(
                f"File {raw_path!r} is {body_bytes} bytes, over the "
                f"{MAX_FILE_CONTENT_BYTES}-byte per-file cap"
            )
        total_bytes += body_bytes
        if total_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise FilesetError(
                f"File set exceeds the {MAX_TOTAL_UNCOMPRESSED_BYTES}-byte total cap"
            )
        canonical = sanitize_path(raw_path)
        if canonical in files:
            # two spellings collapsing to one path would silently overwrite
            raise FilesetError(f"Duplicate path {canonical!r} (from {raw_path!r})")
        files[canonical] = body
    if len(files) > MAX_FILES_PER_SET:
        raise FilesetError(f"File set has {len(files)} files, over the {MAX_FILES_PER_SET} cap")
    validate_fileset(files)
    return files


def _byte_len(text: str) -> int:
    """Caps are byte budgets: `len` counts code points, which under-counts
    multi-byte text by up to 4x."""
    return len(text.encode("utf-8"))


def sanitize_path(path: str) -> str:
    """Normalize and validate one file path; return its canonical form.

    Separators are normalized before any other check so a backslash spelling
    cannot slip past the traversal rules. The returned value is case-folded so
    two spellings of the same path collapse instead of colliding silently on a
    case-insensitive filesystem.
    """
    raw = str(path).strip()
    # decode percent escapes to a fixed point first: `%2e%2e%2f` and
    # `%252e%252e%252f` must be caught by the same segment rules as `../`,
    # and any escape left after decoding is rejected as an illegal `%`
    normalized = raw
    for _ in range(3):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    normalized = normalized.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if len(normalized) > MAX_PATH_LENGTH:
        raise FilesetError(f"Path {raw!r} is longer than {MAX_PATH_LENGTH} characters")
    if normalized.startswith("/"):
        raise FilesetError(f"Path {raw!r} is absolute")
    if re.match(r"^[A-Za-z]:", normalized):
        raise FilesetError(f"Path {raw!r} has a drive letter")
    segments = [s for s in normalized.split("/") if s != ""]
    if not segments:
        raise FilesetError(f"Path {raw!r} is empty")
    for segment in segments:
        if segment in (".", ".."):
            raise FilesetError(f"Path {raw!r} contains a {segment!r} segment")
        if _ILLEGAL_CHARS.search(segment):
            raise FilesetError(f"Path {raw!r} contains an illegal character")
        if not segment.isascii():
            raise FilesetError(f"Path {raw!r} contains a non-ASCII character")
        if segment != segment.strip(". "):
            raise FilesetError(f"Path {raw!r} has a segment starting or ending with a dot or space")
        if segment.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise FilesetError(f"Path {raw!r} uses the reserved name {segment!r}")
    return "/".join(segments).lower()


def validate_fileset(files: dict[str, str]) -> None:
    """Set-level rules: no duplicates, no path a prefix of another, no dirs."""
    paths = list(files)
    if len(paths) > MAX_FILES_PER_SET:
        raise FilesetError(f"File set has {len(paths)} files, over the {MAX_FILES_PER_SET} cap")
    total_path_bytes = sum(len(p) for p in paths)
    if total_path_bytes > MAX_TOTAL_PATH_BYTES:
        raise FilesetError(f"File set paths total {total_path_bytes} bytes, over the cap")
    for path in paths:
        if path.endswith("/"):
            raise FilesetError(f"Path {path!r} is a directory entry")
    for i, path in enumerate(paths):
        for other in paths[i + 1:]:
            if path == other:
                raise FilesetError(f"Duplicate path {path!r}")
            if other.startswith(f"{path}/") or path.startswith(f"{other}/"):
                raise FilesetError(f"Path {path!r} conflicts with {other!r} as file and directory")


def merge_filesets(
    file_sets: list[tuple[int, dict[str, str]]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Merge per-subtask file sets; later subtasks win, conflicts recorded.

    `file_sets` is a list of (subtask_id, files) in plan order. Only paths and
    subtask ids appear in the conflict records — never file contents.
    """
    merged: dict[str, str] = {}
    owners: dict[str, int] = {}
    conflicts: list[dict[str, Any]] = []
    for subtask_id, files in file_sets:
        for path, body in files.items():
            if path in merged:
                conflicts.append({
                    "path": path,
                    "winner_subtask": subtask_id,
                    "loser_subtask": owners[path],
                })
            merged[path] = body
            owners[path] = subtask_id
    return merged, conflicts


def build_zip(files: dict[str, str]) -> bytes:
    """Build a byte-reproducible zip of the file set.

    Sorted entries, pinned timestamps and header fields, fixed compression, and
    S_IFREG attributes so identical input always yields identical bytes and no
    entry can be read as a symlink.
    """
    validate_fileset(files)
    if not all(isinstance(p, str) and isinstance(c, str) for p, c in files.items()):
        raise FilesetError("build_zip requires a dict of str paths to str contents")
    # defense in depth: this is the last boundary before bytes exist, so no
    # caller can zip a path that sanitization would have rewritten or rejected
    for path in files:
        if sanitize_path(path) != path:
            raise FilesetError(f"Path {path!r} is not canonical — run it through parse_fileset")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=_ZIP_COMPRESSLEVEL) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.create_version = 20
            info.extract_version = 20
            info.flag_bits = 0x0
            info.external_attr = _ZIP_EXTERNAL_ATTR
            archive.writestr(info, files[path])
    data = buffer.getvalue()
    if len(data) > MAX_ZIP_OUTPUT_BYTES:
        raise FilesetError(f"Archive would be {len(data)} bytes, over the {MAX_ZIP_OUTPUT_BYTES} cap")
    return data


def summarize_fileset(files: dict[str, str]) -> dict[str, Any]:
    """The only file-set shape allowed into traces: paths, sizes, hashes."""
    return {
        "paths": sorted(files),
        "sizes": {p: len(b) for p, b in sorted(files.items())},
        "sha256": {p: hashlib.sha256(b.encode()).hexdigest() for p, b in sorted(files.items())},
    }


def manifest_listing(files: dict[str, str]) -> str:
    """A content-free listing for the judge prompt (paths and sizes only)."""
    return "\n".join(f"{p} ({len(files[p])} bytes)" for p in sorted(files))


def expected_paths(task_metadata: dict[str, Any]) -> list[str]:
    """Sanitized `metadata.expected_paths`, for validation and dry runs."""
    declared = task_metadata.get("expected_paths") or []
    if not isinstance(declared, list):
        return []
    return [sanitize_path(p) for p in declared if isinstance(p, str) and p.strip()]


def _entry_pairs(data: Any) -> list[tuple[Any, Any]]:
    """Normalize the accepted file-set shapes into (path, content) pairs."""
    if isinstance(data, dict):
        files = data.get("files")
        if isinstance(files, list):
            return [
                (item.get("path"), item.get("content", ""))
                for item in files if isinstance(item, dict)
            ]
        if isinstance(files, dict):
            return list(files.items())
        # a bare {path: content} map, minus known metadata keys
        return [
            (k, v) for k, v in data.items()
            if k not in ("notes", "reasoning", "subtask_id")
        ]
    if isinstance(data, list):
        return [
            (item.get("path"), item.get("content", ""))
            for item in data if isinstance(item, dict)
        ]
    return []
