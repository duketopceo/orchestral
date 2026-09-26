"""Tests for the ce-plan final-verification failure message: which fault, no model text.

When the final verification pass answers `passed: false`, `assemble_ce` raised
`f"final verification failed: {reason}"` with 200 chars of the *model's own*
`reasoning`/`reason` in the message. That message is not private: `runner.py`
copies `str(exc)` into the `run.failed` event twice — `output_data["error"]`
and the event's own `error` — and the TUI renders `error` in the run-inspection
log line and, at 500 chars, in the event detail panel.

The full verification response is already in `events.jsonl` by design, from the
`_llm_call` that produced it. So the requirement is the same one the judge parse
reason meets: the message must identify the response, not republish it.

The unit tests assert on the message, which is what `runner.py` writes verbatim.
The end-to-end test reads the real `run.failed` event rather than trusting that
equivalence.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from harness import _fail_line
from orchestral.config import ModelConfig, TaskSpec
from orchestral.logger import EventLogger
from orchestral.planners import assemble_ce
from orchestral.runner import Runner
from orchestral.storage import RunStore

CANARY = "SUPERSECRETVENDORKEY"

# long enough that the fingerprint's head and tail edge windows cover different
# places, so a truncated verification response is still diagnosable
PAD = "filler sentence. " * 20

ARTIFACT = "<html><head><title>T</title></head><body>ok</body></html>"


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
    )


def _chat_client(*contents: str) -> MagicMock:
    client = MagicMock()
    client.chat.side_effect = [
        {
            "content": content,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "latency_ms": 1,
            "id": "mock",
        }
        for content in contents
    ]
    return client


def _verification(reasoning: str | None = None, *, key: str = "reasoning") -> str:
    """A `passed: false` verification. `reasoning`/`reason` is the field the old
    message pasted, so the canary has to ride in whichever one the model used."""
    payload: dict[str, Any] = {"passed": False}
    if reasoning is not None:
        payload[key] = reasoning
    return json.dumps(payload)


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _logger_and_dir(tmp: str) -> tuple[EventLogger, Path]:
    store = RunStore(tmp)
    run_id, run_dir = store.new_run("o/m", "t", "w/m", {"planner": "ce-plan"})
    return EventLogger(run_dir, store=store, run_id=run_id), run_dir


def _assemble(logger: EventLogger, client: MagicMock) -> tuple[str, list[dict[str, Any]]]:
    return assemble_ce(
        logger=logger,
        task=TaskSpec(id="t", type="html", prompt="build a page"),
        orchestrator=_model("o/m", "orchestrator"),
        step=1,
        results=[{"id": 0, "content": "<section>hero</section>"}],
        client=client,
        dry_run=False,
    )


def _failing_assemble(tmp: str, verification: str) -> tuple[Exception, Path]:
    """Run `assemble_ce` expecting the verification failure; return it with the
    run dir so the log can be read."""
    logger, run_dir = _logger_and_dir(tmp)
    try:
        # call 1 assembles the artifact, call 2 is the final verification pass
        _assemble(logger, _chat_client(ARTIFACT, verification))
    except ValueError as exc:
        return exc, run_dir
    finally:
        logger.close()
    raise AssertionError("expected the verification failure to raise")


# (label, verification response) — the canary rides in each field the old
# message read, and at each position in the response.
#
# Every case keeps the canary inside the first 200 chars of the reason, because
# that is the window the old `[:200]` slice quoted. A canary planted past it
# would prove nothing: the old code would have cut it off. `reasoning-long`
# pads *after* the canary instead, so it stays inside the window while the
# response is long enough to exercise the fingerprint's head and tail.
CASES: list[tuple[str, str]] = [
    ("reasoning", _verification(f"{CANARY} is why it failed.")),
    ("reasoning-mid", _verification(f"Verdict: {CANARY}. Verdict.")),
    ("reasoning-tail", _verification(f"The verdict stands. {CANARY}")),
    ("reason", _verification(f"{CANARY}", key="reason")),
    ("reasoning-long", _verification(f"{CANARY} is why it failed. {PAD * 2}")),
    # no reason at all — the old message said "unspecified"
    ("no-reason", _verification()),
]


class TestNoModelTextInTheFailureMessage(unittest.TestCase):
    """The republished `str(exc)` must identify the response, not quote it."""

    def test_message_never_carries_the_models_own_words(self):
        for label, verification in CASES:
            if label == "no-reason":
                continue
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                exc, _ = _failing_assemble(tmp, verification)
                self.assertNotIn(CANARY, str(exc))

    def test_message_names_the_fault_and_the_response(self):
        for label, verification in CASES:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                exc, _ = _failing_assemble(tmp, verification)
                message = str(exc)
                self.assertIn("final verification failed", message)
                self.assertIn(f"{len(verification)} chars", message)
                self.assertRegex(message, r"sha256:[0-9a-f]{12}")

    def test_a_missing_reason_is_still_reported_as_missing(self):
        """Dropping the "unspecified" branch silently would make a model that
        gave no reason look like one that did."""
        with tempfile.TemporaryDirectory() as tmp:
            exc, _ = _failing_assemble(tmp, _verification())
        self.assertIn("no reason", str(exc).lower())

    def test_the_digest_separates_two_responses_of_equal_length(self):
        digests = set()
        for verdict in ('{"passed": false, "reasoning": "a"}', '{"passed": false, "reasoning": "b"}'):
            with tempfile.TemporaryDirectory() as tmp:
                exc, _ = _failing_assemble(tmp, verdict)
            digests.add(re.search(r"sha256:[0-9a-f]{12}", str(exc)).group(0))
        self.assertEqual(len(digests), 2)

    def test_the_log_still_holds_the_full_response(self):
        """The fingerprint replaces a quote, not the evidence: the response is
        logged by the `_llm_call` that produced it."""
        verification = _verification(f"{CANARY} is why it failed.")
        with tempfile.TemporaryDirectory() as tmp:
            _exc, run_dir = _failing_assemble(tmp, verification)
            logged = [
                ev for ev in _events(run_dir)
                if ev.get("type") == "llm_call" and ev.get("role") == "ce-work"
            ]
            self.assertEqual(len(logged), 1)
            self.assertEqual(logged[0]["output"]["content"], verification)
            self.assertIn(CANARY, json.dumps(logged[0]))

    def test_truncation_is_still_diagnosable(self):
        verification = _verification(PAD * 2)
        self.assertGreater(len(verification), 400, "needs to exceed the 400-char edge window")
        with tempfile.TemporaryDirectory() as tmp:
            exc, _ = _failing_assemble(tmp, verification)
        message = str(exc)
        head = re.search(r"head='([^']*)'", message)
        tail = re.search(r"tail='([^']*)'", message)
        self.assertIsNotNone(head, message)
        self.assertIsNotNone(tail, message)
        self.assertNotEqual(head.group(1), tail.group(1))


class TestTheRunFailedEventIsClean(unittest.TestCase):
    """`runner.py` copies `str(exc)` into the `run.failed` event twice, the TUI
    renders that field, and `harness._fail_line` prints it to stderr. Drive the
    real runner and read the real event and the real stderr line."""

    def _failed_run(self, tmp: str, verification: str) -> tuple[list[dict[str, Any]], Any, str]:
        # ce-plan order: plan, confidence, doc-review, assemble, final verify
        orch = _chat_client(
            json.dumps({"subtasks": [{"id": 0, "description": "hero section"}]}),
            json.dumps({"confidence": 0.9}),
            json.dumps({"findings": []}),
            ARTIFACT,
            verification,
        )
        store = RunStore(tmp)
        runner = Runner(
            runs_dir=tmp, store=store, planner="ce-plan",
            clients={"orchestrator": orch, "worker": _chat_client("<section>hero</section>")},
        )
        # `runner.run` logs the `run.failed` event and then re-raises, so the
        # caller sees the same message the event carries
        with self.assertRaises(ValueError) as raised:
            runner.run(
                TaskSpec(id="t", type="html", prompt="build a page"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
        # `_fail_line` is what every harness command prints for a failed run, so
        # build the line the CLI would print for this exception
        fail_line = _fail_line("run", 1, 1, "1/1", raised.exception)
        runs = store.list_runs()
        self.assertEqual(len(runs), 1)
        meta = runs[0]
        self.assertEqual(meta.status, "failed")
        # the run index and metrics read the category, never the message
        self.assertTrue(meta.failure_reason.startswith("exception:"))
        self.assertNotIn(CANARY, meta.failure_reason)
        return _events(Path(meta.run_dir)), meta, fail_line

    def test_run_failed_event_carries_no_model_text(self):
        verification = _verification(f"{CANARY} is why it failed. {PAD * 2}")
        with tempfile.TemporaryDirectory() as tmp:
            events, _meta, _line = self._failed_run(tmp, verification)
        failures = [ev for ev in events if ev.get("type") == "run.failed"]
        self.assertEqual(len(failures), 1, "expected exactly one run.failed event")
        event = failures[0]
        # the two fields the TUI reads, plus the whole event as a blunt check
        self.assertNotIn(CANARY, str(event.get("error") or ""))
        self.assertNotIn(CANARY, str((event.get("output") or {}).get("error") or ""))
        self.assertNotIn(CANARY, json.dumps(event))
        # the fault is still named for whoever reads the event
        self.assertIn("final verification failed", str(event.get("error"))
                      )

    def test_the_re_raised_exception_carries_no_model_text(self):
        """`runner.run` re-raises, so the message also reaches whatever called
        it — including `harness._fail_line` and therefore the CI log."""
        with tempfile.TemporaryDirectory() as tmp:
            *_, fail_line = self._failed_run(
                tmp, _verification(f"{CANARY} is why it failed. {PAD * 2}")
            )
        self.assertIn("[fail] run replicate 1/1", fail_line)
        self.assertIn("ValueError: final verification failed", fail_line)
        self.assertNotIn(CANARY, fail_line)

    def test_the_canary_really_reached_the_code_under_test(self):
        """Positive control for the two negatives above: the same run does log
        the full response, so the canary was delivered and would have been
        quoted had anything quoted it. Without this, a canary that never
        reached `assemble_ce` would make both tests pass for free."""
        with tempfile.TemporaryDirectory() as tmp:
            events, _meta, _line = self._failed_run(
                tmp, _verification(f"{CANARY} is why it failed.")
            )
        logged = [
            ev for ev in events
            if ev.get("type") == "llm_call" and ev.get("role") == "ce-work"
        ]
        self.assertEqual(len(logged), 1)
        self.assertIn(CANARY, logged[0]["output"]["content"])


class TestAssemblyItselfIsUnchanged(unittest.TestCase):
    """The fix is a message change. Passing verification, and the artifact,
    must behave exactly as before."""

    def _assemble(self, tmp: str, verification: str) -> tuple[str, list[dict[str, Any]]]:
        logger, _ = _logger_and_dir(tmp)
        try:
            return _assemble(logger, _chat_client(ARTIFACT, verification))
        finally:
            logger.close()

    def test_a_passing_verification_still_returns_the_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, costs = self._assemble(tmp, json.dumps({"passed": True}))
        self.assertEqual(artifact, ARTIFACT)
        self.assertEqual(len(costs), 2)

    def test_the_string_false_is_still_not_a_failure(self):
        """`passed` is compared with `is False`, so the string "false" does not
        fail the run. The fix must not change that comparison."""
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _ = self._assemble(tmp, json.dumps({"passed": "false"}))
        self.assertEqual(artifact, ARTIFACT)

    def test_an_unparseable_verification_still_degrades_quietly(self):
        """An unparseable verification is suppressed, not raised — the DUK-159
        contract, and this change must not disturb it."""
        with tempfile.TemporaryDirectory() as tmp:
            artifact, costs = self._assemble(tmp, "not json at all")
        self.assertEqual(artifact, ARTIFACT)
        self.assertEqual(len(costs), 2)

    def test_a_passing_verification_with_a_reason_still_keeps_its_text(self):
        """A pass never raised, so nothing about the model's own text on the
        success path changes."""
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _ = self._assemble(
                tmp, json.dumps({"passed": True, "reasoning": f"{CANARY} is fine"})
            )
        self.assertEqual(artifact, ARTIFACT)


if __name__ == "__main__":
    unittest.main()
