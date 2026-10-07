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
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "orchestral" / "entrypoints.yaml"

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
        "match": "attributes resolved through __getattr__ delegates",
        "why": (
            "orchestral/web/snapshot.py _PublishedStore.__getattr__ forwards "
            "every RunStore method — the wrapped methods have no visible caller."
        ),
    },
)


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    via: str


def _cli_commands(harness: Path) -> list[Entry]:
    tree = ast.parse(harness.read_text())
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


def _http_surface(server: Path) -> list[Entry]:
    tree = ast.parse(server.read_text())
    entries = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("do_"):
            entries.append(
                Entry(
                    name=f"orchestral.web.server.<handler>.{node.name}",
                    kind="http_handler",
                    via="BaseHTTPRequestHandler method dispatch",
                )
            )
    seen: set[str] = set()
    for node in ast.walk(tree):
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and child.value.startswith("/")
                and "\n" not in child.value
                and child.value not in seen
            ):
                seen.add(child.value)
                entries.append(
                    Entry(
                        name=child.value,
                        kind="http_route",
                        via="path literal in _route_get/_route_post",
                    )
                )
    return entries


def _audit_rules(audit: Path) -> list[Entry]:
    tree = ast.parse(audit.read_text())
    for node in ast.walk(tree):
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
    for path in sorted(tui_dir.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and re.match(
                        r"^(on_|action_|compose$|watch_|validate_)", item.name
                    ):
                        entries.append(
                            Entry(
                                name=f"orchestral.tui.{path.stem}.{node.name}.{item.name}",
                                kind="textual_handler",
                                via="Textual message/action dispatch",
                            )
                        )
    return entries


def _js_views(router: Path) -> list[Entry]:
    entries = []
    for m in re.finditer(
        r'import\s*\{\s*(\w+)\s*\}\s*from\s*"(\./views/[\w.]+\.js)"',
        router.read_text(),
    ):
        entries.append(
            Entry(name=f"{m.group(2)[2:]}::{m.group(1)}", kind="js_view", via="router.js import")
        )
    return entries


def _main_guards() -> list[Entry]:
    entries = []
    for path in sorted(ROOT.glob("**/*.py")):
        parts = path.relative_to(ROOT).parts
        if parts[0] in {"tests", ".venv", "node_modules"} or "__pycache__" in parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
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
                        Entry(
                            name=path.relative_to(ROOT).as_posix(),
                            kind="main_guard",
                            via='if __name__ == "__main__"',
                        )
                    )
    return entries


def _module_exports() -> list[Entry]:
    entries = []
    for path in sorted(ROOT.glob("orchestral/**/*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__all__"
                    and isinstance(node.value, (ast.List, ast.Tuple))
                ):
                    names = [
                        str(e.value)
                        for e in node.value.elts
                        if isinstance(e, ast.Constant)
                    ]
                    entries.append(
                        Entry(
                            name=f"{path.relative_to(ROOT).as_posix()}:{','.join(names)}",
                            kind="module_exports",
                            via="__all__",
                        )
                    )
    return entries


def scan(root: Path = ROOT) -> list[Entry]:
    entries = (
        _cli_commands(root / "harness.py")
        + _console_scripts(root / "pyproject.toml")
        + _http_surface(root / "orchestral" / "web" / "server.py")
        + _audit_rules(root / "orchestral" / "audit.py")
        + _textual_handlers(root / "orchestral" / "tui")
        + _js_views(root / "ui" / "js" / "router.js")
        + _main_guards()
        + _module_exports()
    )
    return sorted(set(entries), key=lambda e: (e.kind, e.name))


def render(entries: list[Entry]) -> str:
    lines = [
        "# Entry-point registry — reachability roots the static call graph",
        "# cannot see. Regenerate with `scripts/audit_entrypoints.py --write`;",
        "# tests/test_entrypoints.py fails when the scan drifts from this file.",
        "schema: 1",
        "patterns:",
    ]
    for p in PATTERNS:
        lines.append(f"  - kind: {p['kind']}")
        lines.append(f"    match: {p['match']!r}")
        lines.append(f"    why: {p['why']!r}")
    lines.append("entry_points:")
    for e in entries:
        lines.append(f"  - name: {e.name!r}")
        lines.append(f"    kind: {e.kind}")
        lines.append(f"    via: {e.via!r}")
    return "\n".join(lines) + "\n"


def parse_registry(text: str) -> set[tuple[str, str]]:
    """Read the checked-in registry without a YAML dependency for callers."""
    entries: set[tuple[str, str]] = set()
    name = kind = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- name:"):
            name = s.split(":", 1)[1].strip().strip("'\"")
        elif s.startswith("kind:"):
            kind = s.split(":", 1)[1].strip()
            if name and kind:
                entries.add((name, kind))
                name = kind = None
    return entries


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    rendered = render(scan())
    if "--write" in args:
        REGISTRY.write_text(rendered)
        print(f"wrote {REGISTRY}")
        return 0
    current = REGISTRY.read_text() if REGISTRY.exists() else ""
    if parse_registry(current) != parse_registry(rendered):
        print("entrypoints.yaml is stale — run scripts/audit_entrypoints.py --write")
        return 1
    print(f"registry current: {len(parse_registry(rendered))} entry points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
