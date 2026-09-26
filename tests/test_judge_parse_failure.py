"""Tests for the judge parse-failure reason: which fault, and no model text.

`reasoning` is a designed, quoted field, so it is not the place to paste a model
response. `reporter.py` renders it into the HTML report's reasoning column, the
TUI shows it, and `export --format md` writes it into the audit that `scrub`
publishes. The old reason was `f"Could not parse judge response: {content[:200]}"`,
which put 200 chars of judge output on all three.

The full completion is still logged, by design, in the event's `output` — that is
the log's job, and it stays inside the run directory. What must not happen is a
second copy of the same words in a field built for human readers. So the canary
assertions below run against each of the three surfaces, not just the returned
dict: the return value, `events.jsonl`'s `reasoning` field, and the rendered
report.
"""

from __future__ import annotations

import html
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.export import run_audit_markdown
from orchestral.judge import judge_artifact
from orchestral.logger import EventLogger
from orchestral.reporter import generate_html_report
from orchestral.storage import RunStore

CANARY = "SUPERSECRETVENDORKEY"

# long enough that the head and tail edge windows cover different places, so
# both ends of the response are exercised
PAD = "filler sentence. " * 20

# (expected fault name, response) for every path that reaches the except block,
# with the canary at the head, the middle, and the tail of the response
PATHS: list[tuple[str, list[str]]] = [
    # `_extract_json`: no delimiter anywhere
    ("No JSON found", [
        f"{CANARY} is the verdict, in prose.",
        f"My verdict. {CANARY}. My verdict.",
        f"My verdict, in prose: {CANARY}",
    ]),
    # `_extract_json`: a delimiter found, never closed
    ("Unbalanced JSON", [
        f'{{"score": {CANARY}, "passed": true, "reasoning": "{PAD}',
        f'{{"score": 0.9, "passed": {CANARY}, "reasoning": "{PAD}',
        f'{{"score": 0.9, "passed": true, "reasoning": "{PAD}{CANARY}',
    ]),
    # `_extract_json`: delimiters balanced, `json.loads` failed
    ("Balanced JSON", [
        f'{{"note": "{CANARY}", "score": }}',
        f'{{"note": "{CANARY}" "score": 1}}',
        f'{{"score": 0.9, "note": "{CANARY}",}}',
    ]),
    # judge contract: valid JSON, but not an object
    ("Judge did not return a JSON object", [
        f'["{CANARY}"]',
        f'[1, "{CANARY}", 2]',
        f'["{CANARY}", "{CANARY}"]',
    ]),
    # judge contract: an object without the two required keys
    ("Judge JSON missing score or passed", [
        f'{{"note": "{CANARY}"}}',
        f'{{"a": "{CANARY}", "b": 2}}',
        f'{{"reasoning": "{CANARY}"}}',
    ]),
    # judge contract: `score` present but not a number. The canary has to sit
    # inside a JSON value, not be a bare word, or the response never parses.
    ("Judge score was not a number", [
        f'{{"score": "{CANARY}", "passed": true}}',
        f'{{"score": {{"v": "{CANARY}"}}, "passed": true}}',
        f'{{"score": ["{CANARY}"], "passed": true}}',
    ]),
]

# the `raw` column is a sanctioned copy of the event, the same one the log
# already holds; the rest of the report is the part built for human readers
_RAW_COLUMN = re.compile(r"<details>.*?</details>", re.DOTALL)


def _all_responses() -> list[tuple[str, str]]:
    return [(fault, response) for fault, responses in PATHS for response in responses]


