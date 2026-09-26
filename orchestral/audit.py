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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import TaskSpec, load_task
from .fileset import expected_paths

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
    "multi-file": frozenset({"non_empty", "zip_signature", "has_paths"}),
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


# Types whose grader never reads `validation:` — they compute a fixed check set
# from `metadata` instead. Declaring checks on these specs is always a mistake.
IGNORES_VALIDATION = frozenset({"code", "sql", "extract", "api"})

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

# The `unittest` classes a suite's test methods can be collected from. Matched
# on the last dotted segment, so `unittest.case.TestCase` and
# `unittest.async_case.IsolatedAsyncioTestCase` resolve alongside the direct
# names — all three of those are documented unittest import forms.
_TEST_CASE_BASES = frozenset({"TestCase", "IsolatedAsyncioTestCase"})

# Types that grade against their own metadata anchor, so "no topic anchor in
# `validation:`" is not a finding for them. `constraint` and `needle` are *not*
# in this set: the runner uses them only as labelling flags and grades them
# through the same `_validate`, so a `validation: [html]` `constraint` spec gets
# `html_parses` and `non_empty` and nothing topical. Keying this on the type name
# was the same hole the anchor check just closed for `metadata.required`.
SELF_ANCHORED_TYPES = frozenset({"code", "sql", "extract", "api", "multi-file"})

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


# Shapes the grader can iterate. YAML gives a spec author a string, an int, a
# float, a bool, a date, a list or a mapping, and the grader writes `for t in
# required` — so a number, a bool or a date is a declaration it cannot read.
_DECLARATION_SHAPES = (str, list, tuple, dict, set, frozenset)


def _required_tokens(value: Any) -> list[str]:
    """The tokens `runner.py` will compare the artifact against.

    The runner writes `for t in required` and then `str(t).lower()`, so the shape
    of the declaration decides what is compared, not just its content:

    - a list or tuple contributes its items;
    - a mapping contributes its **keys** — `required: {"kite": true}` really is
      enforced, so treating it as an absent declaration was factually wrong;
    - a bare string contributes its **characters**, so `required: kite` asks only
      that the artifact contain `k`, `i`, `t` and `e`. A token of one character
      is satisfied by nearly any artifact and therefore declares nothing, which
      is also why `[""]`, `[0]` and `["a"]` declare nothing.

    Modelling the iteration rather than the YAML is the point: the audit's list
    has to be the runner's list, or the two disagree about what is anchored.

    A value the grader cannot iterate at all is a *malformed* declaration, not an
    absent one, and it is reported as such rather than raised. `required: 5`
    raised `TypeError` out of here and took the gate's answer for every other
    spec with it, and this was the only metadata read in the file without a type
    guard — the shape of the bug was an omission, so the guard is explicit and
    every other reader is covered by `TestHostileMetadataNeverRaises`.
    """
    if not value:
        return []
    if not isinstance(value, _DECLARATION_SHAPES):
        return []
    return [token for item in value for token in _scalar_token(item)]


def _scalar_token(value: Any) -> list[str]:
    """One declared token, or nothing when the value is not a token at all.

    The token is `str(value)` unstripped, because that is what the runner
    compares: `str(t).lower() in lowered` and nothing else. Stripping first made
    the audit's list disagree with the runner's in both directions — it treated
    `" kite "` as `kite` (so an artifact containing the bare word scored 0 while
    the audit called the spec anchored) and it treated `"  "` as a token (which
    ordinary indented HTML satisfies). A token that is entirely whitespace is
    rejected outright instead, which is stricter than the runner in the safe
    direction and keeps the two lists identical everywhere else.
    """
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return [text] if len(text) > 1 and text.strip() else []
    # A nested container is stringified by the runner (`str({'kite': True})`), so
    # it compares the artifact against a repr. That is a check nothing can pass,
    # which anchors no topic.
    return []


def _pattern_declared(value: Any) -> bool:
    """Is `metadata.pattern` something the runner will compile?

    `re.search(str(pattern), ...)` takes the whole value, so a pattern is never
    iterated and a bare string is a perfectly good one. Whether the regex is
    *strong* enough to anchor a topic is a separate question from whether one was
    declared, and only the second one is decided here.
    """
    return bool(str(value).strip()) if value else False


