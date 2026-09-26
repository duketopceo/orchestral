"""Deterministic validator for `extract` tasks — JSON field grading.

A extract task ships a grading contract in `metadata`:

- `fields` — {name: {"type": "str|int|number|bool|list|object",
                    "required": bool, "enum": [...]}}  — schema-lite checks
- `expected` — {name: value} — deep-equality graded keys
- `pass_threshold` — score floor in (0, 1], default 1.0 — minimum score to
  pass; anything else is a spec error and fails the run closed

Score is the fraction of `expected` keys whose extracted value deep-equals
the expected one; with no `expected`, score is 1.0 when all field checks
pass. `passes` additionally requires every `required` field present and
type/enum-valid — partial credit never counts as a pass by accident.

A contract that grades nothing fails closed (`contract_anchored`): an empty
artifact would score 1.0 against it, so the grader refuses it whether or not the
audit ran. The audit rule `absent_grading_contract` rejects the same specs
upstream, but `orchestral/audit.py` is not on this branch — it lands with
`pr50`/`pr67` (DUK-94), so that half of the note is a forward reference until
one of them merges.
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


def _as_mapping(
    metadata: dict[str, Any], key: str, errors: list[str]
) -> tuple[dict[str, Any], bool]:
    """Read `metadata[key]` as a mapping; a malformed value contributes nothing.

    The audit already treats a non-dict `fields` as declaring no fields, and a
    contract is read before the artifact is parsed, so a wrong-shaped value is
    reached on every run rather than only on a parseable artifact. Dropping it
    keeps the grader total; recording why keeps the typo from being silent.

    Returns the mapping and whether a value was declared but unreadable — `None`
    is not a malformation, it means "not declared".
    """
    value = metadata.get(key)
    if isinstance(value, dict):
        return value, False
    if value is not None:
        errors.append(
            f"metadata.{key} is a {type(value).__name__}, not a mapping, so it declares "
            "nothing and was not applied to the grade."
        )
    return {}, value is not None


def _as_threshold(metadata: dict[str, Any], errors: list[str]) -> tuple[float, bool]:
    """Read `metadata.pass_threshold` as a score floor in (0, 1]; 1.0 when undeclared.

    `passes` is gated on `score >= threshold`, so a floor of `0` declares no floor
    at all: a completely wrong artifact scores 0.0, clears the gate, and passes
    with its value mismatches still sitting in `errors`. The floor is therefore
    (0, 1], and anything outside it is the spec author's error — recorded and
    failed closed rather than clamped, because clamping to an epsilon still
    passes the wrong answer and clamping to 1.0 silently overrides the author.

    A `bool` is refused rather than coerced. `float(False) == 0.0` and
    `float(True) == 1.0`, so `pass_threshold: no` becomes "anything passes" while
    `pass_threshold: yes` works by accident: the coercion is invisible in the one
    direction that fails safe, which is why it survived review.

    A floor above 1 needs no branch. A score is a fraction, so it can never clear
    one and the run fails closed without help.

    Reading a bad value must not raise. This is reached through
    `Runner._validate_extract` inside the run's `try:`, where the handler set
    `meta.status = "failed"`, logged `run.failed`, and re-raised — so one typo in
    one spec destroyed the whole run after the model had been paid. As in
    `_as_mapping`, `None` is not a malformation: it means "not declared".

    Returns the floor and whether the run may pass at all.
    """
    value = metadata.get("pass_threshold", 1.0)
    if value is None:
        return 1.0, True
    if isinstance(value, bool):
        errors.append(
            "metadata.pass_threshold is a bool, not a score in (0, 1], so it declares "
            "no floor and this run cannot pass. Give pass_threshold a number in (0, 1]."
        )
        return 1.0, False
    try:
        floor = float(value)
    except (TypeError, ValueError):
        errors.append(
            f"metadata.pass_threshold is a {type(value).__name__}, not a number, so it "
            "declares no floor and this run cannot pass. Give pass_threshold a number "
            "in (0, 1]."
        )
        return 1.0, False
    # `not floor > 0` rather than `floor <= 0` so a NaN floor, which no score can
    # clear either, is reported here instead of passing through as a silent NaN.
    if not floor > 0:
        errors.append(
            f"metadata.pass_threshold is {value!r}, not a score above 0, so it declares "
            "no usable floor and this run cannot pass: `passes` is gated on "
            "`score >= pass_threshold`, so a floor of 0 lets a wrong artifact pass. "
            "Give pass_threshold a number in (0, 1]."
        )
        return 1.0, False
    return floor, True


def _unanchored_fields(
    fields: dict[str, Any], expected: dict[str, Any]
) -> tuple[list[Any], bool]:
    """Which declared fields the contract never grades, and whether any anchor exists.

    A field is graded when it is `required` (checked for presence) or named in
    `expected` (deep-compared). A field that is neither is decorative: absent, it
    is skipped; present, only its type is read. An absent field and any
    fabricated value score the same, so declaring it anchors nothing.

    The second element is whether the contract grades anything at all, which
    covers the empty contract where `fields` is empty and there is no `expected`.

    Names come straight from YAML, so a key written `7:` is an int, not a `str`.
    They are returned as the keys are, and the caller stringifies to name them.
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
    fields, _fields_unreadable = _as_mapping(metadata, "fields", report["errors"])
    expected, _expected_unreadable = _as_mapping(metadata, "expected", report["errors"])
    unanchored, has_anchor = _unanchored_fields(fields, expected)
    report["checks"]["contract_anchored"] = has_anchor and not unanchored
    if unanchored:
        # Names are stringified before the sort: a YAML `7:` key is an int, and
        # joining it raised TypeError out of here, losing the report this branch
        # exists to write.
        report["errors"].append(
            f"metadata.fields declares {', '.join(sorted(str(n) for n in unanchored))} with neither "
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
    if _expected_unreadable and has_anchor:
        # A malformed `expected` is an unreadable value grade, not an absent one. It
        # is dropped above, which silently downgrades the contract to a presence
        # check: `{"name": "Eve"}` scores 1.0 and passes a `required: true` field
        # even though the declared deep-comparison never ran. That is the fail-open
        # DUK-94 closed, so an unreadable `expected` costs the run its pass. A
        # malformed `fields` does not, because a well-formed `expected` is still a
        # value grade over the artifact.
        report["checks"]["contract_anchored"] = False
        report["errors"].append(
            "metadata.expected is not a mapping, so the values it declares were not compared "
            "and the remaining fields are graded on presence alone: an absent field and any "
            "fabricated value score the same. Give metadata.expected a mapping shape."
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

    # Read with the score rather than with the contract above: a parse failure
    # already fails closed before this point, so the only artifacts that can be
    # talked into passing by a bad floor are the ones that got this far.
    threshold, threshold_ok = _as_threshold(metadata, report["errors"])
    report["passes"] = (
        threshold_ok
        and report["checks"]["contract_anchored"]
        and report["checks"]["required_present"]
        and report["checks"]["types_ok"]
        and report["score"] >= threshold
    )
    if len(json.dumps(obj)) > _PREVIEW:
        report["artifact_preview"] = json.dumps(obj)[:_PREVIEW]
    return report
