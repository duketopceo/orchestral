"""Execution validator for `sql` tasks — read-only sqlite result equivalence.

A sql task ships a fixture (`metadata.schema`, `metadata.seed`) and a
reference query (`metadata.reference_sql`). The worker's candidate query is
executed read-only against a fresh fixture copy and compared to the
reference's result — multiset by default, ordered when `metadata.ordered`.
The reference must return at least one row: an empty result is a broken spec,
not an empty answer for a candidate to match.

The connection is `mode=ro` and a progress-handler step cap bounds runaway
queries. This is deterministic and cheap, not a sandbox — sqlite has no
write access in ro mode, but candidate SQL still executes on this machine.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_MAX_STEPS = 1_000_000
_PREVIEW = 500

_FENCE_RE = re.compile(r"```(?:sql|sqlite)?\s*\n(.*?)```", re.DOTALL)


def extract_sql(payload: Any) -> str:
    """Pull a SQL string out of a worker output: dict keys, fences, or raw."""
    if isinstance(payload, dict):
        for key in ("query", "sql", "content"):
            if isinstance(payload.get(key), str):
                return extract_sql(payload[key])
        return ""
    text = str(payload or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    return text


def _script(value: Any) -> str:
    """Metadata scripts may be a string or a list of statements."""
    if isinstance(value, list):
        return ";\n".join(str(stmt).rstrip().rstrip(";") for stmt in value)
    return str(value or "")


def build_fixture(dest: Path, schema_sql: str, seed_sql: str) -> Path:
    """Create the task's sqlite database under `dest`; return its path."""
    db = dest / "fixture.db"
    conn = sqlite3.connect(db)
    try:
        if schema_sql:
            conn.executescript(schema_sql)
        if seed_sql:
            conn.executescript(seed_sql)
        conn.commit()
    finally:
        conn.close()
    return db


def run_readonly_query(
    db: Path, sql: str, *, max_steps: int = DEFAULT_MAX_STEPS
) -> tuple[list[tuple[Any, ...]] | None, str | None]:
    """Execute `sql` read-only; return (rows, error). Aborts past max_steps."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return None, f"open failed: {exc}"
    steps = 0

    def _progress() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps > max_steps else 0

    conn.set_progress_handler(_progress, 1000)
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        return None, str(exc)
    finally:
        conn.close()
    return [tuple(r) for r in rows], None


def rows_match(
    expected: list[tuple[Any, ...]], got: list[tuple[Any, ...]], *, ordered: bool
) -> bool:
    if ordered:
        return expected == got
    return Counter(expected) == Counter(got)


def run_sql_check(
    metadata: dict[str, Any],
    candidate_sql: str,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> dict[str, Any]:
    """Full check: fixture + reference + candidate; returns a report dict."""
    report: dict[str, Any] = {
        "executed": False,
        "match": False,
        "ordered": bool(metadata.get("ordered")),
        "rows_expected": None,
        "rows_got": None,
        "error": None,
        "score": None,
    }
    reference_sql = str(metadata.get("reference_sql") or "").strip()
    if not reference_sql:
        report["error"] = "sql task has no metadata.reference_sql"
        return report
    if not candidate_sql.strip():
        report["error"] = "worker produced no SQL"
        return report

    with tempfile.TemporaryDirectory(prefix="orchestral-sql-") as tmp:
        try:
            db = build_fixture(
                Path(tmp),
                _script(metadata.get("schema")),
                _script(metadata.get("seed")),
            )
        except sqlite3.Error as exc:
            report["error"] = f"fixture build failed (task spec is broken): {exc}"
            return report
        expected, ref_err = run_readonly_query(db, reference_sql, max_steps=max_steps)
        if ref_err is not None:
            report["error"] = f"reference_sql failed (task spec is broken): {ref_err}"
            return report
        if not expected:
            # An empty reference is a broken spec, never a free pass: the
            # candidate would be graded against nothing, so any query returning
            # zero rows (including a nonsense one) would score 1.0. See DUK-90.
            report["error"] = "reference_sql returned no rows (task spec is broken)"
            return report
        got, cand_err = run_readonly_query(db, candidate_sql, max_steps=max_steps)
        if cand_err is not None:
            report["executed"] = True
            report["error"] = f"candidate query failed: {cand_err}"
            report["score"] = 0.0
            return report

    report["executed"] = True
    report["rows_expected"] = len(expected or [])
    report["rows_got"] = len(got or [])
    report["match"] = rows_match(expected or [], got or [], ordered=report["ordered"])
    report["score"] = 1.0 if report["match"] else 0.0
    if not report["match"]:
        report["expected_preview"] = repr((expected or [])[:5])[:_PREVIEW]
        report["got_preview"] = repr((got or [])[:5])[:_PREVIEW]
    return report
