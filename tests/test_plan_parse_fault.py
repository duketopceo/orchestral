"""A plan parse fault emits one countable event, and no model text.

DUK-183 declined to add a plan retry because the rate of transport faults in
the plan phase is unknown — one anecdote does not justify permanent retry
machinery on the most expensive call in a run. That decision can only be
revisited on a measured rate, so the rate has to be countable: one
`orchestrator.plan_parse_fault` per fault, carrying the fault class and the
response digest.

Two properties matter as much as the count. The event must not republish the
model's words, because an event stream is scrubbed, exported and commented on;
and the exception must still propagate, because measuring a fault must not
change the response to it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.logger import LIFECYCLE_EVENTS, EventLogger
from orchestral.planners import (
    PlanParseFault,
    _response_digest,
    plan_ce,
    plan_raw,
)
from orchestral.runner import Runner
from orchestral.storage import RunStore

FAULT_EVENT = "orchestrator.plan_parse_fault"

# A distinctive string planted in the model response. The fault event must not
# carry it; the `llm_call` event does, which is the pre-existing answer-key
# scrubbing problem, not this event's.
CANARY = "CANARY-7c1e-do-not-log"

# The three faults, keyed by the `kind` each one must report.
RESPONSES = {
    "no_json": f"Sure, here is the plan. {CANARY}",
    "unbalanced": '{"plan": "' + CANARY + ' is cut off mid',
    "balanced_invalid": '{"plan": "' + CANARY + '", }',
}


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
    )


def _chat_client(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "latency_ms": 1,
        "id": "mock",
    }
    return client


def _task() -> TaskSpec:
    return TaskSpec(id="t1", type="html", prompt="make a page")


def _events(run_dir: Path | str) -> list[dict]:
    return [
        json.loads(line)
        for line in (Path(run_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _faults(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == FAULT_EVENT]


class TestEventIsEmitted(unittest.TestCase):
    """One fault, one event, on both plan paths."""

    def _plan(self, tmp: str, content: str, planner) -> list[dict]:
        logger = EventLogger(tmp)
        try:
            with self.assertRaises(PlanParseFault):
                planner(
                    logger=logger,
                    task=_task(),
                    orchestrator=_model("o/m", "orchestrator"),
                    step=1,
                    client=_chat_client(content),
                    dry_run=False,
                )
        finally:
            logger.close()
        return _events(tmp)

    def _assert_one_fault_event(self, tmp: str, kind: str, content: str, planner) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            events = self._plan(run_dir, content, planner)
        faults = _faults(events)
        self.assertEqual(len(faults), 1, [e.get("type") for e in events])
        event = faults[0]
        out = event["output"]
        self.assertEqual(out["fault"], kind)
        self.assertEqual(event["phase"], "plan")
        self.assertEqual(event["role"], "orchestrator")
        self.assertEqual(event["step"], 1)
        # the digest identifies the response as logged, so a reader can match
        # the fault to the llm_call event that produced it
        chars, digest = _response_digest(content)
        self.assertEqual(out["chars"], chars)
        self.assertEqual(out["chars"], len(content))
        self.assertEqual(out["sha256"], digest)
        self.assertRegex(out["sha256"], r"^[0-9a-f]{12}$")

    def test_plan_raw_reports_every_fault_class(self):
        for kind, content in RESPONSES.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                self._assert_one_fault_event(tmp, kind, content, plan_raw)

    def test_plan_ce_reports_every_fault_class(self):
        for kind, content in RESPONSES.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                self._assert_one_fault_event(tmp, kind, content, plan_ce)

    def test_the_digest_identifies_the_response_not_the_stripped_candidate(self):
        """A fenced response is stripped before parsing, so a digest of the
        candidate would not match the `llm_call` event's `content`."""
        content = "```json\n" + RESPONSES["unbalanced"] + "\n```"
        with tempfile.TemporaryDirectory() as tmp:
            events = self._plan(tmp, content, plan_raw)
        out = _faults(events)[0]["output"]
        self.assertEqual(out["chars"], len(content))
        self.assertEqual(out["sha256"], _response_digest(content)[1])

    def test_no_fault_event_when_the_plan_parses(self):
        content = json.dumps({"subtasks": [{"id": 0, "description": "do it"}]})
        logger = EventLogger(tempfile.mkdtemp())
        try:
            plan, _ = plan_raw(
                logger=logger, task=_task(), orchestrator=_model("o/m", "orchestrator"),
                step=1, client=_chat_client(content), dry_run=False,
            )
        finally:
            logger.close()
        self.assertEqual(plan["planner"], "raw")
        self.assertEqual(_faults(_events(logger.run_dir)), [])

    def test_a_plan_that_parses_but_breaks_the_contract_is_not_a_parse_fault(self):
        """`[1, 2]` is valid JSON and an invalid plan. The taxonomy has exactly
        three parse faults, and a contract break is not one of them: counting it
        here would inflate the transport-fault rate this event exists to
        measure."""
        logger = EventLogger(tempfile.mkdtemp())
        try:
            with self.assertRaises(ValueError) as ctx:
                plan_raw(
                    logger=logger, task=_task(), orchestrator=_model("o/m", "orchestrator"),
                    step=1, client=_chat_client("[1, 2]"), dry_run=False,
                )
        finally:
            logger.close()
        self.assertNotIsInstance(ctx.exception, PlanParseFault)
        self.assertEqual(_faults(_events(logger.run_dir)), [])


