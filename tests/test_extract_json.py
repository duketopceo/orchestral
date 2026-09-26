"""Tests for `_extract_json`: three named faults, and no model text in a message.

A failed plan parse has to say which of three things went wrong — the model
sent no JSON, the response was cut off, or the model broke the JSON contract —
because those need different responses from whoever reads the log. And no
message may republish the model's words: an exception string reaches CI logs
and issue comments, and run output is supposed to stay in the run directory.
"""

from __future__ import annotations

import re
import unittest

from orchestral.planners import _extract_json

# the first 200 chars of the failed `eval` response on PR #66, as the old error
# message printed it. The old message cut the response at 200 chars, so the
# real payload was at least this long and ended mid-word.
CI_LOG_PREFIX = (
    '{\n  "plan": "Create a responsive landing page for a premium coffee '
    'subscription service using a single-page HTML file with embedded CSS and '
    'JavaScript (or separate assets). The page will include a he'
)

CANARY = "CANARY-4f2a-do-not-log"


def _message(payload: str) -> str:
    try:
        _extract_json(payload)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"expected a parse failure for {payload[:40]!r}")


class TestExtractJsonStillParses(unittest.TestCase):
    """The fault reporting must not change what counts as parseable."""

    def test_object(self):
        self.assertEqual(_extract_json('{"plan": "ship it"}'), {"plan": "ship it"})

    def test_array(self):
        self.assertEqual(_extract_json("[1, 2, 3]"), [1, 2, 3])

    def test_fenced(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_surrounding_prose(self):
        self.assertEqual(_extract_json('Sure!\n{"a": 1}\nHope that helps.'), {"a": 1})

    def test_brace_inside_a_string_does_not_close_the_block(self):
        self.assertEqual(_extract_json('{"css": "body { margin: 0 }"}'), {"css": "body { margin: 0 }"})

    def test_escaped_quote_inside_a_string(self):
        self.assertEqual(_extract_json(r'{"q": "say \"hi\""}'), {"q": 'say "hi"'})


class TestThreeNamedFaults(unittest.TestCase):
    def test_no_json_anywhere(self):
        msg = _message("Here is the plan you asked for, in prose.")
        self.assertIn("No JSON found", msg)
        self.assertIn(f"{len('Here is the plan you asked for, in prose.')} chars", msg)

    def test_unbalanced_delimiters_name_truncation(self):
        msg = _message('{"plan": "cut off mid')
        self.assertIn("Unbalanced JSON", msg)
        self.assertIn("truncated", msg)
        self.assertIn("brace inside a string escaped the matcher", msg)

    def test_balanced_but_invalid_names_position_and_reason(self):
        msg = _message('{"plan": }')
        self.assertIn("Balanced JSON", msg)
        self.assertIn("Expecting value", msg)
        self.assertIn("position 9", msg)

    def test_the_three_messages_are_distinct(self):
        msgs = {
            _message("prose only, no delimiters"),
            _message('{"plan": "cut off mid'),
            _message('{"plan": }'),
        }
        self.assertEqual(len(msgs), 3)

    def test_every_fault_stays_a_value_error(self):
        """`delegate`, `delegate_multi` and `judge` catch ValueError to degrade
        gracefully; wrapping the decode error must not escape that contract."""
        for payload in ("prose only", '{"plan": "cut off mid', '{"plan": }'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _extract_json(payload)


class TestTruncationIsDiagnosable(unittest.TestCase):
    def test_length_and_both_ends_are_reported(self):
        # long enough that the head and tail edges are different places
        payload = '{"plan": "' + "filler sentence. " * 40 + "unclosed"
        msg = _message(payload)
        self.assertIn(f"{len(payload)} chars", msg)
        head = re.search(r"head='([^']*)'", msg)
        tail = re.search(r"tail='([^']*)'", msg)
        self.assertIsNotNone(head, msg)
        self.assertIsNotNone(tail, msg)
        self.assertNotEqual(head.group(1), tail.group(1))
        # opens an object, opens the plan value, never closes either
        self.assertTrue(head.group(1).startswith("{"), head.group(1))
        self.assertFalse(tail.group(1).endswith("}"), tail.group(1))

    def test_offset_of_the_candidate_is_reported(self):
        payload = 'Here is the plan.\n\n{"plan": "cut off mid'
        msg = _message(payload)
        self.assertIn(f"offset {payload.index('{')}", msg)

    def test_digest_distinguishes_two_truncated_responses(self):
        a = _message('{"plan": "cut off mid')
        b = _message('{"plan": "cut off differently')
        self.assertNotEqual(a.split("sha256:")[1][:12], b.split("sha256:")[1][:12])


class TestNoModelTextInFailureMessages(unittest.TestCase):
    """No fault message may carry model words — not at the head, middle, or tail."""

    def _payloads(self) -> list[str]:
        pad = "b" * 250
        return [
            # no-JSON branch: canary is the whole response
            f"prose {CANARY} more prose",
            # unbalanced branch: canary at the head, the middle, and the tail
            '{"' + CANARY + '": "' + pad,
            '{"plan": "' + CANARY + pad + CANARY,
            '{"plan": "' + pad + CANARY,
            # balanced-but-invalid branch: canary inside the block
            '{"plan": "' + CANARY + '", }',
        ]

    def test_canary_never_appears(self):
        for payload in self._payloads():
            with self.subTest(payload=payload[:24]):
                self.assertNotIn(CANARY, _message(payload))

    def test_model_words_never_appear(self):
        # distinctive words from the real response, in each branch
        for payload in (
            f"Create a responsive landing page {CANARY}",
            '{"plan": "Create a responsive landing page for a he',
            '{"plan": "Create a responsive landing page", }',
        ):
            with self.subTest(payload=payload[:24]):
                msg = _message(payload)
                self.assertNotIn("responsive", msg)
                self.assertNotIn("landing", msg)
                self.assertNotIn("Create", msg)

    def test_response_body_never_appears(self):
        payload = '{"plan": "' + "SECRETBODY" * 20
        msg = _message(payload)
        self.assertNotIn("SECRETBODY", msg)
        self.assertIn(f"{len(payload)} chars", msg)


class TestRealCiRepro(unittest.TestCase):
    """The payload from PR #66's failed `eval` check must get the true message.

    The old branch reported "Could not extract JSON" for a response that *did*
    contain JSON, which is what sent the reading off looking for a harness
    serialization bug instead of a truncated provider response.
    """

    def test_logged_prefix_reports_truncation(self):
        msg = _message(CI_LOG_PREFIX)
        self.assertIn("Unbalanced JSON", msg)
        self.assertNotIn("Could not extract JSON", msg)
        self.assertNotIn("No JSON found", msg)
        self.assertIn(f"{len(CI_LOG_PREFIX)} chars", msg)

    def test_response_longer_than_the_logged_prefix(self):
        """The old message truncated at 200 chars; the fingerprint is not."""
        payload = CI_LOG_PREFIX + "ader with a hero section, a signup form, and a footer"
        msg = _message(payload)
        self.assertIn(f"{len(payload)} chars", msg)
        self.assertNotIn("200 chars", msg)
        self.assertGreater(len(payload), 200)


if __name__ == "__main__":
    unittest.main()
