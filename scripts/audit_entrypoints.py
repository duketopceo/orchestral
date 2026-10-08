#!/usr/bin/env python3
"""Regenerate and check ``orchestral/entrypoints.yaml``.

The dead-code pipeline trusts codebase-memory zero-inbound queries, but the
graph only records CALLS edges. Symbols reached through mechanisms it cannot
see — argparse ``set_defaults`` tables, HTTP method dispatch, Textual's
``on_*``/``action_*`` conventions, tuple registries, JS imports, console
scripts — look dead and are not. This script enumerates those roots so the
registry can subtract them from any candidate list.

Two layers:

- ``scan()`` enumerates every explicitly reachable symbol/route the script
  knows how to find. The checked-in registry must match this output exactly;
  a new ``cmd_*`` handler, route, rule, or view that is not in the registry
  is a drift failure.
- ``PATTERNS`` documents kinds that cover unbounded or structural name sets
  (dunders, unittest discovery, callbacks passed by reference, getattr
  proxies). These cannot be enumerated; U3's report must treat any symbol
  matching them as reachable.

Parity contract: drift is checked on ``(name, kind)`` pairs only — ``via`` is
advisory documentation and does not participate in staleness checks.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "orchestral" / "entrypoints.yaml"

_HEADER = """\
# Entry-point registry — reachability roots the static call graph
# cannot see. Regenerate with `python3 scripts/audit_entrypoints.py --write`;
# tests/test_entrypoints.py fails when the scan drifts from this file.
#
# How to read this file:
#   - Join rule: a symbol whose name appears here (or matches a `patterns`
#     kind) is NOT a dead-code candidate, whatever its inbound count says.
#   - Name grammar per kind: cli_command/console_script/audit_rule/
#     textual_handler are dotted symbols (module.function or module.Class.method);
#     http_route names are path literals (not symbols — includes prefixes and
#     redirect fragments, not just exact routes); http_handler, getattr_table,
#     js_view and module_exports names are `path:symbol` (js_view uses `::` and
#     is relative to ui/js/); main_guard names are file paths (the whole file
#     is reachable).
#   - The scanner enumerates known kinds only. A new wiring pattern that none
#     of them recognize produces no drift failure — absence from this file is
#     not proof of unreachability. Extend the scanner when you add a mechanism.
#   - Parity is checked on (name, kind) pairs; `via` is advisory only.
"""

PATTERNS = (
    {
        "kind": "dunder_protocol",
        "match": "any __x__ attribute (methods, dataclass hooks, descriptors)",
        "why": "Python protocol dispatch — never a dead-code candidate.",
    },
    {
        "kind": "unittest_discovery",
        "match": "tests/**: Test* classes and test_* methods",
        "why": "unittest discovery, not static calls.",
    },
    {
        "kind": "callback_argument",
        "match": "any function object passed by name as a call argument",
        "why": (
            "The graph records CALLS only; an argument-position reference "
            "(e.g. set_authorizer(_fixture_authorizer)) leaves zero inbound "
            "CALLS edges on a live callback. U3 must check USAGE edges too."
        ),
    },
    {
        "kind": "getattr_proxy",
        "match": "any symbol reached through a __getattr__ delegate or getattr(obj, name) dispatch",
        "why": (
            "e.g. orchestral/web/snapshot.py _PublishedStore.__getattr__ forwards "
            "every RunStore method — the wrapped methods have no visible caller."
        ),
    },
    {
        "kind": "lazy_import",
        "match": "any symbol imported by a function-scope import inside a registered root",
        "why": (
            "Registered roots are reachability seeds, not a closure: e.g. "
            "harness.py cmd_sync does `from orchestral import cf` inside the "
            "function, so cf.* callees can show zero inbound edges while fully "
            "live. U3 must treat the import closure of every entry point as "
            "reachable, not just the listed name."
        ),
    },
)


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    via: str


@cache
def _parse(path: Path) -> ast.Module:
    # Memoized per process; callers that mutate files between scans must
    # call _parse.cache_clear() first or rescans serve stale trees.
    return ast.parse(path.read_text())


def _cli_commands(harness: Path) -> list[Entry]:
    tree = _parse(harness)
    # `x = sub.add_parser("name")` binds the subparser var to its CLI name.
    subparser_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "add_parser"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    subparser_names[target.id] = str(node.value.args[0].value)
    entries = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_defaults"
        ):
            continue
        var = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
        for kw in node.keywords:
            if kw.arg == "func" and isinstance(kw.value, ast.Name):
                sub = subparser_names.get(var, var)
                entries.append(
                    Entry(
                        name=f"harness.{kw.value.id}",
                        kind="cli_command",
                        via=f'subparser "{sub}"',
                    )
                )
    return entries


def _console_scripts(pyproject: Path) -> list[Entry]:
    data = tomllib.loads(pyproject.read_text())
    return [
        Entry(name=target, kind="console_script", via=f'[project.scripts] "{name}"')
        for name, target in sorted(data.get("project", {}).get("scripts", {}).items())
    ]


def _http_surface(root: Path) -> list[Entry]:
    # Any class holding do_* handlers — defs or alias assignments like
    # `do_GET = _handle` — is an HTTP dispatch surface. All tracked files are
    # scanned, not just web/server.py (apistub.py, scripts/serve_laya.py).
    entries = []
    seen_routes: set[str] = set()
    for rel in _tracked_py(root):
        if rel.startswith("tests/"):
            continue
        path = root / rel
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            handler = False
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name.startswith("do_")
                ):
                    handler = True
                    entries.append(
                        Entry(
                            name=f"{rel}:{node.name}.{item.name}",
                            kind="http_handler",
                            via="BaseHTTPRequestHandler method dispatch",
                        )
                    )
                elif isinstance(item, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id.startswith("do_") for t in item.targets
                ):
                    handler = True
                    for t in item.targets:
                        if isinstance(t, ast.Name) and t.id.startswith("do_"):
                            entries.append(
                                Entry(
                                    name=f"{rel}:{node.name}.{t.id}",
                                    kind="http_handler",
                                    via="handler method alias assignment",
                                )
                            )
            if not handler:
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(item):
                    if (
                        isinstance(child, ast.Constant)
                        and isinstance(child.value, str)
                        and child.value.startswith("/")
                        and "\n" not in child.value
                        and child.value not in seen_routes
                    ):
                        seen_routes.add(child.value)
                        entries.append(
                            Entry(
                                name=child.value,
                                kind="http_route",
                                via=f"path literal in {rel}:{node.name} methods",
                            )
                        )
    return entries


def _audit_rules(audit: Path) -> list[Entry]:
    for node in ast.walk(_parse(audit)):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "PER_SPEC_RULES"
                and isinstance(node.value, ast.Tuple)
            ):
                return [
                    Entry(
                        name=f"orchestral.audit.{elt.id}",
                        kind="audit_rule",
                        via="PER_SPEC_RULES tuple",
                    )
                    for elt in node.value.elts
                    if isinstance(elt, ast.Name)
                ]
    return []


def _textual_handlers(tui_dir: Path) -> list[Entry]:
    entries = []
    root = tui_dir.parent.parent
    for path in sorted(tui_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        dotted = ".".join(path.relative_to(root).with_suffix("").parts)
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(
                        item, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and re.match(r"^(_?on_|action_|compose$|watch_|validate_)", item.name):
                        entries.append(
                            Entry(
                                name=f"{dotted}.{node.name}.{item.name}",
                                kind="textual_handler",
                                via="Textual message/action dispatch",
                            )
                        )
    return entries


def _js_views(router: Path) -> list[Entry]:
    entries = []
    for m in re.finditer(
        r'import\s*\{([^}]+)\}\s*from\s*["\'](\./views/[\w.]+\.js)["\']',
        router.read_text(),
    ):
        for name in m.group(1).split(","):
            name = name.strip().split(" as ")[0].strip()
            if name:
                entries.append(
                    Entry(
                        name=f"{m.group(2)[2:]}::{name}",
                        kind="js_view",
                        via="router.js import",
                    )
                )
    return entries


def _getattr_tables(root: Path) -> list[Entry]:
    # String values in module-level UPPER_CASE table constants that match a
    # def in the same file are getattr-dispatch targets (e.g. runner.py's
    # _CANDIDATE_TASKS -> _validate_* methods, invoked via getattr(self, name)).
    entries = []
    for rel in _tracked_py(root):
        if rel.startswith("tests/"):
            continue
        path = root / rel
        tree = _parse(path)
        defined = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in tree.body:
            value: ast.expr | None
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets, value = [node.target], node.value
            else:
                continue
            if not any(t.id.isupper() for t in targets) or value is None:
                continue
            for child in ast.walk(value):
                if (
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and child.value in defined
                ):
                    entries.append(
                        Entry(
                            name=f"{rel}:{child.value}",
                            kind="getattr_table",
                            via="UPPER_CASE table string matching a local def",
                        )
                    )
    return entries


def _tracked_py(root: Path, prefix: str = "") -> list[str]:
    # Tracked files only — a filesystem glob would sweep gitignored trees
    # (runs/, worktrees, .venv) and make the registry machine-dependent.
    out = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        line for line in out.splitlines() if line.startswith(prefix)
    )


def _main_guards(root: Path) -> list[Entry]:
    entries = []
    for line in _tracked_py(root):
        path = root / line
        if line.split("/", 1)[0] == "tests":
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                left, comps = node.test.left, node.test.comparators
                if (
                    isinstance(left, ast.Name)
                    and left.id == "__name__"
                    and comps
                    and isinstance(comps[0], ast.Constant)
                    and comps[0].value == "__main__"
                ):
                    entries.append(
                        Entry(name=line, kind="main_guard", via='if __name__ == "__main__"')
                    )
    return entries


def _module_exports(root: Path) -> list[Entry]:
    entries = []
    for line in _tracked_py(root, "orchestral/"):
        rel = line
        path = root / line
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__all__"
                    and isinstance(node.value, (ast.List, ast.Tuple))
                ):
                    for e in node.value.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            entries.append(
                                Entry(
                                    name=f"{rel}:{e.value}",
                                    kind="module_exports",
                                    via="__all__",
                                )
                            )
    return entries


def scan(root: Path = ROOT) -> list[Entry]:
    entries = (
        _cli_commands(root / "harness.py")
        + _console_scripts(root / "pyproject.toml")
        + _http_surface(root)
        + _audit_rules(root / "orchestral" / "audit.py")
        + _textual_handlers(root / "orchestral" / "tui")
        + _js_views(root / "ui" / "js" / "router.js")
        + _getattr_tables(root)
        + _main_guards(root)
        + _module_exports(root)
    )
    return sorted(set(entries), key=lambda e: (e.kind, e.name))


def render(entries: list[Entry]) -> str:
    doc = {
        "schema": 1,
        "patterns": list(PATTERNS),
        "entry_points": [{"name": e.name, "kind": e.kind, "via": e.via} for e in entries],
    }
    return _HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def parse_registry(text: str) -> set[tuple[str, str]]:
    return {
        (e["name"], e["kind"]) for e in (yaml.safe_load(text) or {}).get("entry_points", [])
    }


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    rendered = render(scan())
    if "--write" in args:
        REGISTRY.write_text(rendered)
        print(f"wrote {REGISTRY}")
        return 0
    scanned = parse_registry(rendered)
    current = parse_registry(REGISTRY.read_text()) if REGISTRY.exists() else set()
    if current != scanned:
        print("entrypoints.yaml is stale — run scripts/audit_entrypoints.py --write")
        return 1
    print(f"registry current: {len(scanned)} entry points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
