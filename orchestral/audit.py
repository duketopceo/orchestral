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

# Registry names the runner expands into other checks instead of assigning
# itself, so they never appear as a `checks[...]` key.
VALIDATION_SHORTHANDS = frozenset({"html"})

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
    return bool(spec.metadata.get("required"))


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


def _suite_asserts_anything(tests_source: str) -> bool:
    """Does the suite contain at least one real assertion?

    Parsed, not grepped, so the word "assert" in a docstring or a string
    literal does not pass for a gate. `ast.parse` does not execute the source.
    An unparseable suite fails closed: code that cannot be imported cannot gate
    anything.
    """
    try:
        tree = ast.parse(tests_source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name.startswith("assert"):
                return True
    return False


def _absent_contract_reason(spec: TaskSpec) -> str | None:
    """Why this spec's grader has no anchor, or None when it has one."""
    keys = GRADING_CONTRACT.get(spec.type)
    if keys is None:
        return None
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
    if reason is None and spec.type == "code" and not _suite_asserts_anything(
        str(spec.metadata.get("tests") or "")
    ):
        reason = (
            "metadata.tests contains no assertion, so the suite passes for any artifact "
            "including an empty one. Add a real assertion or self.assert* call."
        )
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


def _anchor_advice(task_type: str) -> str:
    """How to anchor this type — only types whose grader reads text can be."""
    if VALIDATION_CHECKS.get(task_type, frozenset()) & TOPIC_ANCHORS:
        return "Add has_required or matches_pattern to anchor the subject."
    return (
        f"no implemented check reads the content of this {task_type} artifact, so there is no "
        "compliant way to anchor the subject from the spec: the topic checks are text-only and "
        "this type never grades text. Add a content check to the runner, or grade the artifact as "
        "a text-producing type."
    )


def check_structural_only(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag specs where nothing in the grader requires topical content.

    Element checks (`has_title`, `has_cta`, `has_form`, `has_viewport`,
    `no_placeholder`) prove markup exists, not that the artifact is about the
    task's subject, so they do not clear this finding. Only `has_required`,
    `matches_pattern`, or declared `metadata.required` do.
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
                f"well-formed {spec.type} artifact passes. {_anchor_advice(spec.type)}"
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
