"""Static integrity audit of the task suite.

Two questions, one read-only pass over `tasks/*.yaml`:

1. **Can every check the suite asks for actually fire?** A misspelled check
   name is silently dropped by the runner, so a spec can request a gate that
   never runs and still be reported as a pass.
2. **Can a model score on a spec without doing the work?** Structural-only
   checks, answers printed in the prompt, textbook problems, and 100 copies of
   one template all inflate a score without measuring capability.

Nothing here executes model code or calls a provider. It is a pure read of the
YAML, so it is safe to run in CI on every commit (`harness.py audit`).

Rules are stable identifiers: other tooling, the claims battery, and run
reports may reference them by name.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import TaskSpec, load_task
from .fileset import expected_paths, required_content

ERROR = "error"
WARN = "warn"
INFO = "info"

# ---------------------------------------------------------------------------
# Check registry — one source of truth for what the runner actually implements.
#
# `runner._validate` handles the text-producing types; the media and fileset
# types each compute their own small set. A name missing from these sets is
# dropped without comment, so this table is the contract `audit` enforces.
# ---------------------------------------------------------------------------

TEXT_CHECKS = frozenset(
    {
        "html",  # shorthand for html_parses + non_empty, used by generated batches
        "html_parses",
        "non_empty",
        "has_title",
        "has_cta",
        "has_form",
        "has_viewport",
        "no_placeholder",
        "within_budget",
        "has_required",
        "no_forbidden",
        "matches_pattern",
        "no_pattern",
    }
)

VALIDATION_CHECKS: dict[str, frozenset[str]] = {
    "html": TEXT_CHECKS,
    "constraint": TEXT_CHECKS,
    "needle": TEXT_CHECKS,
    "image": frozenset({"non_empty", "png_signature"}),
    "video": frozenset({"non_empty", "mp4_signature"}),
    "multi-file": frozenset({"non_empty", "zip_signature", "has_paths", "has_content"}),
}

# Registry names a validator expands into other checks instead of assigning
# itself. Keyed by type, so a name cannot hide behind another type's shorthand.
VALIDATION_SHORTHANDS: dict[str, frozenset[str]] = {
    "html": frozenset({"html"}),
    "constraint": frozenset({"html"}),
    "needle": frozenset({"html"}),
}

# The grading contract each self-anchored type needs before its grader has
# anything to compare the artifact against. A value lists interchangeable keys:
# supplying any one of them satisfies the contract. Without an anchor the
# grader either fails closed (`sql`, `api`) or vacuously passes — `extract`
# scores an empty `expected` as 1.0, and a `code` suite of `pass` bodies
# reports `tests_run=1 ok=True`.
GRADING_CONTRACT: dict[str, tuple[str, ...]] = {
    "code": ("tests",),
    "extract": ("fields", "expected"),
    "sql": ("reference_sql",),
    "api": ("calls",),
}

# Types whose grader reads `metadata.required`. `has_required` is a text check,
# so for every other type the declaration is inert — it must not count as an
# anchor there.
METADATA_REQUIRED_TYPES = frozenset({"html", "constraint", "needle"})

# Types whose grader never reads `validation:` — they compute a fixed check set
# from `metadata` instead. Declaring checks on these specs is always a mistake.
IGNORES_VALIDATION = frozenset({"code", "sql", "extract", "api"})

# Types whose artifact is bytes the text checks cannot read. A `has_required`
# token on a PNG is not a weaker gate, it is an unimplemented one — the audit
# would be asking for a check the runner drops as `unknown_validation_check`.
# Their topicality is the vision judge's job, so it is reported as
# `judge_gated_media` instead of pretending `validation:` can close it.
BYTE_ARTIFACT_TYPES = frozenset({"image", "video"})

# `multi-file` checks that read a file body rather than a name and byte count.
FILESET_BODY_CHECKS = frozenset({"has_content"})

# The fixed checks those types actually produce, for the error message.
COMPUTED_CHECKS: dict[str, str] = {
    "code": "expected_paths, quality_ok, compiles, tests_pass",
    "sql": "executed, matches_reference",
    "extract": "json_parses, required_present, types_ok, field score vs metadata.expected",
    "api": "missing, unexpected vs metadata.calls",
}

# Checks that can only pass if the artifact is *about* the task's subject.
# Element checks like has_title or has_cta deliberately do not appear here:
# they prove markup exists, not that the content is on-topic.
TOPIC_ANCHORS = frozenset({"has_required", "matches_pattern"})

# Types that grade against their own metadata anchor, so "no topic anchor in
# `validation:`" is not a finding for them.
SELF_ANCHORED_TYPES = frozenset(
    {"code", "sql", "extract", "api", "multi-file", "constraint", "needle"}
)

# Textbook problems with heavy pretraining coverage. Matching one is a
# contamination risk, not proof of contamination — the signal is that the
# score cannot separate recall from reasoning.
MEMORIZATION_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fizzbuzz", re.compile(r"\bfizz\s*buzz\b|\bfizzbuzz\b", re.I)),
    ("palindrome", re.compile(r"\bpalindrom", re.I)),
    ("slugify", re.compile(r"\bslugif|\burl\s*slug\b", re.I)),
    ("lru-cache", re.compile(r"\blru\b|least recently used", re.I)),
    (
        "expression-parser",
        re.compile(
            r"expression parser|infix (notation|parser)|\bpostfix\b|\brpn\b"
            r"|expr[- ]parser|shunting[- ]yard|recursive descent",
            re.I,
        ),
    ),
    ("reverse-string", re.compile(r"reverse (a |the )?string", re.I)),
    ("two-sum", re.compile(r"\btwo\s*sum\b", re.I)),
    ("binary-search", re.compile(r"binary search", re.I)),
    ("fibonacci", re.compile(r"\bfibonacci\b", re.I)),
    ("sorting-algorithm", re.compile(r"\b(merge sort|quicksort|quick sort|bubble sort|heap sort)\b", re.I)),
    ("valid-parentheses", re.compile(r"valid parentheses|balanced (brackets|parentheses)", re.I)),
    ("roman-numeral", re.compile(r"roman numera", re.I)),
    ("anagram", re.compile(r"\banagram\b", re.I)),
    ("leetcode", re.compile(r"\bleetcode\b", re.I)),
    ("hello-world", re.compile(r"\bhello[ ,]world\b", re.I)),
)

# `extract` is excluded on purpose: the prompt carries the source document, so
# every expected value is a copy of prompt text by construction. That is a
# transcription ceiling (W3), not a leak.
ANSWER_DERIVABLE_TYPES = frozenset({"extract", "sql"})

MIN_TOKEN_LEN = 3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One integrity or gaming-surface observation."""

    rule: str
    severity: str
    detail: str
    task_id: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "task_id": self.task_id,
            "path": self.path,
            "detail": self.detail,
        }


