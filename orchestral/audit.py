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

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import TaskSpec, load_task
from .fileset import required_content

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


def check_code_has_tests(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """A `code` spec with no hidden suite has no behavioural gate at all."""
    if spec.type != "code":
        return []
    if str(spec.metadata.get("tests") or "").strip():
        return []
    return [
        Finding(
            rule="code_without_tests",
            severity=ERROR,
            task_id=spec.id,
            path=str(path) if path else None,
            detail=(
                "no metadata.tests, so grading is expected_paths + static quality regexes. "
                "Any file with a byte in the right name passes."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Warnings — a model can score without doing the work
# ---------------------------------------------------------------------------


def check_structural_only(spec: TaskSpec, path: Path | None = None) -> list[Finding]:
    """Flag specs where nothing in the grader requires topical content.

    Element checks (`has_title`, `has_cta`, `has_form`, `has_viewport`,
    `no_placeholder`) prove markup exists, not that the artifact is about the
    task's subject, so they do not clear this finding. Only `has_required`,
    `matches_pattern`, or declared `metadata.required` do.

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
                f"well-formed {spec.type} artifact passes. Add has_required or matches_pattern to "
                "anchor the subject."
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
    """`multi-file` grades names and byte counts; nothing checks the contents."""
    if spec.type != "multi-file":
        return []
    requested = set(spec.validation or [])
    if "has_paths" not in requested:
        return []
    # A body-reading check only anchors the fileset once it has tokens to look
    # for. Requested-but-unconfigured, `has_content` fails every artifact, so
    # the gate fires but the spec is not a graded site and the finding stands.
    if requested & FILESET_BODY_CHECKS and required_content(spec.metadata):
        return []
    declared = spec.metadata.get("expected_paths") or []
    if not declared:
        return []  # has_paths already fails closed on an empty declaration
    if requested & FILESET_BODY_CHECKS:
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
    check_code_has_tests,
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
