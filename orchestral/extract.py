"""Deterministic validator for `extract` tasks — JSON field grading.

A extract task ships a grading contract in `metadata`:

- `fields` — {name: {"type": "str|int|number|bool|list|object",
                    "required": bool, "enum": [...]}}  — schema-lite checks
- `expected` — {name: value} — deep-equality graded keys
- `pass_threshold` — float, default 1.0 — minimum score to pass

Score is the fraction of `expected` keys whose extracted value deep-equals
the expected one; with no `expected`, score is 1.0 when all field checks
pass. `passes` additionally requires every `required` field present and
type/enum-valid — partial credit never counts as a pass by accident.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_PREVIEW = 500

_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),  # bool is an int subclass; excluded below
    "number": (int, float),
    "bool": (bool,),
    "list": (list,),
    "object": (dict,),
    "any": (object,),
}


def extract_json(text: str) -> Any:
    """Pull a JSON value out of worker output: fenced block or raw text."""
    raw = str(text or "").strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # last resort: outermost {...} or [...] span
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = raw.find(open_ch), raw.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _strict_eq(a: Any, b: Any) -> bool:
    """Equality that keeps bool distinct from int/float (True == 1 in Python).

    Nested containers are compared element-wise so a bool hiding inside a
    list/dict cannot silently match an int.
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_strict_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_strict_eq(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _type_ok(declared: str, value: Any) -> bool:
    types = _TYPES.get(str(declared))
    if types is None:
        return True  # unknown type names don't fail closed — treated as `any`
    if declared in ("int", "number") and isinstance(value, bool):
        return False
    return isinstance(value, types)


def check_extraction(metadata: dict[str, Any], artifact_text: str) -> dict[str, Any]:
    """Grade `artifact_text` against the metadata contract; returns a report."""
    report: dict[str, Any] = {
        "parsed": False,
        "checks": {"json_parses": False, "required_present": False, "types_ok": False},
        "field_results": {},
        "missing_required": [],
        "errors": [],
        "score": None,
        "passes": False,
    }
    obj = extract_json(artifact_text)
    if obj is None:
        report["errors"].append("artifact does not contain parseable JSON")
        return report
    report["parsed"] = True
    report["checks"]["json_parses"] = True
    if not isinstance(obj, dict):
        report["errors"].append("extracted JSON is not an object")
        report["score"] = 0.0
        return report

    fields = metadata.get("fields") or {}
    expected = metadata.get("expected") or {}

    missing: list[str] = []
    type_errors: list[str] = []
    for name, spec in fields.items():
        spec = spec if isinstance(spec, dict) else {}
        required = bool(spec.get("required"))
        if required and name not in obj:
            missing.append(name)
            continue
        if name in obj:
            if "type" in spec and not _type_ok(str(spec["type"]), obj[name]):
                type_errors.append(f"{name}: expected {spec['type']}, got {type(obj[name]).__name__}")
            if "enum" in spec and not any(_strict_eq(obj[name], choice) for choice in spec["enum"]):
                type_errors.append(f"{name}: {obj[name]!r} not in enum {spec['enum']!r}")
    report["missing_required"] = missing
    report["checks"]["required_present"] = not missing
    report["checks"]["types_ok"] = not type_errors
    report["errors"].extend(type_errors)

    if expected:
        matched = 0
        for name, want in expected.items():
            ok = name in obj and _strict_eq(obj[name], want)
            report["field_results"][name] = ok
            matched += int(ok)
            if not ok:
                got = repr(obj.get(name))[:120]
                # The expected value is the answer key. `field_results` still
                # grades the field and the error names it, so the report says a
                # field mismatched and what the candidate produced — never what
                # was wanted. A stored `expected {want!r}` here would reach
                # report.json, the HTML report, and every published scrub.
                report["errors"].append(f"{name}: value mismatch (got {got})")
        report["score"] = matched / len(expected)
    else:
        report["score"] = 1.0 if all(report["checks"].values()) else 0.0

    threshold = float(metadata.get("pass_threshold", 1.0))
    report["passes"] = (
        report["checks"]["required_present"]
        and report["checks"]["types_ok"]
        and report["score"] >= threshold
    )
    if len(json.dumps(obj)) > _PREVIEW:
        report["artifact_preview"] = json.dumps(obj)[:_PREVIEW]
    return report