def _has_topic_anchor(spec: TaskSpec) -> bool:
    """Will the grader actually compare the artifact against the subject?

    The runner reads `metadata.required` in exactly one place, inside
    `if "has_required" in requested`, and `metadata.pattern` inside
    `if "matches_pattern" in requested`. So a declaration anchors the subject
    only when that check is requested *and* the declaration names something.

    Either half alone proves nothing, and both halves alone have been the bug:
    keying the exemption on the task *type* let one word of YAML silence the
    finding, and letting `has_required` clear it on its own meant
    `validation: [html, has_required]` with no `required` silenced it through
    the other key — a configuration the runner itself reports as an error.
    """
    checks = effective_checks(spec)
    if "has_required" in checks and _required_tokens(spec.metadata.get("required")):
        return True
    return "matches_pattern" in checks and _pattern_declared(spec.metadata.get("pattern"))


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
    # `method` is spec-controlled and the same line already escapes `path`.
    # Interpolating it raw makes the gate itself the denial of service: a method
    # of `(A+)+B` is a nested quantifier, and the backtracking cost doubles every
    # two characters, so a 30-character prompt is measured in minutes and a
    # 50-character one in hours. No audit finding is worth that, and the word
    # boundary already restricts a real method name to something like `GET`.
    pattern = re.compile(rf"\b{re.escape(method)}(?:s|es)?\b\W{{0,3}}{re.escape(path)}\b", re.I)
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


