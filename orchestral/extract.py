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

A contract that grades nothing fails closed (`contract_anchored`): the audit
rule `absent_grading_contract` already rejects these specs, and a spec that
bypasses the audit must not score 1.0 here either.
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
        return len(a) == len(b) and all(_strict_eq(x, y) for x, y in zip(a, b))
    return a == b


def _type_ok(declared: str, value: Any) -> bool:
    types = _TYPES.get(str(declared))
    if types is None:
        return True  # unknown type names don't fail closed — treated as `any`
    if declared in ("int", "number") and isinstance(value, bool):
        return False
    return isinstance(value, types)


def _as_mapping(metadata: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any]:
    """Read `metadata[key]` as a mapping; a malformed value contributes nothing.

    The audit already treats a non-dict `fields` as declaring no fields, and a
    contract is read before the artifact is parsed, so a wrong-shaped value is
    reached on every run rather than only on a parseable artifact. Dropping it
    keeps the grader total; recording why keeps the typo from being silent.
    """
    value = metadata.get(key)
    if isinstance(value, dict):
        return value
    if value is not None:
        errors.append(
            f"metadata.{key} is a {type(value).__name__}, not a mapping, so it declares "
            "nothing and was not applied to the grade."
        )
    return {}


def _unanchored_fields(
    fields: dict[str, Any], expected: dict[str, Any]
) -> tuple[list[str], bool]:
    """Which declared fields the contract never grades, and whether any anchor exists.

    A field is graded when it is `required` (checked for presence) or named in
    `expected` (deep-compared). A field that is neither is decorative: absent, it
    is skipped; present, only its type is read. An absent field and any
    fabricated value score the same, so declaring it anchors nothing.

    The second element is whether the contract grades anything at all, which
    covers the empty contract where `fields` is empty and there is no `expected`.
    """
    required = [
        name for name, spec in fields.items() if isinstance(spec, dict) and spec.get("required")
    ]
    unanchored = [name for name in fields if name not in expected and name not in required]
    return unanchored, bool(expected) or bool(required)


def check_extraction(metadata: dict[str, Any], artifact_text: str) -> dict[str, Any]:
    """Grade `artifact_text` against the metadata contract; returns a report."""
    report: dict[str, Any] = {
        "parsed": False,
        "checks": {
            "json_parses": False,
            "required_present": False,
            "types_ok": False,
            "contract_anchored": False,
        },
        "field_results": {},
        "missing_required": [],
        "errors": [],
        "score": None,
        "passes": False,
    }
    obj = extract_json(artifact_text)

    # Contract anchoring depends on metadata alone, so it is settled before the
    # artifact is looked at: a spec with no anchor fails closed whatever the
    # worker returned, and a parse failure must not be reported as a bad contract.
    # Reading it this early means a malformed contract is reached before the parse
    # early-returns, so both are normalised to a mapping: the audit treats a
    # non-dict `fields` as declaring no fields, and a spec author's typo must not
    # cost the run its record. A contract that ends up with nothing to grade still
    # fails closed below.
    fields = _as_mapping(metadata, "fields", report["errors"])
    expected = _as_mapping(metadata, "expected", report["errors"])
    unanchored, has_anchor = _unanchored_fields(fields, expected)
    report["checks"]["contract_anchored"] = has_anchor and not unanchored
    if unanchored:
        report["errors"].append(
            f"metadata.fields declares {', '.join(sorted(unanchored))} with neither "
            "`required: true` nor a metadata.expected value, so the contract grades nothing "
            "for it: an absent field and any fabricated value score the same. Set "
            "`required: true` or add a metadata.expected value."
        )
    elif not has_anchor:
        report["errors"].append(
            "metadata has no `fields` entry marked `required: true` and no metadata.expected, "
            "so the contract grades nothing: an empty artifact scores 1.0. Add a required "
            "field, or declare expected values."
        )

    if obj is None:
        report["errors"].append("artifact does not contain parseable JSON")
        return report
    report["parsed"] = True
    report["checks"]["json_parses"] = True
    if not isinstance(obj, dict):
        report["errors"].append("extracted JSON is not an object")
        report["score"] = 0.0
        return report

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
                report["errors"].append(f"{name}: expected {want!r}, got {got}")
        report["score"] = matched / len(expected)
    else:
        report["score"] = 1.0 if all(report["checks"].values()) else 0.0

    threshold = float(metadata.get("pass_threshold", 1.0))
    report["passes"] = (
        report["checks"]["contract_anchored"]
        and report["checks"]["required_present"]
        and report["checks"]["types_ok"]
        and report["score"] >= threshold
    )
    if len(json.dumps(obj)) > _PREVIEW:
        report["artifact_preview"] = json.dumps(obj)[:_PREVIEW]
    return report
