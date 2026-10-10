#!/usr/bin/env python3
"""Emit the tiered dead-code candidate report (reduction program U3).

Joins three independent evidence sources against the entry-point registry
minus-list and writes ``docs/analysis/dead-code-report-<stamp>.json``:

- **Graph**: codebase-memory's ``graph.db.zst`` (zstd-compressed SQLite,
  read offline — no MCP server). Candidates are ``Function``/``Method``
  nodes with zero inbound ``CALLS`` or ``USAGE`` edges in repo-owned paths.
  ``USAGE`` absorbs the ``callback_argument`` hazard: an argument-position
  reference produces a USAGE edge, so passed-by-name callbacks are not
  candidates. ``artifact.json.commit`` must equal ``git rev-parse HEAD``
  unless ``--allow-stale-index``.
- **Vulture** (optional, ``--vulture-file``): ``uvx vulture <paths>
  --min-confidence 60`` output. A ``file:line`` record corroborates a
  graph candidate; the confidence value rides along in ``evidence[]``.
- **Coverage** (optional, ``--coverage-json``): ``coverage json -o``
  output. A candidate whose line range intersects ``executed_lines`` was
  observed at runtime and is dropped; a zero-hit range corroborates.

Tiers: A = graph plus a second source (only deletable candidates). B =
single source, or capped by a reachability mechanism the graph cannot
prove (``__getattr__`` proxy, lazy import inside a registered root,
``tests/`` surface). C = ``ui/js`` — the graph indexes JS but
route/template wiring is weak signal. Registry ``entry_points`` names and
``dunder_protocol``/``unittest_discovery`` pattern matches are excluded
before tiering.

Collecting coverage is ad-hoc, not a repo dep::

    coverage run --source=orchestral,harness -m unittest discover -s tests
    coverage json -o /tmp/coverage.json

Same inputs produce byte-identical output: rows sort by
``(file, start_line, symbol)`` and the report carries no wall-clock fields.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO_OWNED = ("orchestral/", "harness.py", "scripts/", "demo/", "tests/", "ui/js/")
DEFAULT_REGISTRY = "orchestral/entrypoints.yaml"
DEFAULT_OUT = "docs/analysis/dead-code-report-2026-10.json"

_DUNDER = re.compile(r"^__\w+__$")
_TEST_NAME = re.compile(r"^(Test\w+|test_\w+)$")
# Convention-named framework callbacks — http.server (do_*/log_message),
# HTMLParser (handle_*tag), Textual (key_*/action_*/on_*). The graph cannot
# see the dispatcher, so zero-inbound is meaningless for these names.
_FRAMEWORK_CALLBACK = re.compile(
    r"^(do_[A-Z]+|log_message|handle_\w+tag\w*|key_\w+|action_\w+|on_\w+)$"
)
_VULTURE_LINE = re.compile(r"^(.+?):(\d+):\s+(.+?)\s+\((\d+)%\s+confidence\)\s*$")


def _decompress_graph(zst_path: Path) -> Path:
    """Return a path to the uncompressed sqlite DB (a temp file)."""
    try:
        import zstandard  # type: ignore[import-not-found]

        out = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        with open(zst_path, "rb") as src:
            out.write(zstandard.ZstdDecompressor().decompress(src.read()))
        out.close()
        return Path(out.name)
    except ImportError:
        pass
    out = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    out.close()
    proc = subprocess.run(
        ["zstd", "-dc", str(zst_path)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"cannot decompress {zst_path}: install `zstandard` or the `zstd` CLI"
        )
    Path(out.name).write_bytes(proc.stdout)
    return Path(out.name)


def _head_commit(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git rev-parse HEAD failed in {repo_root}")
    return proc.stdout.strip()


def _check_freshness(
    artifact_path: Path, head: str, allow_stale: bool
) -> tuple[str, bool]:
    artifact = json.loads(artifact_path.read_text())
    index_commit = artifact.get("commit", "")
    fresh = index_commit == head
    if not fresh and not allow_stale:
        raise SystemExit(
            f"stale index: artifact commit {index_commit} != HEAD {head} "
            "(reindex, or pass --allow-stale-index)"
        )
    return index_commit, fresh


def _candidate_pool(db_path: Path) -> list[dict[str, Any]]:
    """Zero-inbound CALLS|USAGE Function/Method nodes in repo-owned files."""
    db = sqlite3.connect(db_path)
    clauses = " OR ".join(
        ["n.file_path LIKE ?" if p.endswith("/") else "n.file_path = ?"
         for p in REPO_OWNED]
    )
    params = [p + "%" if p.endswith("/") else p for p in REPO_OWNED]
    rows = db.execute(
        f"""SELECT n.qualified_name, n.name, n.file_path,
                   n.start_line, n.end_line, n.properties, n.label
            FROM nodes n
            WHERE n.label IN ('Function', 'Method')
              AND ({clauses})
              AND NOT EXISTS (
                    SELECT 1 FROM edges e
                    WHERE e.target_id = n.id AND e.type IN ('CALLS', 'USAGE'))
            ORDER BY n.file_path, n.start_line""",
        params,
    ).fetchall()
    db.close()
    pool = []
    for qualified, name, path, start, end, props, label in rows:
        pool.append(
            {
                "qualified_name": qualified,
                "name": name,
                "file": path,
                "start_line": start,
                "end_line": end,
                "label": label,
                "properties": json.loads(props or "{}"),
            }
        )
    return pool


def _load_registry(path: Path) -> tuple[set[str], set[tuple[str, str]], list[str]]:
    """Return (qualified-name minus-list, (file, name) minus-list, pattern kinds)."""
    data = yaml.safe_load(path.read_text()) or {}
    qualified: set[str] = set()
    file_symbol: set[tuple[str, str]] = set()
    for entry in data.get("entry_points", []):
        name = entry.get("name", "")
        if not name:
            continue
        if ":" in name:
            file_part, _, symbol = name.partition(":")
            file_symbol.add((_js_to_path(file_part), symbol))
        elif name.endswith(".py") or "/" in name or "?" in name:
            # path literals (http_route, main_guard) are not symbols
            continue
        else:
            qualified.add(name)
    patterns = [p.get("kind", "") for p in data.get("patterns", [])]
    return qualified, file_symbol, patterns


def _js_to_path(file_part: str) -> str:
    """Registry file parts are repo-relative paths already (js_view is
    relative to ui/js/ and joins with '::' — normalize both)."""
    if file_part.endswith(".js") and not file_part.startswith("ui/js/"):
        return "ui/js/" + file_part
    return file_part


def _matches_registry(
    candidate: dict[str, Any], qualified: set[str], file_symbol: set[tuple[str, str]]
) -> bool:
    """Graph qualified_names carry a project prefix (``orchestral.``); the
    registry's dotted names do not."""
    qn = candidate["qualified_name"]
    normalized = qn.split(".", 1)[1] if "." in qn else qn
    if normalized in qualified or qn in qualified:
        return True
    return (candidate["file"], candidate["name"]) in file_symbol


