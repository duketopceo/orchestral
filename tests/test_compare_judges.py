"""Judge agreement surface: pairwise verdict agreement, noul deltas, and
the divergent-run list for runs carrying ≥2 judge verdicts."""

from __future__ import annotations

import unittest

from orchestral.judge import judge_agreement

JEV = "~typesafe/jev-latest"
PPLX = "perplexity/pplx-decider-v1.1-27b"


def _v(passed: bool, noul: float, score: float) -> dict:
    return {"passed": passed, "noul": noul, "score": score, "model": "m"}


def _row(run_id: str, task: str, judges: dict) -> dict:
    return {"run_id": run_id, "task_id": task, "judges": judges}


class TestJudgeAgreement(unittest.TestCase):
    def test_agreement_math(self):
        rows = [
            _row("r1", "t", {JEV: _v(True, 0.9, 0.75), PPLX: _v(True, 0.8, 0.5)}),
            _row("r2", "t", {JEV: _v(True, 0.7, 0.75), PPLX: _v(False, 0.2, 0.25)}),
        ]
        out = judge_agreement(rows)
        self.assertEqual(out["runs_considered"], 2)
        self.assertEqual(out["runs_multi_judged"], 2)
        (pair,) = out["pairs"]
        self.assertEqual(pair["judges"], sorted([JEV, PPLX]))
        self.assertEqual(pair["compared"], 2)
        self.assertAlmostEqual(pair["verdict_agreement"], 0.5)
        self.assertAlmostEqual(pair["mean_noul_delta"], (0.1 + 0.5) / 2)
        self.assertAlmostEqual(pair["mean_score_delta"], (0.25 + 0.5) / 2)
        (div,) = pair["divergent"]
        self.assertEqual(div["run_id"], "r2")
        self.assertAlmostEqual(div["noul_delta"], 0.5)

    def test_divergent_sorted_by_noul_delta(self):
        rows = [
            _row("small", "t", {JEV: _v(True, 0.6, 0.5), PPLX: _v(False, 0.45, 0.5)}),
            _row("big", "t", {JEV: _v(True, 0.95, 0.9), PPLX: _v(False, 0.05, 0.1)}),
        ]
        out = judge_agreement(rows)
        ids = [d["run_id"] for d in out["pairs"][0]["divergent"]]
        self.assertEqual(ids, ["big", "small"])

    def test_single_judge_and_inconclusive_excluded(self):
        rows = [
            _row("one-judge", "t", {JEV: _v(True, 0.9, 0.9)}),
            _row("inconclusive-extra", "t", {
                JEV: _v(True, 0.9, 0.9),
                PPLX: {"inconclusive": True, "passed": None, "noul": None},
            }),
            _row("no-judges", "t", {}),
        ]
        out = judge_agreement(rows)
        self.assertEqual(out["runs_considered"], 3)
        self.assertEqual(out["runs_multi_judged"], 0)
        self.assertEqual(out["pairs"], [])

    def test_null_passed_not_compared(self):
        rows = [
            _row("r", "t", {JEV: {"passed": True, "noul": 0.9},
                            PPLX: {"passed": None, "noul": 0.5}}),
        ]
        out = judge_agreement(rows)
        self.assertEqual(out["runs_multi_judged"], 1)
        self.assertEqual(out["pairs"][0]["compared"], 0)
        self.assertIsNone(out["pairs"][0]["verdict_agreement"])


if __name__ == "__main__":
    unittest.main()