class TestNoModelTextInTheEvent(unittest.TestCase):
    def test_the_canary_never_reaches_the_fault_event(self):
        for kind, content in RESPONSES.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                logger = EventLogger(tmp)
                try:
                    with self.assertRaises(PlanParseFault):
                        plan_raw(
                            logger=logger, task=_task(),
                            orchestrator=_model("o/m", "orchestrator"),
                            step=1, client=_chat_client(content), dry_run=False,
                        )
                finally:
                    logger.close()
                faults = _faults(_events(tmp))
                self.assertEqual(len(faults), 1)
                self.assertNotIn(CANARY, json.dumps(faults[0]))
                # the only strings the event carries are the class, the length,
                # the digest, and the event's own schema fields
                self.assertEqual(
                    sorted(faults[0]["output"]), ["chars", "fault", "sha256"]
                )


class TestEventLandsInARun(unittest.TestCase):
    """The event is only useful if it survives into the run directory, which is
    what the eval workflow reads to write its PR comment."""

    def test_a_truncated_orchestrator_response_fails_the_run_with_one_fault_event(self):
        content = '{"plan": "' + CANARY + " is cut off mid"
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store,
                clients={"orchestrator": _chat_client(content), "worker": _chat_client("")},
            )
            with self.assertRaises(PlanParseFault):
                runner.run(
                    _task(), _model("o/m", "orchestrator"), _model("w/m", "worker"),
                )
            run_dir = Path(store.list_runs()[0].run_dir)
            events = _events(run_dir)
            faults = _faults(events)
            self.assertEqual(len(faults), 1)
            self.assertEqual(faults[0]["output"]["fault"], "unbalanced")
            self.assertEqual(faults[0]["output"]["sha256"], _response_digest(content)[1])
            # the fault is recorded before the run is failed, and the run still
            # fails: measuring the fault did not soften it
            types = [e["type"] for e in events]
            self.assertLess(types.index(FAULT_EVENT), types.index("run.failed"))
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")


class TestVocabulary(unittest.TestCase):
    def test_the_event_is_in_the_lifecycle_vocabulary(self):
        """`lifecycle()` rejects any type outside the vocabulary, so an
        unregistered event would raise instead of recording the fault."""
        self.assertIn(FAULT_EVENT, LIFECYCLE_EVENTS)


if __name__ == "__main__":
    unittest.main()