@dataclass
class AuditReport:
    """Aggregated audit output for a suite of specs."""

    specs: int = 0
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def of(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def errors(self) -> list[Finding]:
        return self.of(ERROR)

    @property
    def warnings(self) -> list[Finding]:
        return self.of(WARN)

    @property
    def infos(self) -> list[Finding]:
        return self.of(INFO)

    @property
    def ok(self) -> bool:
        """True when no check-name or spec-shape error was found.

        Warnings describe a gaming surface a human may accept on purpose, so
        they do not fail the audit. Errors mean a requested gate cannot fire.
        """
        return not self.errors

    def counts(self) -> dict[str, int]:
        return {
            "error": len(self.errors),
            "warn": len(self.warnings),
            "info": len(self.infos),
        }

    def by_rule(self) -> dict[str, list[Finding]]:
        grouped: dict[str, list[Finding]] = {}
        for finding in self.findings:
            grouped.setdefault(finding.rule, []).append(finding)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        return {
            "specs": self.specs,
            "ok": self.ok,
            "counts": self.counts(),
            "rules": {
                rule: [f.to_dict() for f in items] for rule, items in sorted(self.by_rule().items())
            },
        }

    def to_text(self, *, max_examples: int = 3) -> str:
        lines = [
            f"orchestral task audit — {self.specs} spec(s): "
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), {len(self.infos)} info",
        ]
        if not self.findings:
            lines.append("")
            lines.append("No gaming surface found.")
            return "\n".join(lines)
        for severity in (ERROR, WARN, INFO):
            items = self.of(severity)
            if not items:
                continue
            lines.append("")
            lines.append(f"{severity.upper()} ({len(items)})")
            by_rule: dict[str, list[Finding]] = {}
            for finding in items:
                by_rule.setdefault(finding.rule, []).append(finding)
            for rule, group in sorted(by_rule.items()):
                shown = group[:max_examples]
                extra = len(group) - len(shown)
                lines.append(f"  [{rule}] x{len(group)}")
                for finding in shown:
                    scope = finding.task_id or "suite"
                    lines.append(f"    - {scope}: {finding.detail}")
                if extra:
                    lines.append(f"    - ... and {extra} more")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_specs(root: Path | str = "tasks") -> list[tuple[Path, TaskSpec]]:
    """Load every task spec under `root`, sorted by path.

    A malformed spec raises `ConfigError` — a spec the runner cannot load is a
    hard error, not an audit finding.
    """
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f"task directory not found: {base}")
    loaded: list[tuple[Path, TaskSpec]] = []
    for path in sorted(base.rglob("*.yaml")):
        loaded.append((path, load_task(path)))
    return loaded


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def effective_checks(spec: TaskSpec) -> set[str]:
    """Check names the runner will act on, after shorthand expansion."""
    requested = set(spec.validation) if spec.validation else {"html_parses", "non_empty", "has_title"}
    if "html" in requested:
        requested.discard("html")
        requested |= {"html_parses", "non_empty"}
    return requested


