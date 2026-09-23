"""U5: judge-by-default, the unified inconclusive rule, and KTD14 — the
judge axis never writes the stored mechanical verdict.

Three no-answer paths (parse failure, noul=None, exception) all record
``judge_inconclusive`` on the judge axis: ``report.judge.passed is None``,
``inconclusive`` is set, and no judge_cache row is written. A real judge
rejection is recorded on the judge axis but cannot mutate ``meta.passes``;
a judge pass can never rescue a mechanical failure.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.judge import judge_artifact
from orchestral.logger import EventLogger
from orchestral.providers import Provider, provider_for
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str, retry_limit: int = 0) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role, retry_limit=retry_limit,
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
    )


GOOD_HTML = "<html><title>t</title><body><h1>x</h1></body></html>"


def _orch_client(assembly: str = GOOD_HTML) -> MagicMock:
    client = MagicMock()
    client.chat.side_effect = [
        {"content": json.dumps({"subtasks": [{"id": 0, "description": "s"}]}),
         "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "latency_ms": 1, "id": "p"},
        {"content": assembly,
         "usage": {"prompt_tokens": 200, "completion_tokens": 100}, "latency_ms": 1, "id": "a"},
    ]
    return client


def _worker_client(content: str = "section") -> MagicMock:
    client = MagicMock()
    client.chat.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "latency_ms": 1, "id": "w",
    }
    return client


def _judge_client(content: str) -> MagicMock:
    """Chat-judge mock returning `content` verbatim as the completion."""
    client = MagicMock()
    client.chat.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "latency_ms": 1, "id": "j",
    }
    return client


def _decisions_client(verdict_noul) -> MagicMock:
    client = MagicMock()
    client.decide.return_value = {
        "answers": {
            "verdict": {"noul": verdict_noul},
            "quality": {"score": 2.0, "confidence": 0.8, "probabilities": {}},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0001},
        "id": "d",
    }
    return client


def _run(tmp: str, judge: ModelConfig | None, judge_client=None,
         assembly: str = GOOD_HTML, task: TaskSpec | None = None) -> tuple:
    store = RunStore(tmp)
    clients = {"orchestrator": _orch_client(assembly), "worker": _worker_client()}
    if judge_client is not None:
        clients["judge"] = judge_client
    runner = Runner(runs_dir=tmp, store=store, clients=clients)
    meta = runner.run(
        task or TaskSpec(id="t1", type="html", prompt="make a page",
                         validation=["non_empty"]),
        _model("o/m", "orchestrator"), _model("w/m", "worker"), judge=judge,
    )
    report = json.loads((Path(meta.run_dir) / "report.json").read_text())
    return meta, report, store


class TestJudgeNeverGatesMechanical(unittest.TestCase):
    """KTD14: passes is the mechanical verdict; the judge is a separate axis."""

    def test_judge_reject_does_not_fail_mechanical_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            meta, report, _ = _run(
                tmp, judge,
                _judge_client(json.dumps({"score": 0.1, "passed": False,
                                          "reasoning": "bad page"})))
            self.assertTrue(meta.passes)                      # mechanical verdict stands
            self.assertIsNone(meta.failure_reason)            # no "judge" failure class
            self.assertFalse(report["judge"]["passed"])       # rejection recorded on judge axis
            self.assertEqual(meta.status, "finished")

    def test_judge_pass_cannot_rescue_mechanical_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            task = TaskSpec(id="t1", type="html", prompt="make a page",
                            validation=["non_empty", "has_title"])
            meta, report, _ = _run(
                tmp, judge,
                _judge_client(json.dumps({"score": 0.9, "passed": True})),
                # starts with "<" so assemble_raw uses it verbatim — no title
                assembly="<html><body>x</body></html>",
                task=task)
            self.assertFalse(meta.passes)
            self.assertEqual(meta.failure_reason, "validation")  # never "judge"
            self.assertTrue(report["judge"]["passed"])


class _CodeJudgeFake:
    """One client for the whole pipeline: plan JSON for the orchestrator,
    a files payload for workers, judge JSON for anything else."""

    def __init__(self, impl: str, judge_json: str):
        self.impl = impl
        self.judge_json = judge_json

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except (json.JSONDecodeError, TypeError, KeyError):
            data = {}
        if "subtask" in data:
            body = json.dumps({"files": [{"path": "fizzbuzz.py", "content": self.impl}]})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [{"id": 0, "description": "impl"}]})
        else:
            body = self.judge_json
        return {"content": body, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_ms": 1, "id": "f"}


class TestScoreAxesStaySeparate(unittest.TestCase):
    """U7: `score` is mechanical; the judge's number lives on judge_score.
    A judged run must not have its mechanical score overwritten (the same
    conflation KTD14 removed from `passes`, on the score axis)."""

    def test_judge_score_lands_on_judge_axis_not_score(self):
        judge = _model("j/m", "judge")
        with tempfile.TemporaryDirectory() as tmp:
            meta, report, store = _run(
                tmp, judge, _judge_client(json.dumps(
                    {"score": 0.9, "passed": True, "reasoning": "fine"})))
            # html has no mechanical score — it must stay None, not become 0.9
            self.assertIsNone(report["score"])
            self.assertEqual(report["judge"]["score"], 0.9)
            self.assertIsNone(meta.score)
            self.assertEqual(meta.judge_score, 0.9)
            self.assertTrue(meta.judge_passed)
            # and it round-trips through the index
            again = store.get_run(meta.run_id)
            self.assertIsNone(again.score)
            self.assertEqual(again.judge_score, 0.9)
            self.assertTrue(again.judge_passed)

    def test_mechanical_score_survives_judge_on_code_task(self):
        """A real mechanical score (test fraction) must survive a judged run —
        the live code path executes the unittest suite for real."""
        tests = (
            "import unittest\nfrom fizzbuzz import fizzbuzz\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_plain(self): self.assertEqual(fizzbuzz(1), 1)\n"
            "    def test_fizz(self): self.assertEqual(fizzbuzz(3), 'Fizz')\n"
        )
        task = TaskSpec(id="code-t", type="code", prompt="p",
                        metadata={"module": "fizzbuzz.py", "tests": tests})
        fake = _CodeJudgeFake(
            "def fizzbuzz(n):\n    return n\n",  # passes 1 of 2 tests
            json.dumps({"score": 0.9, "passed": True, "reasoning": "fine"}))
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": fake, "worker": fake, "judge": fake},
            ).run(task, _model("o/m", "orchestrator"), _model("w/m", "worker"),
                  judge=_model("j/m", "judge"))
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertEqual(report["score"], 0.5)       # mechanical fraction
            self.assertEqual(report["judge"]["score"], 0.9)
            self.assertEqual(meta.score, 0.5)
            self.assertEqual(meta.judge_score, 0.9)
            self.assertFalse(meta.passes)                 # tests fail → mechanical fail
            self.assertTrue(meta.judge_passed)

    def test_inconclusive_writes_no_judge_fields(self):
        judge = _model("j/m", "judge")
        with tempfile.TemporaryDirectory() as tmp:
            meta, report, store = _run(
                tmp, judge, _judge_client("this is not json"))
            self.assertTrue(report["judge"]["inconclusive"])
            self.assertIsNone(meta.judge_score)
            self.assertIsNone(meta.judge_passed)
            self.assertIsNone(meta.score)


class TestInconclusiveRule(unittest.TestCase):
    """KTD7: parse failure, null verdict, and exception share one rule."""

    def test_parse_failed_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            meta, report, _ = _run(tmp, judge, _judge_client("not json at all"))
            self.assertTrue(meta.passes)
            j = report["judge"]
            self.assertIsNone(j["passed"])
            self.assertTrue(j["inconclusive"])
            self.assertEqual(j["model"], "j/model")

    def test_noul_none_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("~typesafe/jev-latest", "judge")
            meta, report, _ = _run(tmp, judge, _decisions_client(None))
            self.assertTrue(meta.passes)
            j = report["judge"]
            self.assertIsNone(j["passed"])
            self.assertTrue(j["inconclusive"])
            self.assertEqual(j["model"], "~typesafe/jev-latest")

    def test_judge_exception_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            flaky = _judge_client("")
            flaky.chat.side_effect = RuntimeError("provider 500")
            meta, report, _ = _run(tmp, judge, flaky)
            self.assertTrue(meta.passes)
            self.assertEqual(meta.status, "finished")
            j = report["judge"]
            self.assertIsNone(j["passed"])
            self.assertTrue(j["inconclusive"])
            self.assertEqual(j["model"], "j/model")

    def test_inconclusive_not_cached(self):
        """A transient no-answer must not freeze in judge_cache — a retry
        on the same artifact hash re-calls the judge."""
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            task = TaskSpec(id="t1", type="html", prompt="make a page",
                            validation=["non_empty"])
            meta, _, store = _run(tmp, judge, _judge_client("garbage"), task=task)
            sha = hashlib.sha256(task.prompt.encode() + b"\0" + GOOD_HTML.encode()).hexdigest()
            self.assertIsNone(store.get_judge_result(task.id, judge.slug, sha))

            good = _judge_client(json.dumps({"score": 0.8, "passed": True}))
            _run(tmp, judge, good, task=task)
            self.assertEqual(good.chat.call_count, 1)  # re-called, not replayed
            self.assertIsNotNone(store.get_judge_result(task.id, judge.slug, sha))

    def test_string_verdict_coerces_not_truthy(self):
        """A judge replying `"passed": "false"` must read False — Python's
        bool("false") is True, so the parse uses an explicit table."""
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            _, report, _ = _run(
                tmp, judge,
                _judge_client(json.dumps({"score": 0.2, "passed": "false"})))
            j = report["judge"]
            self.assertIs(j["passed"], False)
            self.assertFalse(j.get("inconclusive"))

    def test_garbage_verdict_is_inconclusive(self):
        """`"passed": "maybe"` is no verdict — inconclusive, not bool() luck."""
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            _, report, _ = _run(
                tmp, judge,
                _judge_client(json.dumps({"score": 0.5, "passed": "maybe"})))
            j = report["judge"]
            self.assertIsNone(j["passed"])
            self.assertTrue(j["inconclusive"])

    def test_garbage_score_is_none_not_crash(self):
        """`"score": null`/garbage used to escape the parse guard and surface
        as judge_error — now it degrades to a scored-None verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            judge = _model("j/model", "judge")
            meta, report, _ = _run(
                tmp, judge,
                _judge_client(json.dumps({"score": None, "passed": True})))
            self.assertEqual(meta.status, "finished")
            j = report["judge"]
            self.assertIsNone(j["score"])
            self.assertTrue(j["passed"])