# Folded operations are pure arithmetic and predicate evaluation over constants.
# `ast.literal_eval` is not enough: it refuses a `Compare` or a `UnaryOp`, so
# `assert 1 == 1` and `assert not None` read no value yet do not fold.
_UNARY_OPS = {ast.Not: lambda v: not v, ast.USub: lambda v: -v, ast.UAdd: lambda v: +v}
_BINARY_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
    ast.BitAnd: lambda a, b: a & b,
    ast.BitOr: lambda a, b: a | b,
    ast.BitXor: lambda a, b: a ^ b,
}
_COMPARE_OPS: dict[type, Callable[[Any, Any], bool]] = {
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
# Pure builtins, so folding a call cannot execute model code.
_FOLDABLE_CALLS: dict[str, Callable[..., Any]] = {
    "len": len,
    "bool": bool,
    "str": str,
    "int": int,
    "float": float,
    "list": list,
    "tuple": tuple,
    "dict": dict,
    "set": set,
    "frozenset": frozenset,
    "sorted": sorted,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "any": any,
    "all": all,
    "repr": repr,
    "chr": chr,
    "ord": ord,
}
# A fold has to terminate. `2**2**2**2**2**2` doubles in bit length per level
# and `'ab' * 10**9 * 10**9` grows a string, so an eager operator turns one
# hostile suite into a wedged gate — `harness.py audit` answers for the whole
# tree, so that is a total outage, not one bad spec. `ast.literal_eval` refused
# both instantly; these caps are what put the capability back on a leash.
# Refusing to fold is the conservative direction: the assertion is then treated
# as possibly reading the artifact, which is what a real assertion looks like.
_MAX_FOLD_BITS = 4096
_MAX_FOLD_LEN = 4096
_MAX_FOLD_DEPTH = 200


def _too_large(value: Any) -> bool:
    """Is this folded value too big to have come from cheap arithmetic?"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value.bit_length() > _MAX_FOLD_BITS
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
        return len(value) > _MAX_FOLD_LEN
    return False


def _blows_up(op: type, left: Any, right: Any) -> bool:
    """Would evaluating this operator do unbounded work on these operands?

    Two shapes grow far faster than their operands suggest: `a ** b`, whose
    result carries about `b * bit_length(a)` bits, and `seq * n`, whose result
    is `len(seq) * n` long. Both are predictable from the operands, so both are
    refused before the work happens rather than after. Every other operator is
    bounded by the size of its operands, which `_too_large` already checks.
    """
    if op is ast.Pow:
        exponent = abs(right) if isinstance(right, int) and not isinstance(right, bool) else 0
        base_bits = left.bit_length() if isinstance(left, int) and not isinstance(left, bool) else 1
        return exponent * base_bits > _MAX_FOLD_BITS
    if op is ast.Mult:
        return any(
            _is_repeat_too_long(sequence, count)
            for sequence, count in ((left, right), (right, left))
        )
    return False


def _is_repeat_too_long(sequence: Any, count: Any) -> bool:
    """Would `sequence * count` build something past the length cap?"""
    if not isinstance(sequence, (str, bytes, list, tuple)):
        return False
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return False
    return len(sequence) * count > _MAX_FOLD_LEN


_FOLD_FAILURE = (
    ValueError, TypeError, ZeroDivisionError, OverflowError, IndexError, KeyError,
    AttributeError, RecursionError, MemoryError,
)


def _const(node: ast.AST, depth: int = 0) -> tuple[bool, Any]:
    """Fold `node` to a constant, or report that it is not provable.

    Returns `(True, value)` only when the node provably reads nothing from the
    artifact. Anything that touches a name, an attribute, a subscript of a
    non-literal, or an unknown call is left unfolded, so a real assertion is
    never mistaken for a constant.

    A `Compare` folds to the boolean it evaluates to, including `False`. That
    is what lets `assert not (1 == 2)` be caught: the inner comparison is a
    constant, so the `not` over it is too.
    """
    if depth > _MAX_FOLD_DEPTH:
        return False, None
    if isinstance(node, ast.Constant):
        return True, node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items: list[Any] = []
        for element in node.elts:
            ok, value = _const(element, depth + 1)
            if not ok:
                return False, None
            items.append(value)
        if isinstance(node, ast.Tuple):
            return True, tuple(items)
        if isinstance(node, ast.Set):
            try:
                return True, set(items)
            except TypeError:  # unhashable constant element
                return False, None
        return True, items
    if isinstance(node, ast.Dict):
        pairs: list[tuple[Any, Any]] = []
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:  # {**other} reads a value
                return False, None
            key_ok, key = _const(key_node, depth + 1)
            value_ok, value = _const(value_node, depth + 1)
            if not (key_ok and value_ok):
                return False, None
            pairs.append((key, value))
        try:
            return True, dict(pairs)
        except TypeError:
            return False, None
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        ok, operand = _const(node.operand, depth + 1)
        if not ok or _too_large(operand):
            return False, None
        try:
            folded = _UNARY_OPS[type(node.op)](operand)
        except _FOLD_FAILURE:
            return False, None
        return (False, None) if _too_large(folded) else (True, folded)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left_ok, left = _const(node.left, depth + 1)
        right_ok, right = _const(node.right, depth + 1)
        if not (left_ok and right_ok):
            return False, None
        if _too_large(left) or _too_large(right) or _blows_up(type(node.op), left, right):
            return False, None
        try:
            folded = _BINARY_OPS[type(node.op)](left, right)
        except _FOLD_FAILURE:
            return False, None
        return (False, None) if _too_large(folded) else (True, folded)
    if isinstance(node, ast.BoolOp):
        # `and`/`or` short-circuit, so `False and f(x)` folds even though f is
        # unknown — a decisive operand settles the result on its own.
        is_and = isinstance(node.op, ast.And)
        last: Any = None
        unknown = False
        for value_node in node.values:
            ok, value = _const(value_node, depth + 1)
            if not ok:
                unknown = True
                continue
            if is_and and not value:
                return True, False
            if not is_and and value:
                return True, True
            last = value
        if unknown:
            return False, None
        return True, bool(last)
    if isinstance(node, ast.Compare):
        left_ok, left = _const(node.left, depth + 1)
        if not left_ok:
            return False, None
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right_ok, right = _const(comparator, depth + 1)
            if not right_ok or type(op) not in _COMPARE_OPS:
                return False, None
            try:
                step = _COMPARE_OPS[type(op)](left, right)
            except _FOLD_FAILURE:
                return False, None
            if not step:
                # A provably false comparison is still a constant. It just does
                # not contribute a truthy value, so the chain ends here.
                return True, False
            left = right
        return True, True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fold = _FOLDABLE_CALLS.get(node.func.id)
        # `node.args` is empty whenever every argument is a keyword, so the
        # keyword test has to be here too: `self.assertEqual(first=f(x),
        # second=y)` has no positional argument and is not an empty call.
        if fold is None or node.keywords:
            return False, None
        args: list[Any] = []
        for arg in node.args:
            ok, value = _const(arg, depth + 1)
            if not ok:
                return False, None
            args.append(value)
        try:
            return True, fold(*args)
        except _FOLD_FAILURE:
            return False, None
    return False, None


def _assertion_constant(node: ast.AST) -> tuple[bool, bool | None]:
    """`(is a constant, and if so the value it takes for any artifact)`.

    A constant is `True` (passes for any artifact) or `False` (fails for every
    artifact); the polarity is `None` when the assertion method decides it rather
    than the arguments.

    Method-agnostic on purpose. Enumerating `assert*` names was the first
    version's mistake: it covered four of the methods `unittest` provides, and
    the ones it missed (`assertNotEqual(1, 2)`, `assertIn(1, [1, 2])`) audited
    clean while scoring 1.0 against a stub. The question is not *which*
    assertion it is but whether anything in it can read the artifact — so an
    assertion every expression of which folds is a constant, whatever it is
    called. Staying method-agnostic is why an `assert*` call reports no polarity:
    whether it passes or fails follows from the method, not from the arguments.
    """
    if isinstance(node, ast.Assert):
        ok, value = _const(node.test)
        return (True, bool(value)) if ok else (False, None)
    if isinstance(node, ast.Call):
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr.startswith("assert")):
            return False, None
        # Both argument kinds count. `all()` over an empty `node.args` is
        # vacuously True, so checking only the positional arguments reported
        # `self.assertEqual(first=f(x), second=y)` as a constant.
        values = [*node.args, *(keyword.value for keyword in node.keywords)]
        if not values:
            return True, True  # a bare self.assertTrue() cannot read anything
        if all(_const(value)[0] for value in values):
            return True, None
    return False, None


def _dotted(node: ast.expr) -> str | None:
    """`unittest.case.TestCase` as a dotted string, or None if not plain names."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _unittest_names(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """`(local name -> its `unittest` member, every locally bound import name)`.

    Matching a base by the bare name `TestCase` rejected
    `from unittest import TestCase as TC`, which is the common idiom and a
    suite that does discriminate. Resolving through the import is the only
    way to tell `unittest.TestCase` from a same-named local class.

    The second set is every name *any* import binds, `unittest` or not. Without
    it, `from mypkg import unittest` is invisible here, so the name looks unbound
    and the resolver falls back to treating it as `unittest` itself — outside the
    package guard rather than through it.
    """
    names: dict[str, str] = {}
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                bound.add(alias.asname or root)
                if alias.name == "unittest" or alias.name.startswith("unittest."):
                    # `import unittest.case` binds the *root* name `unittest`, so
                    # the root has to resolve as well as the full dotted form.
                    names.setdefault(root, root)
                    names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound.add(alias.asname or alias.name)
            if node.module == "unittest" or node.module.startswith("unittest."):
                for alias in node.names:
                    names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names, bound


def _base_target(base: ast.expr, imports: dict[str, str], bound: set[str]) -> str | None:
    """Resolve a base-class reference to a dotted `unittest` name, or None.

    The test is on the resolved *package*, not the first segment: `from
    unittest import case` binds `case` to `unittest.case`, and comparing
    `imports["case"] == "unittest"` reported that documented idiom as a suite
    that passes for any artifact. It did not.

    A name no import binds falls back to itself, which is what makes a bare
    `unittest.TestCase` resolve in a suite that never imports it. A name some
    *other* import binds does not: it is not `unittest`, whatever it is called.
    """
    dotted = _dotted(base)
    if dotted is None:
        return None
    head, _, tail = dotted.partition(".")
    if head in bound and head not in imports:
        return None
    resolved = imports.get(head, head)
    if resolved != "unittest" and not resolved.startswith("unittest."):
        return None
    return f"{resolved}.{tail}" if tail else resolved


def _is_test_case(node: ast.ClassDef, locals_: dict[str, ast.ClassDef],
                  imports: dict[str, str], bound: set[str]) -> str:
    """Resolve this class to `unittest.TestCase` or `IsolatedAsyncioTestCase`.

    Returns "" when it is neither, "sync" for plain `TestCase`, and "async" for
    `IsolatedAsyncioTestCase`. A cyclic base list cannot be resolved. The
    submodule form is matched on the last segment, so `unittest.case.TestCase`
    and `unittest.async_case.IsolatedAsyncioTestCase` resolve the same way the
    direct names do.

    The base chain is walked with an explicit worklist rather than by
    recursion. Chain *length* is not bounded the way tree depth is: each class in
    a 1000-long `class C1(C0): pass` chain is a separate top-level statement, so
    the parser accepts it and the recursion overflowed the stack, taking the
    gate's answer for every other spec with it. A depth cap would trade that
    crash for a false positive on a chain that really does end at a `TestCase`,
    so the chain is walked to its end and the answer is exact.
    """
    pending = [node]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current.name in seen:
            continue
        seen.add(current.name)
        for base in current.bases:
            target = _base_target(base, imports, bound)
            if target is not None:
                leaf = target.rsplit(".", 1)[-1]
                if leaf in _TEST_CASE_BASES:
                    return "async" if leaf == "IsolatedAsyncioTestCase" else "sync"
            # a base may be another class defined in the same suite
            dotted = _dotted(base)
            local = locals_.get(dotted) if dotted else None
            if local is not None:
                pending.append(local)
    return ""


def _is_effect_free(body: list[ast.stmt]) -> bool:
    """Does this method body provably do nothing when it runs?

    `pass`, a docstring, `...`, a `global`/`nonlocal` declaration and a bare
    annotation are the statements with no effect. This is what separates a suite
    of inert tests from one that assembles its assertion at runtime: both have no
    assertion the audit can *see*, but only the first cannot gate anything.
    """
    for statement in body:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue  # a docstring, or a bare literal
        if isinstance(statement, (ast.Global, ast.Nonlocal)):
            continue  # a declaration binds nothing and evaluates nothing
        if isinstance(statement, ast.AnnAssign) and statement.value is None:
            continue  # a bare annotation: no value, no call, no effect
        return False
    return True


# The suite could not be read at all. A `RecursionError` from a deeply nested
# constant, or a `MemoryError` from a very long one, is a resource limit on the
# audit, not a defect in the spec — but it still leaves the gate unable to say
# whether the suite gates anything, so it is reported like any other unanalysable
# suite rather than raised.
_UNREADABLE_SUITE = (
    "metadata.tests is too deeply nested for the audit to read, so it cannot confirm the suite gates "
    "anything. Keep assertions shallow and reference the solution's output in a local first."
)
# No assertion the audit can find that unittest will actually run.
_NO_COLLECTABLE_ASSERTION = (
    "metadata.tests has no assertion inside a collectable test* method of a "
    "unittest.TestCase, so nothing in it can fail. The suite passes for any artifact."
)
# Assertions are there, and every one of them is the same for any artifact.
#
# A constant folds to True (passes for any artifact) or to False (fails for every
# artifact). Only `assert <expr>` exposes which, so there are two wordings and the
# second is used whenever the polarity is not knowable without enumerating the
# `assert*` methods — enumerating them is what the method-agnostic check exists
# to avoid. The neutral wording is true for both polarities: a suite whose
# outcome never varies with the artifact is not testing the artifact, whichever
# way it varies.
_CONSTANT_ASSERTIONS_PASSES = (
    "every assertion in metadata.tests is a constant: it evaluates the same for any artifact, so the "
    "suite passes for any artifact. Assert against something the solution produces."
)
_CONSTANT_ASSERTIONS_FAILS = (
    "every assertion in metadata.tests is a constant and at least one of them fails for every "
    "artifact, so the suite scores 0 whatever the solution produces. Assert against something the "
    "solution produces."
)
_CONSTANT_ASSERTIONS_NEUTRAL = (
    "every assertion in metadata.tests is a constant: the suite's outcome does not depend on what the "
    "solution produced, so it cannot separate one artifact from another. Assert against something the "
    "solution produces."
)


def _suite_gate_reason(tests_source: str) -> str | None:
    """Why the suite cannot gate anything, or None when it can.

    Four properties are decidable from the source alone, and only these:

    - the suite parses — code that cannot be imported cannot gate anything;
    - it carries an assertion `unittest` will actually collect: a `test*` method
      of a `TestCase` subclass, resolved through the module's imports. A
      coroutine test on a plain `TestCase` does not count, because unittest
      never awaits it and reports the suite as passing anyway;
    - that assertion is not a constant, so it can discriminate between artifacts;
    - a test body that is inert (`pass`, a docstring) is not a gate.

    **Not provable by a static read.** An assertion arranged by the test itself
    — `self.flag = True` then `self.assertTrue(self.flag)` — provably passes
    for any artifact. Detecting that needs execution: run the suite against a
    stub and require it to fail. This audit executes nothing.

    A test body that does something but shows no assertion to the audit is not
    reported: an assertion assembled at runtime is a real gate, and calling it
    a constant would be false in both halves.
    """
    try:
        tree = ast.parse(tests_source)
    except SyntaxError:
        return "metadata.tests does not parse, so it cannot be a suite the runner can import."
    except (RecursionError, MemoryError):
        return _UNREADABLE_SUITE
    imports, bound = _unittest_names(tree)
    locals_ = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    collectable = False
    saw_assertion = False
    did_something = False
    polarities: list[bool | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        kind = _is_test_case(node, locals_, imports, bound)
        if not kind:
            continue
        for member in node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not member.name.startswith("test"):
                continue
            # a coroutine test only runs on an async test case
            if isinstance(member, ast.AsyncFunctionDef) and kind != "async":
                continue
            collectable = True
            if not _is_effect_free(member.body):
                did_something = True
            for inner in ast.walk(member):
                is_assertion = isinstance(inner, ast.Assert) or (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr.startswith("assert")
                )
                if not is_assertion:
                    continue
                saw_assertion = True
                is_constant, polarity = _assertion_constant(inner)
                if not is_constant:
                    return None
                polarities.append(polarity)
    if not collectable or (not saw_assertion and not did_something):
        return _NO_COLLECTABLE_ASSERTION
    if not saw_assertion:
        return None
    # Every assertion is a constant. Which message depends on the one thing the
    # folder can know: a constant that folds to False fails for *every* artifact,
    # so saying the suite "passes for any artifact" there would be the same false
    # claim in the other direction.
    if False in polarities:
        return _CONSTANT_ASSERTIONS_FAILS
    if all(polarity is True for polarity in polarities):
        return _CONSTANT_ASSERTIONS_PASSES
    return _CONSTANT_ASSERTIONS_NEUTRAL


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
        reason = _suite_gate_reason(str(spec.metadata.get("tests") or ""))
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


# ---------------------------------------------------------------------------
# Warnings — a model can score without doing the work
# ---------------------------------------------------------------------------


def _anchor_advice(spec: TaskSpec) -> str:
    """How to anchor this spec — only types whose grader reads text can be."""
    checks = effective_checks(spec)
    # What advice is possible keys on what the *type* can grade, not on what
    # this spec happens to request: `html` implements `has_required` whether or
    # not this particular spec asked for it.
    if VALIDATION_CHECKS.get(spec.type, frozenset()) & TOPIC_ANCHORS:
        advice = "Add has_required or matches_pattern to anchor the subject."
    else:
        advice = (
            f"no implemented check reads the content of this {spec.type} artifact, so there is no "
            "compliant way to anchor the subject from the spec: the topic checks are text-only and "
            "this type never grades text. Add a content check to the runner, or grade the artifact "
            "as a text-producing type."
        )
    if _required_tokens(spec.metadata.get("required")) and "has_required" not in checks:
        advice += (
            " metadata.required is declared but has_required is not requested, and the grader reads "
            "metadata.required only inside the has_required check, so it anchors nothing here."
        )
    if checks & TOPIC_ANCHORS and not _has_topic_anchor(spec):
        required = spec.metadata.get("required")
        if required and not isinstance(required, _DECLARATION_SHAPES):
            # A declaration the grader cannot iterate is a different defect from
            # one that names too little, and the runner raises on it at grading
            # time. Telling the author to declare a list of words would send them
            # to fix the wrong thing — the F6 defect, reached through the shape.
            advice += (
                f" metadata.required is a {type(required).__name__}, and the grader iterates it, so it "
                "raises on this spec rather than comparing anything. Declare it as a list of words."
            )
        else:
            advice += (
                " A check is requested but its declaration names nothing the grader can compare against: "
                "metadata.required is iterated, so a bare string is compared character by character and a "
                "one-character token is satisfied by almost any artifact, and metadata.pattern is compiled "
                "as written. Declare a list of words, or a pattern specific enough to exclude a generic "
                "artifact."
            )
    return advice


def check_structural_only(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag specs where nothing in the grader requires topical content.

    Element checks (`has_title`, `has_cta`, `has_form`, `has_viewport`,
    `no_placeholder`) prove markup exists, not that the artifact is about the
    task's subject, so they do not clear this finding. Only a topic check that
    is requested *and* backed by a non-empty declaration does — see
    `_has_topic_anchor` for why either half alone proves nothing.
    """
    if spec.type in SELF_ANCHORED_TYPES:
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


def check_unanchored_fileset(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """`multi-file` grades names and byte counts; nothing checks the contents.

    Every `multi-file` spec lands here in one of three states: no declared
    paths, declared paths the grader never looks for, or declared paths checked
    only for existence. None of them reads a file body.
    """
    if spec.type != "multi-file":
        return []
    # the same normalisation the runner uses, so the reported set is the set it
    # will actually look for
    declared = expected_paths(spec.metadata)
    if not declared:
        detail = (
            "the fileset is unanchored: metadata.expected_paths yields no usable path, so any "
            "readable zip passes, including one containing junk.txt. Declare a list of paths and "
            "request has_paths."
        )
    elif "has_paths" not in set(spec.validation or ()):
        detail = (
            f"metadata.expected_paths declares {sorted(declared)} but has_paths is not requested, "
            "so the grader never looks for them. Add has_paths to validation."
        )
    else:
        detail = (
            f"has_paths only checks that {sorted(declared)} exist and are non-empty. "
            "A one-byte file per path passes; no check reads the file bodies."
        )
    return [
        Finding(
            rule="unanchored_fileset",
            severity=WARN,
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


def check_holdout_arm(specs: list[TaskSpec]) -> list[Finding]:
    """Contamination is unmeasurable until some specs are never published."""
    if not specs:
        return []
    if any(bool(spec.metadata.get("holdout")) for spec in specs):
        return []
    return [
        Finding(
            rule="no_holdout_arm",
            severity=INFO,
            task_id=None,
            path=None,
            detail=(
                f"none of the {len(specs)} specs sets metadata.holdout, so every problem is a "
                "published problem. Mark a small arm holdout and keep it out of runs-pub to make "
                "contamination checkable instead of assumed."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

PER_SPEC_RULES = (
    check_validation_names,
    check_absent_grading_contract,
    check_structural_only,
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
) -> AuditReport:
    """Audit a whole suite. `paths` is parallel to `specs` when given."""
    report = AuditReport(specs=len(specs))
    locations = paths or [None] * len(specs)
    for spec, path in zip(specs, locations, strict=True):
        for finding in audit_spec(spec, path):
            report.add(finding)
    for finding in find_duplicate_families(specs, min_family=min_family, similarity=similarity):
        report.add(finding)
    for finding in check_holdout_arm(specs):
        report.add(finding)
    return report


def audit_tree(root: Path | str = "tasks", **kwargs: Any) -> AuditReport:
    """Load and audit every spec under `root`."""
    loaded = load_specs(root)
    return audit_suite([spec for _, spec in loaded], [path for path, _ in loaded], **kwargs)