def _has_topic_anchor(spec: TaskSpec) -> bool:
    if effective_checks(spec) & TOPIC_ANCHORS:
        return True
    # `metadata.required` is read by the text grader only. For every other type
    # the declaration is inert, so honouring it here would silence the finding
    # on a spec the grader never anchors.
    return spec.type in METADATA_REQUIRED_TYPES and bool(spec.metadata.get("required"))


def _scalars(value: Any) -> list[str]:
    """Flatten a metadata value to comparable strings, ignoring short tokens."""
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            out.extend(_scalars(key))
            out.extend(_scalars(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_scalars(item))
    elif isinstance(value, str):
        if len(value.strip()) >= MIN_TOKEN_LEN:
            out.append(value.strip())
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out.append(str(value))
    return out


def _route_key(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    return f"{str(call.get('method', 'GET')).upper()} {call.get('path', '')}".strip()


def _prompt_mentions_call(prompt: str, call: Any) -> bool:
    """Does the prompt spell out one expected call?

    Prose rarely writes `GET /orders` literally — it writes "GETs /orders" or
    "GET `/orders`" — so match the method (optionally inflected) and the path
    with a tolerant separator.
    """
    if not isinstance(call, dict):
        return False
    method = str(call.get("method", "GET")).upper()
    path = str(call.get("path", "")).strip()
    if not path:
        return False
    pattern = re.compile(rf"\b{method}(?:s|es)?\b\W{{0,3}}{re.escape(path)}\b", re.I)
    return pattern.search(prompt) is not None


# ---------------------------------------------------------------------------
# Errors — a requested gate that cannot fire
# ---------------------------------------------------------------------------


def check_validation_names(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag `validation:` entries the runner would silently drop."""
    if not spec.validation:
        return []
    where = str(path) if path else None
    if spec.type in IGNORES_VALIDATION:
        return [
            Finding(
                rule="ignored_validation_list",
                severity=ERROR,
                task_id=spec.id,
                path=where,
                detail=(
                    f"type '{spec.type}' never reads validation:; it computes "
                    f"{COMPUTED_CHECKS[spec.type]}. Declared: {sorted(spec.validation)}."
                ),
            )
        ]
    known = VALIDATION_CHECKS.get(spec.type, frozenset())
    unknown = sorted(set(spec.validation) - known)
    if not unknown:
        return []
    return [
        Finding(
            rule="unknown_validation_check",
            severity=ERROR,
            task_id=spec.id,
            path=where,
            detail=(
                f"validation {unknown} is not implemented for type '{spec.type}' and is "
                f"dropped without comment. Implemented: {sorted(known)}."
            ),
        )
    ]


# Builtins that are a total function of a foldable argument, so calling one on a
# constant yields a constant. `len` is the idiom the tautology shapes use
# (`assertEqual(0, len(''))`); the rest are the same one-line class.
_FOLDABLE_BUILTINS = frozenset({"len", "abs", "str", "int", "float", "bool"})

# unittest assertions that pass exactly when the condition they assert holds,
# and that take the compared pair as their first two positional arguments. The
# same expression on both sides therefore makes them pass for any artifact.
#
# The negating forms are absent on purpose: `assertNotEqual(x, x)` can never
# pass, which is a broken suite rather than a gate that cannot fail, and
# reporting it here would misname the defect.
_SAME_OPERAND_PASSES = frozenset(
    {
        "assertEqual",
        "assertIs",
        "assertIn",
        "assertSequenceEqual",
        "assertMultiLineEqual",
        "assertListEqual",
        "assertTupleEqual",
        "assertSetEqual",
        "assertDictEqual",
        "assertCountEqual",
    }
)


def _fold(node: ast.AST) -> tuple[bool, Any]:
    """Constant-fold a node, including arithmetic and comparison.

    `ast.literal_eval` rejects anything that is not already a literal, so it
    cannot fold `1 + 0` or `1 == 1` — which is why `assertEqual(1, 1+0)` and
    `assertTrue(1 == 1)` audited clean while `assertEqual(1, 1)` did not. This
    folds the operators over already-foldable operands, so a suite that asserts
    an arithmetic or comparison identity is recognised as the tautology it is.

    Only operators and calls that read nothing outside their own operands are
    folded. A name, an attribute, or a call to anything not in
    `_FOLDABLE_BUILTINS` is not a constant, so a real assertion against a value
    the artifact produces still folds to False and is never mistaken for one.
    """
    if isinstance(node, ast.Constant):
        return True, node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items: list[Any] = []
        for element in node.elts:
            ok, value = _fold(element)
            if not ok:
                return False, None
            items.append(value)
        return True, items if not isinstance(node, ast.Set) else set(items)
    if isinstance(node, ast.UnaryOp):
        ok, operand = _fold(node.operand)
        if not ok:
            return False, None
        try:
            if isinstance(node.op, ast.USub):
                return True, -operand
            if isinstance(node.op, ast.UAdd):
                return True, +operand
            if isinstance(node.op, ast.Not):
                return True, not operand
        except TypeError:
            return False, None
        return False, None
    if isinstance(node, ast.BinOp):
        left_ok, left = _fold(node.left)
        right_ok, right = _fold(node.right)
        if not (left_ok and right_ok):
            return False, None
        operation = _BINOPS.get(type(node.op))
        if operation is None:
            return False, None
        try:
            return True, operation(left, right)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            return False, None
    if isinstance(node, ast.BoolOp):
        values: list[Any] = []
        for value_node in node.values:
            ok, value = _fold(value_node)
            if not ok:
                return False, None
            values.append(value)
        if isinstance(node.op, ast.And):
            return True, all(values)
        return True, any(values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left_ok, left = _fold(node.left)
        right_ok, right = _fold(node.comparators[0])
        if not (left_ok and right_ok):
            return False, None
        operation = _COMPARES.get(type(node.ops[0]))
        if operation is None:
            return False, None
        try:
            return True, operation(left, right)
        except TypeError:
            return False, None
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _FOLDABLE_BUILTINS:
            ok, value = _fold(node.args[0])
            if ok:
                try:
                    return True, {
                        "len": len,
                        "abs": abs,
                        "str": str,
                        "int": int,
                        "float": float,
                        "bool": bool,
                    }[name](value)
                except (TypeError, ValueError):
                    return False, None
    return False, None


_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}

_COMPARES: dict[type[ast.cmpop], Any] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}


def _is_test_case(node: ast.ClassDef, bases: dict[str, ast.ClassDef], seen: frozenset[str] = frozenset()) -> bool:
    """Does this class inherit `unittest.TestCase`, directly or through a local base?"""
    if node.name in seen:  # a cyclic base list cannot be resolved statically
        return False
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name == "TestCase":
            return True
        parent = bases.get(name)
        if parent is not None and _is_test_case(parent, bases, seen | {node.name}):
            return True
    return False


def _assertion_name(node: ast.AST) -> str:
    func = node.func if isinstance(node, ast.Call) else None
    return func.attr if isinstance(func, ast.Attribute) else ""


def _repeats_itself(args: list[ast.expr]) -> bool:
    """Is the assertion comparing one expression against itself?

    A repeated expression is the same value twice, so the comparison holds for
    whatever it is bound to — `self.assertIs(s, s)` and `assertIn(x, [x])` are
    both true for any value of `s` or `x`. Neither folds, because a name is not a
    constant, which is why folding alone misses them.

    A call disqualifies it. `self.assertEqual(f(x), f(x))` also repeats, but `f`
    may read the artifact, so the doc keeps that class as execution-only rather
    than claiming it here. Everything else in an expression — a name, an
    attribute, a subscript, arithmetic — is a pure read of state that already
    exists, so repeating it cannot change the outcome.
    """
    if len(args) < 2:
        return False
    left, right = args[0], args[1]
    same = ast.dump(left) == ast.dump(right)
    if not same and isinstance(right, (ast.List, ast.Tuple, ast.Set)) and len(right.elts) == 1:
        # `assertIn(x, [x])`: the container holds nothing but the needle.
        same = ast.dump(left) == ast.dump(right.elts[0])
    if not same:
        return False
    return not any(isinstance(node, ast.Call) for node in ast.walk(left))


def _is_constant_assertion(node: ast.AST) -> bool:
    """Can this assertion never fail, whatever the artifact is?

    Three decidable shapes, all provable from the source alone:

    - a bare `assert` whose test folds to a truthy constant;
    - a `unittest` assertion whose argument folds to a constant, in which case
      the outcome is fixed whatever the artifact is;
    - a comparison assertion handed the *same* expression twice, or a
      membership assertion whose container is the needle alone — true for any
      value, so no artifact can turn it red.

    The third shape is what the first two miss. `self.assertIs(s, s)` and
    `assertIn(x, [x])` fold to nothing (`s` is a name), yet they hold whatever
    the name is bound to, and a suite built from them cannot discriminate by
    construction.

    Only provable constants are flagged, so a real smoke check such as
    `assert result is not None` is never mistaken for one. A name, an attribute,
    or a call the folder does not know is not a constant, which is exactly the
    distinction.
    """
    if isinstance(node, ast.Assert):
        ok, value = _fold(node.test)
        return ok and bool(value)
    if not isinstance(node, ast.Call):
        return False
    name = _assertion_name(node)
    args = node.args
    if name in _SAME_OPERAND_PASSES and _repeats_itself(args):
        return True
    if name in {"assertTrue", "assertFalse"} and len(args) == 1:
        ok, value = _fold(args[0])
        return ok and bool(value) is (name == "assertTrue")
    if name in {"assertEqual", "assertNotEqual"} and len(args) == 2:
        left_ok, left = _fold(args[0])
        right_ok, right = _fold(args[1])
        if left_ok and right_ok and left == right:
            return name == "assertEqual"
    if name in {"assertIs", "assertIsNot"} and len(args) == 2:
        left_ok, left = _fold(args[0])
        right_ok, right = _fold(args[1])
        if left_ok and right_ok and left is right:
            return name == "assertIs"
    if name in {"assertIn", "assertNotIn"} and len(args) == 2:
        needle_ok, needle = _fold(args[0])
        container_ok, container = _fold(args[1])
        if needle_ok and container_ok:
            try:
                return (needle in container) is (name == "assertIn")
            except TypeError:
                return False
    return False


def _declared_module(spec: TaskSpec) -> str:
    """The module a `code` suite is expected to import, matching the runner."""
    return str(spec.metadata.get("module") or "solution.py")


def _references_module(tree: ast.Module, module: str) -> bool:
    """Does the suite name `metadata.module` anywhere?

    A suite that never names the module under test cannot read it, so nothing it
    asserts is a function of the artifact — the suite passes for every input,
    including a stub. That is decidable without executing anything, and it is
    the property that separates the shapes this file can catch from the ones
    that need a run: `self.assertTrue(self.flag)` names nothing either, but it
    at least touches a value the artifact could have set, so only execution
    settles it.

    All four spelling forms count, because a real suite uses whichever reads
    best: `import solution`, `from solution import solve`, a bare `solution`
    name, or `importlib.import_module("solution")`.
    """
    stem = module.rsplit("/", 1)[-1]
    if stem.endswith(".py"):
        stem = stem[:-3]
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == stem:
            return True
        if isinstance(node, ast.ImportFrom) and node.module and node.module.rsplit(".", 1)[-1] == stem:
            return True
        if isinstance(node, ast.Import):
            if any(alias.name.rsplit(".", 1)[-1] == stem for alias in node.names):
                return True
        if isinstance(node, ast.Constant) and node.value in (stem, module):
            return True
    return False


def _suite_gate_reason(tests_source: str, module: str = "solution.py") -> str | None:
    """Why the suite cannot gate anything, or None when it can.

    Four properties are decidable from the source alone, and only these:

    - the suite parses — code that cannot be imported cannot gate anything;
    - it carries an assertion `unittest` will actually collect, which means
      inside a `test*` method of a `unittest.TestCase` subclass;
    - that assertion is not a constant;
    - the suite names `metadata.module` somewhere, so it can read the artifact
      at all.

    A self-referential assertion (`self.assertTrue(self.flag)`, set by the test
    itself) can never fail and needs execution to detect, which a static read
    of the spec cannot do. The docs say so rather than implying otherwise.
    """
    try:
        tree = ast.parse(tests_source)
    except SyntaxError:
        return "metadata.tests does not parse, so it cannot be a suite the runner can import."
    bases = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_test_case(node, bases):
            continue
        for member in node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not member.name.startswith("test"):
                continue
            for inner in ast.walk(member):
                if isinstance(inner, ast.Assert) or _assertion_name(inner).startswith("assert"):
                    found = True
                    if not _is_constant_assertion(inner):
                        return None
    if not found:
        return (
            "metadata.tests has no assertion inside a test* method of a unittest.TestCase, so "
            "unittest collects nothing that can fail. The suite passes for any artifact."
        )
    if not _references_module(tree, module):
        return (
            f"metadata.tests never names {module!r}, so it cannot read the artifact and nothing it "
            "asserts is a function of the submitted code. Import the module under test and assert "
            "against a value it returns."
        )
    return (
        "every assertion in metadata.tests is a constant, so the suite can never fail. A tautology "
        "is not a gate; assert against a value the artifact produces."
    )


def _has_required_field(metadata: dict[str, Any]) -> bool:
    """Does `metadata.fields` require at least one field?

    An optional field with no `expected` counterpart grades nothing: an absent
    field and any fabricated value both score the same.
    """
    fields = metadata.get("fields")
    if not isinstance(fields, dict):
        return False
    return any(isinstance(spec, dict) and spec.get("required") for spec in fields.values())


def _absent_contract_reason(spec: TaskSpec) -> str | None:
    """Why this spec's grader has no anchor, or None when it has one."""
    keys = GRADING_CONTRACT.get(spec.type)
    if keys is None:
        return None
    if spec.type == "extract":
        if spec.metadata.get("expected") or _has_required_field(spec.metadata):
            return None
        return (
            "metadata.fields requires no field and there is no metadata.expected, so the contract "
            "grades nothing: an absent optional field and any fabricated value score the same. Add "
            "a required field, or declare expected values."
        )
    if any(spec.metadata.get(key) for key in keys):
        return None
    if spec.type == "code":
        return (
            "no metadata.tests, so grading is expected_paths + static quality regexes. "
            "Any file with a byte in the right name passes."
        )
    return (
        f"no {' or '.join(keys)}, so type '{spec.type}' has no grading contract. "
        f"It computes {COMPUTED_CHECKS[spec.type]} against nothing."
    )


def check_absent_grading_contract(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """A self-anchored type with no anchor grades anything as correct.

    Generalises the old `code_without_tests`, which only asked whether
    `metadata.tests` was a non-empty string: a suite whose single test body is
    `pass` is audit-clean and always reports success.
    """
    reason = _absent_contract_reason(spec)
    if reason is None and spec.type == "code":
        reason = _suite_gate_reason(str(spec.metadata.get("tests") or ""), _declared_module(spec))
    if reason is None:
        return []
    return [
        Finding(
            rule="absent_grading_contract",
            severity=ERROR,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=reason,
        )
    ]


def check_presence_only_extract_contract(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """An `extract` contract of `required` fields and no `expected` grades presence.

    `orchestral/extract.py` treats `required` as a legitimate anchor and grades
    it on presence and type, never on a value — a deliberate choice, not a bug,
    so this is a warning and not a contradiction of the runner. But it means a
    fabricated value scores exactly as a correct one, `field_results` stays
    empty, and `score` falls out of the all-checks-pass branch at 1.0. The
    author is graded on having written the right keys with plausible types.
    """
    if spec.type != "extract":
        return []
    if not _has_required_field(spec.metadata) or spec.metadata.get("expected"):
        return []
    fields = sorted(
        name
        for name, field_spec in (spec.metadata.get("fields") or {}).items()
        if isinstance(field_spec, dict) and field_spec.get("required")
    )
    return [
        Finding(
            rule="presence_only_extract_contract",
            severity=WARN,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                f"metadata.expected is absent, so the contract grades presence and type on "
                f"{fields} and never compares a value: a fabricated value scores 1.0 exactly as a "
                "correct one. Declare metadata.expected."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Warnings — a model can score without doing the work
# ---------------------------------------------------------------------------


def _anchor_advice(spec: TaskSpec) -> str:
    """How to anchor this spec — only types whose grader reads text can be."""
    if VALIDATION_CHECKS.get(spec.type, frozenset()) & TOPIC_ANCHORS:
        return "Add has_required or matches_pattern to anchor the subject."
    advice = (
        f"no implemented check reads the content of this {spec.type} artifact, so there is no "
        "compliant way to anchor the subject from the spec: the topic checks are text-only and "
        "this type never grades text. Add a content check to the runner, or grade the artifact as "
        "a text-producing type."
    )
    if spec.metadata.get("required"):
        advice += (
            f" metadata.required is declared, but the {spec.type} grader never reads it, so it "
            "anchors nothing here."
        )
    return advice


def check_structural_only(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag specs where nothing in the grader requires topical content.

    Element checks (`has_title`, `has_cta`, `has_form`, `has_viewport`,
    `no_placeholder`) prove markup exists, not that the artifact is about the
    task's subject, so they do not clear this finding. Only `has_required`,
    `matches_pattern`, or — for the text-producing types only — declared
    `metadata.required` do.

    `image` / `video` are out of scope here: the artifact is encoded bytes, so
    a text token is not a looser anchor but an unimplemented one. See
    `check_judge_gated_media`.
    """
    if spec.type in SELF_ANCHORED_TYPES:
        return []
    if spec.type in BYTE_ARTIFACT_TYPES:
        return []
    if spec.type not in VALIDATION_CHECKS:
        return []
    if _has_topic_anchor(spec):
        return []
    return [
        Finding(
            rule="structural_only",
            severity=WARN,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                f"checks {sorted(effective_checks(spec))} never require topical content, so any "
                f"well-formed {spec.type} artifact passes. {_anchor_advice(spec)}"
            ),
        )
    ]


def check_judge_gated_media(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Media specs are topical only if a run actually carries a judge.

    `png_signature` / `mp4_signature` prove the bytes are a file of that
    format. Nothing in `validation:` can read pixels, so the topicality of an
    image or video artifact rests entirely on the vision judge — and a run
    invoked without `--judge` grades those specs on file format alone.
    """
    if spec.type not in BYTE_ARTIFACT_TYPES:
        return []
    return [
        Finding(
            rule="judge_gated_media",
            severity=INFO,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                f"a {spec.type} artifact is encoded bytes, so no text check can anchor its "
                f"subject. checks {sorted(effective_checks(spec))} prove format only; topicality "
                "rests on the vision judge. Score this spec from a run invoked with --judge, or "
                "exclude it from a headline number."
            ),
        )
    ]


def check_unanchored_fileset(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """`multi-file` grades names and byte counts; nothing checks the contents.

    A fileset is anchored only when the grader reads a file *body*: `has_paths`
    plus `has_content` with tokens in `metadata.required_content`. Every other
    `multi-file` spec lands in one of four states — no usable declared paths,
    declared gate inputs the grader never reads, declared paths checked only for
    existence, or a body check with nothing to look for.

    Error, not warn, because two of those states are the same defect as
    `unknown_validation_check`: the author declared a gate input and no check
    reads it, so deleting one token from `validation:` is enough to turn the
    declared gate off and reach green with no edit here. The gate naming the fix
    while exiting 0 was the hole. See `docs/task-audit.md`.
    """
    if spec.type != "multi-file":
        return []
    requested = set(spec.validation or ())
    # the same normalisation the runner uses, so the reported set is the set it
    # will actually look for
    declared = expected_paths(spec.metadata)
    content = required_content(spec.metadata)
    # `has_content` needs a body to exist for every path it names, so a declared
    # path it covers is read even with `has_paths` absent — and one it does not
    # cover is declared and unread, which is the hole this rule exists to close.
    covered = set(content) if requested & FILESET_BODY_CHECKS else set()

    # Declared-but-unread is the family the severity rests on, so each input the
    # author declared is reported against the check that would have read it.
    unread: list[str] = []
    if declared and "has_paths" not in requested and not set(declared) <= covered:
        unread.append(f"metadata.expected_paths declares {sorted(declared)} but has_paths is not requested")
    if content and "has_content" not in requested:
        unread.append(
            f"metadata.required_content declares tokens for {sorted(content)} but has_content is "
            "not requested"
        )

    if unread:
        detail = (
            f"{'; '.join(unread)}. The grader never reads what the spec declares, so deleting the "
            "matching validation token is enough to reach green with no edit to the spec's intent. "
            "Add it to validation."
        )
    elif covered:
        return []
    elif not declared and not content:
        detail = (
            "the fileset is unanchored: metadata.expected_paths yields no usable path, so any "
            "readable zip passes, including one containing junk.txt. Declare a list of paths and "
            "request has_paths."
        )
    elif requested & FILESET_BODY_CHECKS:
        detail = (
            "has_content is requested but metadata.required_content is empty, so every "
            "artifact fails on an unconfigured gate rather than on its contents."
        )
    else:
        detail = (
            f"has_paths only checks that {sorted(declared)} exist and are non-empty. "
            "A one-byte file per path passes; no check reads the file bodies."
        )
    return [
        Finding(
            rule="unanchored_fileset",
            severity=ERROR,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=detail,
        )
    ]


def check_prompt_states_answer(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag specs whose prompt already contains the graded answer."""
    if spec.type in ANSWER_DERIVABLE_TYPES:
        return []
    prompt = spec.prompt
    lowered = prompt.lower()
    leaks: list[str] = []
    for key in ("expected", "expected_answer"):
        for value in _scalars(spec.metadata.get(key)):
            if value.lower() in lowered:
                leaks.append(f"{key}={value!r}")
    reference = spec.metadata.get("reference_sql")
    if (
        isinstance(reference, str)
        and len(reference.strip()) >= MIN_TOKEN_LEN
        and _normalise(reference) in _normalise(prompt)
    ):
        leaks.append("reference_sql is quoted in the prompt")
    calls = spec.metadata.get("calls")
    if isinstance(calls, list):
        for call in calls:
            if _prompt_mentions_call(prompt, call):
                leaks.append(f"call {_route_key(call)!r}")
    if not leaks:
        return []
    return [
        Finding(
            rule="prompt_states_the_answer",
            severity=WARN,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                "the prompt states graded output, so a lookup or recitation scores the same as "
                f"reasoning: {'; '.join(sorted(set(leaks)))}."
            ),
        )
    ]


def check_answer_derivable(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag specs whose answer is fully readable in the prompt."""
    if spec.type not in ANSWER_DERIVABLE_TYPES:
        return []
    lowered = _normalise(spec.prompt)
    graded = _scalars(spec.metadata.get("expected")) + _scalars(spec.metadata.get("expected_answer"))
    if not graded:
        return []
    present = [v for v in graded if _normalise(v) in lowered]
    if not present or len(present) != len(graded):
        return []
    return [
        Finding(
            rule="answer_derivable_from_prompt",
            severity=WARN,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                f"all {len(graded)} graded value(s) appear verbatim in the prompt, so the ceiling "
                "is transcription and lookup. Add distractors or require a transformation."
            ),
        )
    ]


def check_memorization_risk(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag problems whose answers are near-certainly in the training corpus."""
    haystack = f"{spec.id}\n{spec.prompt}"
    hits = [name for name, pattern in MEMORIZATION_SIGNATURES if pattern.search(haystack)]
    if not hits:
        return []
    return [
        Finding(
            rule="memorization_risk",
            severity=WARN,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                f"matches known textbook signature(s) {hits}. A memorised answer scores the same as "
                "a solved one; pair it with a novel instance before quoting a leaderboard."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Info — measurement hygiene
# ---------------------------------------------------------------------------


def check_unlabeled_difficulty(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    if spec.metadata.get("difficulty"):
        return []
    return [
        Finding(
            rule="unlabeled_difficulty",
            severity=INFO,
            task_id=spec.id,
            path=str(path) if path else None,
            detail="no metadata.difficulty, so this spec cannot be excluded from a headline result.",
        )
    ]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _tokens(prompt: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", prompt.lower()) if len(t) >= 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def find_duplicate_families(
    specs: list[TaskSpec], *, min_family: int = 5, similarity: float = 0.8
) -> list[Finding]:
    """Cluster specs whose prompts are near-identical.

    One template repeated N times adds N to the denominator without adding N
    distinct problems, so aggregate pass rates inherit that template's
    difficulty. Union-find over prompt token Jaccard similarity.
    """
    tokens = [_tokens(spec.prompt) for spec in specs]
    parent = list(range(len(specs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for i in range(len(specs)):
        for j in range(i + 1, len(specs)):
            if _jaccard(tokens[i], tokens[j]) >= similarity:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(len(specs)):
        clusters.setdefault(find(i), []).append(i)

    findings: list[Finding] = []
    for members in clusters.values():
        if len(members) < min_family:
            continue
        ids = sorted(specs[i].id for i in members)
        shared = Counter(t for i in members for t in tokens[i]).most_common(6)
        findings.append(
            Finding(
                rule="near_duplicate_family",
                severity=INFO,
                task_id=None,
                path=None,
                detail=(
                    f"{len(ids)} specs share one prompt shape (jaccard >= {similarity}): "
                    f"{ids[0]}..{ids[-1]}. Most shared terms: {[t for t, _ in shared]}. "
                    "They are one problem counted many times."
                ),
            )
        )
    return findings


def check_holdout_arm(specs: list[TaskSpec], *, probe: Any = None) -> list[Finding]:
    """Contamination is unmeasurable until some specs are never published.

    An arm counts when the suite either holds a committed `metadata.holdout` spec
    or can generate one on demand. A committed holdout spec is a contradiction —
    it is in git, so it is a published problem wearing a holdout label — so the
    generated arm is the normal case and the committed spec is only accepted for
    a private tree that never gets published.

    The generator is not taken on trust: `probe` is called and must return real
    specs whose prompts are not already in the suite. A generator that is absent,
    raises, or emits a prompt the suite already contains leaves contamination
    unmeasured, and the finding stands. Accepting a declared-but-unverified arm
    would make this rule report the absence of a measurement while doing nothing
    to produce one.
    """
    if not specs:
        return []
    if any(bool(spec.metadata.get("holdout")) for spec in specs):
        return []

    detail_suffix = ""
    if probe is not None:
        try:
            generated = list(probe())
        except Exception as exc:  # a broken generator is not an arm
            generated = []
            detail_suffix = f" The generator failed to run: {type(exc).__name__}: {exc}."
        else:
            detail_suffix = ""
        if generated:
            published = {spec.prompt.strip() for spec in specs}
            fresh = [spec for spec in generated if spec.prompt.strip() not in published]
            if fresh and all(bool(spec.metadata.get("holdout")) for spec in fresh):
                return []

    return [
        Finding(
            rule="no_holdout_arm",
            severity=INFO,
            task_id=None,
            path=None,
            detail=(
                f"none of the {len(specs)} specs sets metadata.holdout and no holdout arm could "
                "be generated, so every problem is a published problem. Generate an arm with "
                "`harness.py holdout` and keep it out of runs-pub to make contamination checkable "
                f"instead of assumed.{detail_suffix}"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

PER_SPEC_RULES = (
    check_validation_names,
    check_absent_grading_contract,
    check_presence_only_extract_contract,
    check_structural_only,
    check_judge_gated_media,
    check_unanchored_fileset,
    check_prompt_states_answer,
    check_answer_derivable,
    check_memorization_risk,
    check_unlabeled_difficulty,
)


def audit_spec(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Run every per-spec rule against one spec."""
    findings: list[Finding] = []
    for rule in PER_SPEC_RULES:
        findings.extend(rule(spec, path))
    return findings


def audit_suite(
    specs: list[TaskSpec],
    paths: list[Path | None] | None = None,
    *,
    min_family: int = 5,
    similarity: float = 0.8,
    holdout_probe: Any = None,
) -> AuditReport:
    """Audit a whole suite. `paths` is parallel to `specs` when given.

    `holdout_probe` is a zero-argument callable returning generated holdout
    specs. Pass None to audit a tree as if no generator existed.
    """
    report = AuditReport(specs=len(specs))
    locations = paths or [None] * len(specs)
    for spec, path in zip(specs, locations, strict=True):
        for finding in audit_spec(spec, path):
            report.add(finding)
    for finding in find_duplicate_families(specs, min_family=min_family, similarity=similarity):
        report.add(finding)
    for finding in check_holdout_arm(specs, probe=holdout_probe):
        report.add(finding)
    return report


def default_holdout_probe() -> list[TaskSpec]:
    """Generate a small arm so the audit can check one exists and is usable."""
    from orchestral.holdout import generate_arm

    return generate_arm(4)


def audit_tree(root: Path | str = "tasks", **kwargs: Any) -> AuditReport:
    """Load and audit every spec under `root`."""
    loaded = load_specs(root)
    return audit_suite([spec for _, spec in loaded], [path for path, _ in loaded], **kwargs)
