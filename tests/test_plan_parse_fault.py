"""Tests for `PlanParseFault` and the `orchestrator.plan_parse_fault` event.

[DUK-183](/DUK/issues/DUK-183) decided the plan phase gets no retry, on the
condition that the fault rate is measured instead. That decision is only
honest if the measurement is trustworthy, so these tests hold three lines:

- the three faults stay discriminable, because `kind` is the only thing a
  future retry decision is allowed to branch on (never the message prose);
- the event never carries model text, because the event stream reaches
  `events.jsonl`, the call store, and from there the HTML report — the same
  class of exposure DUK-159 closed on the exception string, reopened on a
  second surface here;
- measuring a fault does not change the response to it. The plan phase has
  no retry, so swallowing or rewriting the exception would turn an
  instrumented fault into a silent one.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.logger import LIFECYCLE_EVENTS, EventLogger
from orchestral.planners import (
    PlanParseFault,
    _extract_json,
    _parse_plan,
    _response_digest,
    _response_fingerprint,
)

CANARY = "CANARY-9d31-do-not-log"

# One failing payload per fault kind. `text` is what `_parse_plan` receives.
FAULT_CASES = {
    "no_json": "here is your plan, sorry: " + CANARY,
    "unbalanced": '{"plan": ["step one", "' + CANARY,
    "balanced_invalid": '{"plan": [1, 2,,]} trailing ' + CANARY,
}


def _event(tmp: str, content: str) -> dict:
    """Run one failing parse through `_parse_plan`, return the emitted event."""
    logger = EventLogger(tmp)
    try:
        _parse_plan(logger=logger, content=content, step=7)
    except PlanParseFault:
        pass
    else:
        raise AssertionError("expected a PlanParseFault")
    finally:
        logger.close()
    lines = (Path(tmp) / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1, f"expected exactly one event, got {len(lines)}"
    return json.loads(lines[0])


class TestFaultIsDiscriminable(unittest.TestCase):
    """`kind` — not the prose — is what a caller branches on."""

    def test_each_fault_names_its_own_kind(self):
        for expected_kind, content in FAULT_CASES.items():
            with self.subTest(kind=expected_kind):
                with self.assertRaises(PlanParseFault) as ctx:
                    _extract_json(content)
                self.assertEqual(ctx.exception.kind, expected_kind)

    def test_all_three_kinds_are_distinct(self):
        kinds = set()
        for content in FAULT_CASES.values():
            with self.assertRaises(PlanParseFault) as ctx:
                _extract_json(content)
            kinds.add(ctx.exception.kind)
        self.assertEqual(kinds, set(FAULT_CASES))

    def test_still_a_valueerror(self):
        """`delegate`, `delegate_multi` and `judge` catch `ValueError`.

        DUK-159 wrapped `json.loads`; this wraps the whole parse. If the new
        type ever stopped being a `ValueError`, every one of those callers
        would turn a graceful degradation into a hard failure.
        """
        for kind, content in FAULT_CASES.items():
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                _extract_json(content)

    def test_message_still_names_its_own_fault(self):
        """The prose stays human-readable; `kind` is for machines."""
        expected = {
            "no_json": "No JSON found",
            "unbalanced": "Unbalanced JSON",
            "balanced_invalid": "did not parse",
        }
        for kind, content in FAULT_CASES.items():
            with self.subTest(kind=kind):
                with self.assertRaises(PlanParseFault) as ctx:
                    _extract_json(content)
                self.assertIn(expected[kind], str(ctx.exception))


class TestResponseDigest(unittest.TestCase):
    """One fingerprint, read the same way by the message and the event."""

    def test_digest_agrees_with_the_fingerprint(self):
        text = "x" * 900
        chars, digest = _response_digest(text)
        fingerprint = _response_fingerprint(text)
        self.assertIn(f"{chars} chars sha256:{digest}", fingerprint)

    def test_digest_carries_no_text(self):
        _, digest = _response_digest(CANARY)
        self.assertNotIn(CANARY, digest)
        self.assertEqual(len(digest), 12)


class TestFaultEvent(unittest.TestCase):
    """The measurement DUK-183 was bought with."""

    def test_event_type_is_registered(self):
        """`EventLogger.lifecycle` raises unless the type is in the vocabulary.

        A missing registration does not fail loudly here — it raises a plain
        `ValueError` from inside the error path, replacing the `PlanParseFault`
        and discarding the fault class. The event would then never be written
        and no test of the *message* would notice.
        """
        self.assertIn("orchestrator.plan_parse_fault", LIFECYCLE_EVENTS)

    def test_event_carries_kind_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = FAULT_CASES["unbalanced"]
            event = _event(tmp, content)
            self.assertEqual(event["type"], "orchestrator.plan_parse_fault")
            self.assertEqual(event["phase"], "plan")
            self.assertEqual(event["role"], "orchestrator")
            self.assertEqual(event["step"], 7)
            chars, digest = _response_digest(content)
            self.assertEqual(event["output"]["fault"], "unbalanced")
            self.assertEqual(event["output"]["chars"], chars)
            self.assertEqual(event["output"]["sha256"], digest)

    def test_event_distinguishes_all_three_faults(self):
        for kind, content in FAULT_CASES.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                event = _event(tmp, content)
                self.assertEqual(event["output"]["fault"], kind)

    def test_event_carries_no_model_text(self):
        """The new egress surface, checked for the leak QA closed on #69.

        QA's canary test covered the exception string. This event is a second
        path to `events.jsonl` and the call store, and `reporter.py` renders
        store content into the HTML report the `eval` workflow posts as a PR
        comment. A canary planted head, middle and tail must not survive.
        """
        for kind in FAULT_CASES:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                planted = f"{CANARY}-head {{'plan': '{CANARY}-mid"
                if kind == "balanced_invalid":
                    planted = f'{{"plan": [1,,], "{CANARY}-mid": "{CANARY}-tail"}}'
                raw = (Path(tmp) / "events.jsonl")
                event = _event(tmp, planted)
                self.assertNotIn(CANARY, json.dumps(event))
                self.assertFalse(raw.exists() and CANARY in raw.read_text())

    def test_measurement_does_not_change_the_response(self):
        """The plan phase has no retry, so the exception must survive intact.

        If instrumentation raised instead of re-raising, the fault class would
        be lost and the run would report a different fault than the one that
        happened.
        """
        for kind, content in FAULT_CASES.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                logger = EventLogger(tmp)
                try:
                    with self.assertRaises(PlanParseFault) as ctx:
                        _parse_plan(logger=logger, content=content, step=1)
                    self.assertEqual(ctx.exception.kind, kind)
                finally:
                    logger.close()

    def test_successful_parse_emits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(tmp)
            try:
                plan = _parse_plan(logger=logger, content='{"plan": ["a"]}', step=1)
            finally:
                logger.close()
            self.assertEqual(plan, {"plan": ["a"]})
            self.assertEqual((Path(tmp) / "events.jsonl").read_text().strip(), "")


if __name__ == "__main__":
    unittest.main()