class TestJudgeResultProvenance(unittest.TestCase):
    """Every report.judge result carries model + inconclusive markers,
    including the skip paths."""

    def _judge_direct(self, judge: ModelConfig, **kw) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            with EventLogger(Path(tmp)) as logger:
                result, _ = judge_artifact(
                    logger=logger, step=0,
                    task=TaskSpec(id="t", type="image", prompt="p"),
                    artifact="", judge=judge, dry_run=False, **kw)
            return result

    def test_oversized_image_skip_stamped(self):
        from orchestral.judge import MAX_JUDGE_IMAGE_BYTES
        result = self._judge_direct(
            _model("j/model", "judge"),
            client=MagicMock(), image_bytes=b"x" * (MAX_JUDGE_IMAGE_BYTES + 1))
        self.assertIsNone(result["passed"])
        self.assertTrue(result["inconclusive"])
        self.assertEqual(result["model"], "j/model")

    def test_decisions_image_skip_stamped(self):
        result = self._judge_direct(
            _model("~typesafe/jev-latest", "judge"),
            client=MagicMock(), image_bytes=b"png")
        self.assertIsNone(result["passed"])
        self.assertTrue(result["inconclusive"])
        self.assertEqual(result["model"], "~typesafe/jev-latest")


class TestDecideProtocol(unittest.TestCase):
    """decide() joins the Provider protocol; non-OpenRouter providers
    raise NotImplementedError honestly."""

    def test_decisions_client_is_a_provider(self):
        class FullClient:
            def chat(self, **kw): return {}
            def images(self, **kw): return {}
            def videos(self, **kw): return {}
            def decide(self, **kw): return {}
            def close(self): pass

        class NoDecideClient:
            def chat(self, **kw): return {}
            def images(self, **kw): return {}
            def videos(self, **kw): return {}
            def close(self): pass

        self.assertIsInstance(FullClient(), Provider)
        self.assertNotIsInstance(NoDecideClient(), Provider)  # decide is required

    def test_non_openrouter_decide_raises(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}):
            client = provider_for(ModelConfig(
                slug="w/m", name="w", role="worker",
                input_price_per_mtok=0.1, output_price_per_mtok=0.4,
                metadata={"provider": "openai-compatible",
                          "base_url": "http://127.0.0.1:9/v1"}))
        with self.assertRaises(NotImplementedError):
            client.decide(model="~typesafe/jev-latest", state={}, questions={})
        client.close()


if __name__ == "__main__":
    unittest.main()
