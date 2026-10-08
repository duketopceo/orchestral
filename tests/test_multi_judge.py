"""Multi-judge runs: a comma-separated --judge list produces one primary
verdict (report.judge) plus a second-opinion verdict per extra slug under
report.judges[slug] — model-marked, cost-attributed, and isolated: one
judge's failure doesn't lose the others."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

import harness
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner


def _model(slug: str, role: str = "worker", **kw) -> ModelConfig:
    kw.setdefault("input_price_per_mtok", 0.1)
    kw.setdefault("output_price_per_mtok", 0.4)
    return ModelConfig(slug=slug, name=slug, role=role, **kw)


def _task() -> TaskSpec:
    return TaskSpec(id="t-multi", type="html", prompt="Make a page.")


class _PipeClient:
    """Orchestrator/worker chat stub — plans one subtask, emits content."""

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = json.dumps({"content": "<html><title>t</title>done</html>"})
        else:
            body = json.dumps({"subtasks": [{"id": 0, "description": "make it"}]})
        return {"content": body, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_ms": 1, "id": "fake"}

    def close(self):
        pass


class _Decider:
    """Decisions-engine stub returning a planted noul/rubric per judge."""

    def __init__(self, noul: float, rubric: int, boom: Exception | None = None):
        self.noul, self.rubric, self.boom = noul, rubric, boom
        self.calls: list[str] = []

    def decide(self, *, model, state, questions):
        self.calls.append(model)
        if self.boom is not None:
            raise self.boom
        return {
            "answers": {
                "verdict": {"noul": self.noul},
                "quality": {"score": self.rubric, "confidence": 0.9,
                            "probabilities": [0, 0, 0, 0.4, 0.6]},
            },
            "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0.00004},
            "latency_ms": 5,
        }

    def close(self):
        pass


JEV = "~typesafe/jev-latest"
PPLX = "perplexity/pplx-decider-v1.1-27b"


def _run(tmp: str, jev: _Decider, pplx: _Decider) -> dict:
    pipe = _PipeClient()
    jev_cfg = _model(JEV, "judge")
    pplx_cfg = _model(PPLX, "judge", metadata={"engine": "decisions"})
    meta = Runner(
        runs_dir=Path(tmp), planner="raw",
        clients={"orchestrator": pipe, "worker": pipe, "judge": jev,
                 f"judge:{PPLX}": pplx},
    ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"),
          jev_cfg, extra_judges=[pplx_cfg])
    return json.loads((Path(meta.run_dir) / "report.json").read_text())


class TestMultiJudgeRun(unittest.TestCase):
    def test_both_judges_recorded_primary_is_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = _run(tmp, _Decider(0.9, 3), _Decider(0.2, 1))
            self.assertEqual(report["judge"]["model"], JEV)
            self.assertEqual(set(report["judges"]), {JEV, PPLX})
            self.assertEqual(report["judges"][JEV]["noul"], 0.9)
            self.assertEqual(report["judges"][PPLX]["noul"], 0.2)
            self.assertFalse(report["judges"][PPLX]["passed"])
            self.assertTrue(report["judges"][JEV]["passed"])
            # both verdicts carry provenance for the agreement surface
            for slug in (JEV, PPLX):
                self.assertEqual(report["judges"][slug]["model"], slug)
                self.assertEqual(report["judges"][slug]["engine"], "decisions")

    def test_extra_judge_failure_keeps_primary_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = _run(tmp, _Decider(0.9, 3), _Decider(0, 0, boom=RuntimeError("api down")))
            self.assertTrue(report["judges"][JEV]["passed"])
            self.assertTrue(report["judges"][PPLX]["inconclusive"])
            self.assertIsNone(report["judges"][PPLX]["passed"])
            self.assertIn(PPLX, report.get("judge_errors", {}))
            self.assertEqual(report["judge"]["model"], JEV)


class TestJudgeSlugParsing(unittest.TestCase):
    def _args(self, judge=None, no_judge=False):
        return argparse.Namespace(judge=judge, no_judge=no_judge, models_dir="models")

    def test_single_slug_no_extras(self):
        args = self._args(judge="openai/gpt-x")
        self.assertEqual(harness._judge_from_arg(args).slug, "openai/gpt-x")
        self.assertEqual(harness._extra_judges_from_arg(args), [])

    def test_comma_list_splits_primary_from_extras(self):
        args = self._args(judge="~typesafe/jev-latest, perplexity/pplx-decider-v1.1-27b")
        self.assertEqual(harness._judge_from_arg(args).slug, JEV)
        extras = harness._extra_judges_from_arg(args)
        self.assertEqual([e.slug for e in extras], [PPLX])
        # the yaml entry marks it a decisions engine — it must not fall
        # through to the free-text chat judge path
        from orchestral.judge import is_decisions_model
        self.assertTrue(is_decisions_model(extras[0]))

    def test_no_judge_clears_everything(self):
        args = self._args(judge=f"{JEV},{PPLX}", no_judge=True)
        self.assertIsNone(harness._judge_from_arg(args))
        self.assertEqual(harness._extra_judges_from_arg(args), [])


class TestJudgeCostCap(unittest.TestCase):
    def _args(self, cap):
        a = argparse.Namespace()
        a.judge_cost_cap = cap
        return a

    def test_decisions_extra_fits_under_default_cap(self):
        decider = _model(PPLX, "judge", input_price_per_mtok=0.02,
                         output_price_per_mtok=0.0,
                         metadata={"engine": "decisions"})
        harness._check_judge_cost_cap(self._args(0.001), [decider])

    def test_pricy_chat_judge_trips_the_cap(self):
        chatty = _model("x-ai/grok-4.7", "judge",
                        input_price_per_mtok=1.60, output_price_per_mtok=4.80,
                        max_tokens=8192)
        with self.assertRaises(SystemExit):
            harness._check_judge_cost_cap(self._args(0.001), [chatty])
        # explicit opt-in: a raised cap lets the same selection through
        harness._check_judge_cost_cap(self._args(0.10), [chatty])

    def test_zero_cap_disables_the_check(self):
        chatty = _model("x-ai/grok-4.7", "judge",
                        input_price_per_mtok=1.60, output_price_per_mtok=4.80,
                        max_tokens=8192)
        harness._check_judge_cost_cap(self._args(0.0), [chatty])


if __name__ == "__main__":
    unittest.main()
