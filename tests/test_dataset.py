"""Dataset export: step payloads in `calls`, reward join, backfill."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.dataset import iter_episodes, iter_steps, write_dataset
from orchestral.storage import RunMeta, RunStore


def _meta(store: RunStore, tmp: str, run_id: str = "r1", **kw) -> RunMeta:
    run_dir = Path(tmp) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    fields = {
        "run_id": run_id, "orchestrator": "o/m", "task_id": "t", "worker": "w/m",
        "status": "finished", "started_at": "2020-01-01T00:00:00",
        "run_dir": str(run_dir), "score": 1.0, "passes": True, "delegated": True,
        "judge_score": 0.9, "judge_passed": True,
    }
    fields.update(kw)
    meta = RunMeta(**fields)
    store.index_meta(meta)
    return meta


class TestCallsPayloads(unittest.TestCase):
    def test_record_call_stores_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.record_call(
                run_id="r1", phase="plan", step=1, role="orchestrator",
                model="o/m", input_tokens=10, output_tokens=5,
                input_json=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
                output_json=json.dumps({"content": "{}", "finish_reason": "stop"}),
                finish_reason="stop",
            )
            rows = store.calls_for_run("r1")
            self.assertEqual(len(rows), 1)
            msgs = json.loads(rows[0]["input_json"])["messages"]
            self.assertEqual(msgs[0]["content"], "hi")
            self.assertEqual(rows[0]["finish_reason"], "stop")

    def test_backfill_rebuilds_calls_from_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            meta = _meta(store, tmp)
            events = [
                {"type": "run_started", "phase": "start", "input": {}, "output": {}},
                {"type": "llm_call", "phase": "plan", "step": 1, "role": "orchestrator",
                 "model": "o/m", "sequence": 2,
                 "input": {"messages": [{"role": "user", "content": "plan this"}]},
                 "output": {"content": "{}", "finish_reason": "stop"},
                 "cost": {"input_tokens": 100, "output_tokens": 50, "usd": 0.001}},
                {"type": "llm_call", "phase": "delegate", "step": 3, "role": "worker",
                 "model": "w/m", "sequence": 4, "worker_id": "worker-0",
                 "input": {"messages": [{"role": "user", "content": "do it"}]},
                 "output": {"content": "done", "finish_reason": "length"},
                 "cost": {"input_tokens": 10, "output_tokens": 4096, "usd": 0.002}},
            ]
            (Path(meta.run_dir) / "events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events))
            n = store.backfill_calls(meta)
            self.assertEqual(n, 2)
            rows = store.calls_for_run("r1")
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[1]["input_json"])["messages"][0]["content"], "do it")
            self.assertEqual(rows[1]["finish_reason"], "length")
            self.assertEqual(rows[1]["worker_id"], "worker-0")


class TestDatasetExport(unittest.TestCase):
    def _seed(self, tmp: str) -> tuple[RunStore, RunMeta]:
        store = RunStore(tmp)
        meta = _meta(store, tmp)
        (Path(meta.run_dir) / "report.json").write_text(json.dumps({
            "score": 1.0, "delegated": True, "subtasks": 2,
            "judge": {"model": "~typesafe/jev-latest", "engine": "decisions",
                      "score": 0.9, "passed": True},
            "judges": {"~typesafe/jev-latest": {"score": 0.9, "passed": True},
                       "gpt-5.6-sol": {"score": 0.8, "passed": True}},
        }))
        store.record_call(
            run_id="r1", phase="plan", step=1, role="orchestrator", model="o/m",
            input_json=json.dumps({"messages": [{"role": "user", "content": "p"}]}),
            output_json=json.dumps({"content": "{}"}),
        )
        return store, meta

    def test_step_record_carries_outcome_and_reward(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._seed(tmp)
            recs = list(iter_steps(store, reward="judge"))
            self.assertEqual(len(recs), 1)
            r = recs[0]
            self.assertEqual(r["schema"], "orchestral.rl_step/v1")
            self.assertEqual(r["step"]["role"], "orchestrator")
            self.assertEqual(r["step"]["prompt"][0]["content"], "p")
            oc = r["outcome"]
            self.assertEqual(oc["reward"], 0.9)
            self.assertEqual(oc["reward_source"], "judge")
            self.assertTrue(oc["delegated"])
            self.assertEqual(oc["judge_scores"]["gpt-5.6-sol"], 0.8)

    def test_unjudged_run_reward_falls_back_with_best(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            meta = _meta(store, tmp, judge_score=None, judge_passed=None)
            (Path(meta.run_dir) / "report.json").write_text(json.dumps({"score": 1.0}))
            store.record_call(run_id="r1", phase="plan", step=1,
                              role="orchestrator", model="o/m")
            rec = next(iter_steps(store, reward="best"))
            self.assertEqual(rec["outcome"]["reward"], 1.0)
            self.assertEqual(rec["outcome"]["reward_source"], "mechanical")
            rec = next(iter_steps(store, reward="judge"))
            self.assertIsNone(rec["outcome"]["reward"])

    def test_dry_runs_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _meta(store, tmp, run_id="r-dry", dry_run=True)
            store.record_call(run_id="r-dry", phase="plan", step=1,
                              role="orchestrator", model="o/m", dry_run=True)
            self.assertEqual(list(iter_steps(store)), [])
            self.assertEqual(len(list(iter_steps(store, include_dry=True))), 1)

    def test_episode_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._seed(tmp)
            eps = list(iter_episodes(store, reward="judge"))
            self.assertEqual(len(eps), 1)
            self.assertEqual(eps[0]["calls"], 1)
            self.assertEqual(eps[0]["outcome"]["reward"], 0.9)

    def test_write_dataset_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._seed(tmp)
            out = Path(tmp) / "steps.jsonl"
            ep = Path(tmp) / "episodes.jsonl"
            counts = write_dataset(store, out, reward="judge", episodes_path=ep)
            self.assertEqual(counts, {"steps": 1, "episodes": 1})
            line = json.loads(out.read_text().splitlines()[0])
            self.assertEqual(line["run_id"], "r1")


if __name__ == "__main__":
    unittest.main()
