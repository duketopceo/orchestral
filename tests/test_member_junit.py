"""Per-member JUnit parsing for wide-decomposition fixture specs."""

import unittest

from orchestral.cubeexec import _parse_member_junit

XML = """<?xml version="1.0"?>
<testsuite name="pytest" tests="5">
  <testcase classname="tests.test_member_compact" name="test_member_compact"/>
  <testcase classname="tests.test_member_round_robin" name="test_member_round_robin">
    <failure message="TypeError">boom</failure>
  </testcase>
  <testcase classname="tests.test_member_flatten_one" name="test_member_flatten_one">
    <skipped message="skip">s</skipped>
  </testcase>
  <testcase classname="tests.test_member_klassy.TestKlass" name="test_k"/>
  <testcase classname="tests.test_member_phantom" name="test_member_phantom"/>
</testsuite>
"""

NAMES = ["test_member_compact", "test_member_round_robin",
         "test_member_flatten_one", "test_member_absent",
         "test_member_klassy", "test_member_phantom"]


class TestParseMemberJunit(unittest.TestCase):
    def test_parse_and_attribution(self):
        results, counts = _parse_member_junit(
            XML, NAMES, {"compact", "round_robin", "flatten_one", "klassy"}
        )
        self.assertEqual(len(results), 6)
        self.assertTrue(results["compact"]["passed"])
        self.assertTrue(results["compact"]["targeted"])
        self.assertFalse(results["round_robin"]["passed"])
        self.assertEqual(results["round_robin"]["failures"], 1)
        # all-skipped member is not a pass
        self.assertFalse(results["flatten_one"]["passed"])
        self.assertEqual(results["flatten_one"]["skipped"], 1)
        # absent member: never produced, never tested -> orchestrator miss
        self.assertFalse(results["absent"]["targeted"])
        self.assertFalse(results["absent"]["passed"])
        # class-based classname still resolves to its module member
        self.assertTrue(results["klassy"]["passed"])
        self.assertEqual(results["klassy"]["tests"], 1)
        # oracle passed but worker never produced the file: not a pass,
        # and it attributes to the orchestrator, not the worker
        self.assertEqual(results["phantom"]["tests"], 1)
        self.assertFalse(results["phantom"]["targeted"])
        self.assertFalse(results["phantom"]["passed"])
        self.assertEqual(counts["worker_miss"], 2)      # round_robin + flatten_one
        self.assertEqual(counts["orchestrator_miss"], 2)  # absent + phantom

    def test_empty_results_still_attributed(self):
        results, counts = _parse_member_junit(
            "<testsuite/>", ["test_member_solo"], set()
        )
        self.assertFalse(results["solo"]["passed"])
        self.assertEqual(counts["orchestrator_miss"], 1)

    def test_unrelated_classnames_ignored(self):
        xml = '<testsuite><testcase classname="other.test_thing" name="x"/></testsuite>'
        results, _ = _parse_member_junit(xml, ["test_member_solo"], {"solo"})
        self.assertEqual(results["solo"]["tests"], 0)


if __name__ == "__main__":
    unittest.main()
