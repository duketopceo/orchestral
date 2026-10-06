"""Tests for the Inspect AI eval-log export (`export --format inspect`)."""

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.export import (
    inspect_eval_log,
    inspect_group_key,
    inspect_log_name,
)
from orchestral.storage import RunMeta


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r1", "orchestrator": "o/m", "task_id": "t1", "worker": "w/m",
        "status": "finished", "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "total_cost_usd": 0.01, "total_input_tokens": 10, "total_output_tokens": 5,
        "score": 0.8, "passes": True, "latency_ms": 1000.0,
        "replicate": 2, "run_group": "exp:t1:o/m:w/m:jev",
    }
    base.update(kw)
    return RunMeta(**base)


def _run_dir(tmp: Path, *, run_id: str = "r1", subtasks=None, worker_output="done",
             failed_run: bool = False) -> Path:
    d = tmp / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "task_id": "t1", "status": "failed" if failed_run else "passed",
        "task_hash": "abc123", "orchestrator_model": "o/m",
        "worker_model": "w/m",
    }))
    (d / "report.json").write_text(json.dumps({
        "checks": {"non_empty": True, "has_title": failed_run is False},
        "judge": {"score": 0.7, "reasoning": "solid", "model": "j/m"},
        "score": 0.8,
    }))
    (d / "plan.json").write_text(json.dumps({
        "plan": "do it", "subtasks": subtasks or [
            {"id": 1, "title": "part one", "description": "first bit"},
        ],
    }))
    (d / "worker-0.json").write_text(json.dumps({
        "id": "1", "subtask_id": "1", "title": "part one",
        "status": "complete", "output": worker_output, "attempts": 1,
    }))
    (d / "debug.jsonl").write_text(
        json.dumps({"message": "chat", "fields": {
            "model": "w/m", "prompt_tokens": 10, "completion_tokens": 5}}) + "\n"
    )
    (d / "artifact.html").write_text("<html>final</html>")
    return d


class TestInspectEvalLog(unittest.TestCase):
    def test_schema_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _meta(run_dir=str(_run_dir(Path(tmp))))
            log = inspect_eval_log([meta], tasks_dir="/nonexistent")
            self.assertEqual(log["version"], 2)
            self.assertEqual(log["status"], "success")
            self.assertEqual(log["eval"]["task"], "t1")
            self.assertEqual(log["eval"]["model"], "orchestral:o/m+w/m")
            self.assertIn("arm:jev", log["eval"]["tags"])
            self.assertEqual(log["results"]["total_samples"], 1)
            self.assertNotIn("scorer", log["results"])  # v2: scores OR scorer, not both
            acc = log["results"]["scores"][0]["metrics"]["accuracy"]
            self.assertEqual(acc["value"], 1.0)
            self.assertEqual(acc["name"], "accuracy")

    def test_sample_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _meta(run_dir=str(_run_dir(Path(tmp))))
            s = inspect_eval_log([meta], tasks_dir="/nonexistent")["samples"][0]
            self.assertEqual(s["id"], "r1")
            self.assertEqual(s["epoch"], 2)
            self.assertEqual(s["scores"]["orchestral_checks"]["value"], "C")
            self.assertEqual(s["scores"]["judge"]["value"], 0.7)
            self.assertEqual(s["metadata"]["arm"], "jev")
            self.assertEqual(s["metadata"]["run_id"], "r1")
            self.assertEqual(
                s["model_usage"]["w/m"],
                {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
            roles = [m["role"] for m in s["messages"]]
            self.assertEqual(roles[0], "assistant")  # plan (no spec -> no user prompt)
            self.assertIn("delegate", s["messages"][1]["content"])
            self.assertEqual(s["output"]["choices"][0]["message"]["content"],
                             "<html>final</html>")

    def test_failed_run_gets_error_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _meta(status="failed", passes=False, failure_reason="boom",
                         run_dir=str(_run_dir(Path(tmp), failed_run=True)))
            s = inspect_eval_log([meta], tasks_dir="/nonexistent")["samples"][0]
            self.assertEqual(s["error"]["message"], "boom")
            self.assertIn("traceback_ansi", s["error"])
            self.assertEqual(s["scores"]["orchestral_checks"]["value"], "I")
            self.assertIn("has_title", s["scores"]["orchestral_checks"]["explanation"])

    def test_string_subtasks_and_dict_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _meta(run_dir=str(_run_dir(
                Path(tmp), subtasks=["just a string"],
                worker_output={"file": "a.html", "content": "<b>x</b>"})))
            s = inspect_eval_log([meta], tasks_dir="/nonexistent")["samples"][0]
            self.assertIn("just a string", s["messages"][1]["content"])
            self.assertEqual(s["messages"][2]["content"], "<b>x</b>")

    def test_grouping_and_names(self):
        a = _meta(run_group="exp:t:o:w:baseline")
        b = _meta(run_group="exp:t:o:w:jev", run_id="r2")
        c = _meta(run_group=None, run_id="r3")
        self.assertEqual(inspect_group_key(a)[-1], "baseline")
        self.assertEqual(inspect_group_key(b)[-1], "jev")
        self.assertEqual(inspect_group_key(c)[-1], "")
        self.assertNotEqual(inspect_group_key(a), inspect_group_key(b))
        name = inspect_log_name(inspect_group_key(a))
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith(".json"))

    def test_group_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            m1 = _meta(run_dir=str(_run_dir(Path(tmp))))
            m2 = _meta(run_id="r2", passes=False,
                       run_dir=str(_run_dir(Path(tmp), run_id="r2", failed_run=True)))
            log = inspect_eval_log([m1, m2], tasks_dir="/nonexistent")
            self.assertEqual(log["results"]["scores"][0]["metrics"]["accuracy"]["value"], 0.5)
            self.assertEqual(log["results"]["scores"][0]["metrics"]["judge_mean"]["value"], 0.7)
            self.assertEqual(log["eval"]["dataset"]["samples"], 2)
            # group usage aggregates per-sample per-model usage
            self.assertEqual(log["stats"]["model_usage"]["w/m"]["total_tokens"], 30)


if __name__ == "__main__":
    unittest.main()