def _pattern_excluded(candidate: dict[str, Any]) -> str | None:
    """Return the pattern kind that excludes this candidate, or None."""
    if _DUNDER.match(candidate["name"]):
        return "dunder_protocol"
    if candidate["file"].startswith("tests/") and _TEST_NAME.match(candidate["name"]):
        return "unittest_discovery"
    return None


def _function_scope_imports(path: Path) -> set[str]:
    """Import targets that appear only inside a function body — the
    lazy_import hazard: callees get no visible top-level importer."""
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return set()
    scoped: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import):
                    scoped.update(a.name for a in inner.names)
                elif isinstance(inner, ast.ImportFrom) and inner.module:
                    scoped.add(inner.module)
                    scoped.update(
                        f"{inner.module}.{a.name}" for a in inner.names
                    )
    return scoped


def _module_defines_getattr(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return False
    return any(
        isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
        for node in tree.body
    )


def _lazy_imported_modules(repo_root: Path, root_files: list[str]) -> set[str]:
    """Modules pulled in by function-scope imports inside registered roots.
    Only orchestral.* modules map back to repo files."""
    modules: set[str] = set()
    for rel in root_files:
        fpath = repo_root / rel
        if fpath.exists():
            modules.update(_function_scope_imports(fpath))
    return {m for m in modules if m.startswith("orchestral")}


def _module_of_file(rel_file: str) -> str:
    return rel_file[:-3].replace("/", ".") if rel_file.endswith(".py") else rel_file


def _parse_vulture(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """vulture records keyed by (repo-relative file, line)."""
    hits: dict[tuple[str, int], dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        m = _VULTURE_LINE.match(line.strip())
        if not m:
            continue
        file_part, line_no, detail, conf = (
            m.group(1), int(m.group(2)), m.group(3), int(m.group(4)),
        )
        # vulture may print absolute paths; keep the repo-relative tail
        for prefix in REPO_OWNED:
            idx = file_part.find(prefix)
            if idx > 0:
                file_part = file_part[idx:]
                break
        hits.setdefault(
            (file_part, line_no), {"detail": detail, "confidence": conf}
        )
    return hits


def _parse_coverage(path: Path) -> dict[str, set[int]]:
    """{repo-relative file: set of executed line numbers}."""
    data = json.loads(path.read_text())
    executed: dict[str, set[int]] = {}
    for fname, fdata in (data.get("files") or {}).items():
        executed[fname] = set(fdata.get("executed_lines") or [])
    return executed


def _vulture_hit(
    candidate: dict[str, Any], hits: dict[tuple[str, int], dict[str, Any]]
) -> dict[str, Any] | None:
    for line_no in range(candidate["start_line"], candidate["end_line"] + 1):
        hit = hits.get((candidate["file"], line_no))
        if hit:
            return hit
    return None


def _redefined_with_callers(
    db_path: Path, cache: dict[str, int]
) -> dict[str, int]:
    """{name: count of same-named Function/Method nodes with inbound
    CALLS|USAGE}. A candidate whose name is implemented and reachable
    elsewhere is interface-shaped (override/duck-type dispatch the graph
    does not record) — cap at B, never A."""
    if cache:
        return cache
    db = sqlite3.connect(db_path)
    for name, cnt in db.execute(
        """SELECT n.name, COUNT(*) FROM nodes n
           WHERE n.label IN ('Function', 'Method')
             AND EXISTS (SELECT 1 FROM edges e
                         WHERE e.target_id = n.id
                           AND e.type IN ('CALLS', 'USAGE'))
           GROUP BY n.name"""
    ):
        cache[name] = cnt
    db.close()
    return cache


def _coverage_state(
    candidate: dict[str, Any], executed: dict[str, set[int]] | None
) -> str | None:
    """'executed' | 'zero' | None when the file went unmeasured.

    A file absent from the coverage data is unknown, not zero — coverage
    may simply not have measured it (e.g. paths outside --source)."""
    if executed is None or candidate["file"] not in executed:
        return None
    lines = executed[candidate["file"]]
    return (
        "executed"
        if any(ln in lines for ln in range(candidate["start_line"],
                                           candidate["end_line"] + 1))
        else "zero"
    )


def _tier(candidate: dict[str, Any], evidence: list[dict[str, Any]],
          caps: list[str]) -> str:
    if candidate["file"].startswith("ui/js/"):
        return "C"
    if caps:
        return "B"
    return "A" if len(evidence) > 1 else "B"


def build_report(
    *,
    db_path: Path,
    artifact_path: Path,
    registry_path: Path,
    repo_root: Path,
    vulture_path: Path | None = None,
    coverage_path: Path | None = None,
    allow_stale: bool = False,
    head: str | None = None,
) -> dict[str, Any]:
    head = head or _head_commit(repo_root)
    index_commit, fresh = _check_freshness(artifact_path, head, allow_stale)
    qualified, file_symbol, _patterns = _load_registry(registry_path)
    pool = _candidate_pool(db_path)

    root_files = (
        [
            "harness.py",
            *sorted(
                str(p.relative_to(repo_root))
                for p in (repo_root / "orchestral").rglob("__main__.py")
            ),
        ]
        if (repo_root / "orchestral").exists()
        else ["harness.py"]
    )
    lazy_modules = _lazy_imported_modules(repo_root, root_files)
    getattr_cache: dict[str, bool] = {}

    vulture = _parse_vulture(vulture_path) if vulture_path else {}
    executed = _parse_coverage(coverage_path) if coverage_path else None
    redefined_counts = _redefined_with_callers(db_path, {})

    rows: list[dict[str, Any]] = []
    excluded_registry = excluded_pattern = dropped_executed = 0
    for cand in pool:
        if _matches_registry(cand, qualified, file_symbol):
            excluded_registry += 1
            continue
        if _pattern_excluded(cand):
            excluded_pattern += 1
            continue
        cov = _coverage_state(cand, executed)
        if cov == "executed":
            dropped_executed += 1
            continue

        evidence: list[dict[str, Any]] = [
            {"source": "graph", "detail": "zero inbound CALLS|USAGE"}
        ]
        hit = _vulture_hit(cand, vulture)
        if hit:
            evidence.append(
                {"source": "vulture",
                 "detail": f"{hit['confidence']}% confidence: {hit['detail']}"}
            )
        if cov == "zero":
            evidence.append(
                {"source": "coverage", "detail": "line range unexecuted"}
            )

        caps: list[str] = []
        if _FRAMEWORK_CALLBACK.match(cand["name"]):
            caps.append("framework-dispatch name")
        if (
            cand["label"] == "Method"
            and redefined_counts.get(cand["name"], 0) > 0
        ):
            caps.append("same-named method reachable elsewhere")
        if cand["file"].startswith("tests/"):
            caps.append("tests surface rides with the code it covers")
        if cand["file"].endswith(".py"):
            fpath = repo_root / cand["file"]
            if cand["file"] not in getattr_cache:
                getattr_cache[cand["file"]] = (
                    fpath.exists() and _module_defines_getattr(fpath)
                )
            if getattr_cache[cand["file"]]:
                caps.append("module defines __getattr__ delegate")
            if _module_of_file(cand["file"]) in lazy_modules:
                caps.append("lazy-imported module")

        rows.append(
            {
                "symbol": cand["qualified_name"].split(".", 1)[-1],
                "file": cand["file"],
                "lines": [cand["start_line"], cand["end_line"]],
                "tier": _tier(cand, evidence, caps),
                "surface": (
                    "tests" if cand["file"].startswith("tests/")
                    else "js" if cand["file"].startswith("ui/js/")
                    else "python"
                ),
                "evidence": evidence,
                "note": "; ".join(caps),
            }
        )

    rows.sort(key=lambda r: (r["file"], r["lines"][0], r["symbol"]))
    counts = {"pool": len(pool), "A": 0, "B": 0, "C": 0}
    for row in rows:
        counts[row["tier"]] += 1
    return {
        "schema": 1,
        "index_commit": index_commit,
        "index_fresh": fresh,
        "counts": counts,
        "excluded": {
            "registry": excluded_registry,
            "pattern": excluded_pattern,
            "coverage_executed": dropped_executed,
        },
        "sources": {
            "vulture": vulture_path is not None,
            "coverage": coverage_path is not None,
        },
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--graph-db", type=Path,
                        default=Path(".codebase-memory/graph.db.zst"),
                        help="zstd-compressed codebase-memory graph")
    parser.add_argument("--artifact", type=Path,
                        default=Path(".codebase-memory/artifact.json"),
                        help="index artifact metadata (freshness gate)")
    parser.add_argument("--registry", type=Path,
                        default=Path(DEFAULT_REGISTRY))
    parser.add_argument("--vulture-file", type=Path, default=None,
                        help="optional `uvx vulture` output for corroboration")
    parser.add_argument("--coverage-json", type=Path, default=None,
                        help="optional `coverage json -o` output for corroboration")
    parser.add_argument("--allow-stale-index", action="store_true",
                        help="emit even when artifact commit != HEAD")
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    db_path = _decompress_graph(args.graph_db)
    report = build_report(
        db_path=db_path,
        artifact_path=args.artifact,
        registry_path=args.registry,
        repo_root=args.repo_root.resolve(),
        vulture_path=args.vulture_file,
        coverage_path=args.coverage_json,
        allow_stale=args.allow_stale_index,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    c = report["counts"]
    print(
        f"pool={c['pool']} A={c['A']} B={c['B']} C={c['C']} "
        f"excluded={sum(report['excluded'].values())} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