class _JudgeRun:
    """One `judge_artifact` call against a real run dir, so the log and the
    rendered report can be inspected rather than mocked."""

    def __init__(self, tmp: str, response: str) -> None:
        self.store = RunStore(tmp)
        self.run_id, self.run_dir = self.store.new_run("o/m", "t", "w/m", {"judge": "j/m"})
        self.logger = EventLogger(self.run_dir, store=self.store, run_id=self.run_id)
        self.response = response
        client = MagicMock()
        client.chat.return_value = {
            "content": response,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1,
            "id": "x",
        }
        self.result, self.costs = judge_artifact(
            logger=self.logger,
            step=1,
            task=TaskSpec(id="t", type="html", prompt="build a page"),
            artifact="<html><body>hi</body></html>",
            judge=ModelConfig(
                slug="j/m", name="j/m", role="judge",
                input_price_per_mtok=0.1, output_price_per_mtok=0.4,
            ),
            client=client,
            dry_run=False,
        )
        self.logger.close()

    @property
    def events(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (self.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def judge_event(self) -> dict[str, Any]:
        calls = [ev for ev in self.events if ev.get("type") == "llm_call" and ev.get("role") == "judge"]
        assert len(calls) == 1, f"expected one judge call, got {len(calls)}"
        return calls[0]

    def report_json(self) -> Path:
        path = self.run_dir / "report.json"
        path.write_text(json.dumps({"task_id": "t", "judge": self.result}), encoding="utf-8")
        return path


class TestNoModelTextReachesAReader(unittest.TestCase):
    """No parse-failure path may put the judge's own words in a quoted field."""

    def test_returned_reason(self):
        for fault, response in _all_responses():
            with self.subTest(fault=fault, response=response[:24]), tempfile.TemporaryDirectory() as tmp:
                run = _JudgeRun(tmp, response)
                self.assertNotIn(CANARY, run.result["reasoning"])
                self.assertEqual(run.result["reasoning"], run.judge_event()["reasoning"])

    def test_events_jsonl_reasoning_field(self):
        """`events.jsonl` records the completion on purpose; the `reasoning`
        field next to it must not become a second copy."""
        for fault, response in _all_responses():
            with self.subTest(fault=fault, response=response[:24]), tempfile.TemporaryDirectory() as tmp:
                run = _JudgeRun(tmp, response)
                event = run.judge_event()
                self.assertNotIn(CANARY, event["reasoning"])
                # the log keeps its own job: the full completion is still there
                self.assertIn(CANARY, event["output"]["content"])

    def test_html_report_reasoning_column(self):
        for fault, response in _all_responses():
            with self.subTest(fault=fault, response=response[:24]), tempfile.TemporaryDirectory() as tmp:
                run = _JudgeRun(tmp, response)
                run.report_json()
                reports = Path(tmp) / "reports"
                generate_html_report(tmp, reports)
                page = (reports / f"{run.run_id}.html").read_text(encoding="utf-8")
                self.assertIn(html.escape(run.result["reasoning"][:120]), page)
                self.assertNotIn(CANARY, _RAW_COLUMN.sub("", page))

    def test_exported_run_audit(self):
        """`export --format md` writes `- judge: {reasoning}` into the audit that
        `scrub` copies into the publishable tree."""
        for fault, response in _all_responses():
            with self.subTest(fault=fault, response=response[:24]), tempfile.TemporaryDirectory() as tmp:
                run = _JudgeRun(tmp, response)
                run.report_json()
                audit = run_audit_markdown(run.run_dir)
                self.assertIn("- judge: ", audit)
                self.assertNotIn(CANARY, audit)


class TestTheReasonStillSaysWhatFailed(unittest.TestCase):
    """Removing the text must not remove the diagnosis."""

    def test_each_path_names_its_own_fault(self):
        for fault, response in _all_responses():
            with self.subTest(fault=fault, response=response[:24]), tempfile.TemporaryDirectory() as tmp:
                reason = _JudgeRun(tmp, response).result["reasoning"]
                self.assertIn(fault, reason)
                self.assertIn("Could not parse judge response", reason)

    def test_the_three_parse_faults_stay_distinct(self):
        reasons = set()
        for _fault, responses in PATHS:
            with tempfile.TemporaryDirectory() as tmp:
                reasons.add(_JudgeRun(tmp, responses[0]).result["reasoning"])
        self.assertEqual(len(reasons), len(PATHS))

    def test_every_reason_carries_length_and_digest(self):
        for fault, response in _all_responses():
            with self.subTest(fault=fault, response=response[:24]), tempfile.TemporaryDirectory() as tmp:
                reason = _JudgeRun(tmp, response).result["reasoning"]
                # `_extract_json` fingerprints the stripped text and the
                # judge fingerprints the raw response, so a trailing space
                # is the only thing that can move the count by one
                self.assertIn(f"{len(response.strip())} chars", reason)
                self.assertRegex(reason, r"sha256:[0-9a-f]{12}")

    def test_the_digest_distinguishes_two_responses_of_equal_length(self):
        digests = set()
        for response in ('{"score": "a", "passed": true}', '{"score": "b", "passed": true}'):
            with tempfile.TemporaryDirectory() as tmp:
                reason = _JudgeRun(tmp, response).result["reasoning"]
                digests.add(re.search(r"sha256:[0-9a-f]{12}", reason).group(0))
        self.assertEqual(len(digests), 2)

    def test_truncation_is_still_diagnosable(self):
        """A long response that stopped mid-object must still show both ends, or
        the fingerprint has removed the only evidence that it was cut off."""
        response = '{"score": 0.9, "passed": true, "reasoning": "' + PAD * 2
        self.assertGreater(len(response), 400, "needs to exceed the 400-char edge window")
        with tempfile.TemporaryDirectory() as tmp:
            reason = _JudgeRun(tmp, response).result["reasoning"]
        head = re.search(r"head='([^']*)'", reason)
        tail = re.search(r"tail='([^']*)'", reason)
        self.assertIsNotNone(head, reason)
        self.assertIsNotNone(tail, reason)
        self.assertNotEqual(head.group(1), tail.group(1))


class TestJudgingItselfIsUnchanged(unittest.TestCase):
    """The fix is a message change. Scoring, degradation, and the judge's own
    reasoning all have to behave exactly as before."""

    def _result(self, response: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            return _JudgeRun(tmp, response).result

    def test_a_valid_response_still_scores(self):
        result = self._result('{"score": 0.75, "passed": true, "reasoning": "solid work"}')
        self.assertEqual(result["score"], 0.75)
        self.assertTrue(result["passed"])
        self.assertNotIn("parse_failed", result)
        # the judge's own reasoning is a designed, human-facing field and stays
        self.assertEqual(result["reasoning"], "solid work")

    def test_the_string_false_is_still_a_fail(self):
        self.assertFalse(self._result('{"score": 0.9, "passed": "false"}')["passed"])

    def test_a_parse_failure_still_degrades_gracefully(self):
        for fault, response in _all_responses():
            with self.subTest(fault=fault):
                result = self._result(response)
                self.assertEqual(result["score"], 0.0)
                self.assertFalse(result["passed"])
                self.assertTrue(result["parse_failed"])

    def test_a_parse_failure_still_reports_its_cost(self):
        """The call happened, so it is billed and logged; dropping the result
        must not drop the cost record."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _JudgeRun(tmp, "not json at all")
        self.assertEqual(len(run.costs), 1)
        self.assertEqual(run.costs[0]["phase"], "judge")
        self.assertGreater(run.costs[0]["output_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
