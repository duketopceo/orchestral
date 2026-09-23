"""Observatory substrate: sequenced lifecycle events, manifest, leaderboard, export."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.export import leaderboard_csv, run_audit_markdown, runs_csv
from orchestral.logger import LIFECYCLE_EVENTS, EventLogger
from orchestral.runner import Runner
from orchestral.stats import MIN_LEADERBOARD_SAMPLES, pairing_leaderboard
from orchestral.storage import RunMeta, RunStore


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


def _plan_client() -> MagicMock:
    return _chat_client(json.dumps({"subtasks": [{"id": 0, "description": "do the thing"}]}))


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t1", "worker": "w/m",
        "status": "finished", "started_at": "t", "finished_at": "t",
        "total_cost_usd": 0.01, "total_input_tokens": 10, "total_output_tokens": 5,
        "score": 0.8, "passes": True, "latency_ms": 1000.0,
    }
    base.update(kw)
    return RunMeta(**base)


class TestEventSchemaV2(unittest.TestCase):
    def test_events_carry_sequence_run_id_worker_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(tmp, run_id="r1")
            e1 = logger.lifecycle("run.created", phase="init", run_id="r1")
            e2 = logger.lifecycle("worker.started", worker_id="worker-0", subtask_id=3)
            e3 = logger.log(
                phase="delegate", step=3, event_type="worker_error", model="w/m",
                role="worker", worker_id="worker-0", input_data={}, output_data={},
            )
            logger.close()
            self.assertEqual((e1["sequence"], e2["sequence"], e3["sequence"]), (1, 2, 3))
            self.assertEqual(e1["run_id"], "r1")
            self.assertEqual(e2["worker_id"], "worker-0")
            self.assertEqual(e3["worker_id"], "worker-0")
            self.assertEqual(e1["schema_version"], "2")
            self.assertEqual(e2["output"]["subtask_id"], 3)

    def test_lifecycle_rejects_unknown_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(tmp)
            with self.assertRaises(ValueError):
                logger.lifecycle("not.real")
            logger.close()

    def test_vocabulary_covers_terminal_and_phase_events(self):
        for needed in (
            "run.created", "run.started", "task.loaded",
            "orchestrator.started", "orchestrator.completed",
            "delegation.created", "worker.started", "worker.progress",
            "worker.completed", "worker.failed",
            "synthesis.started", "synthesis.completed",
            "evaluation.started", "evaluation.completed",
            "usage.recorded", "artifact.saved",
            "run.completed", "run.failed", "run.cancelled",
        ):
            self.assertIn(needed, LIFECYCLE_EVENTS)


class TestManifestAndLifecycle(unittest.TestCase):
    def test_dry_run_writes_manifest_and_lifecycle_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store, dry_run=True,
                run_group="g1", replicate=2, seed=9,
                clients={"orchestrator": _plan_client(), "worker": _chat_client("<html>x</html>")},
            )
            meta = runner.run(
                TaskSpec(id="t1", type="html", prompt="make a page"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            run_dir = Path(meta.run_dir)

            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "passed")
            self.assertEqual(manifest["run_id"], meta.run_id)
            self.assertEqual(manifest["task_id"], "t1")
            self.assertEqual(manifest["orchestrator_model"], "o/m")
            self.assertEqual(manifest["worker_model"], "w/m")
            self.assertEqual(manifest["run_group"], "g1")
            self.assertEqual(manifest["replicate"], 2)
            self.assertEqual(manifest["seed"], 9)
            for field in ("task_hash", "config_hash", "orchestrator_prompt_hash",
                          "worker_prompt_hash", "git_commit", "harness_version",
                          "python_version", "finished_at"):
                self.assertTrue(manifest.get(field), f"manifest missing {field}")

            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            types = [e["type"] for e in events]
            for needed in (
                "run.created", "task.loaded", "run.started",
                "orchestrator.started", "orchestrator.completed",
                "delegation.created", "worker.started", "worker.completed",
                "synthesis.started", "synthesis.completed",
                "evaluation.started", "evaluation.completed",
                "usage.recorded", "artifact.saved", "run.completed",
            ):
                self.assertIn(needed, types)
            # sequence is monotonic and terminal event is last
            seqs = [e["sequence"] for e in events]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(types[-1], "run.completed")
            self.assertEqual(types.index("run.created"), 0)
            # lifecycle events carry their worker scope
            wstart = next(e for e in events if e["type"] == "worker.started")
            self.assertEqual(wstart["worker_id"], "worker-0")

    def test_manifest_status_failed_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            orch = MagicMock()
            orch.chat.side_effect = RuntimeError("boom")
            runner = Runner(
                runs_dir=tmp, store=store,
                clients={"orchestrator": orch, "worker": _chat_client("")},
            )
            with self.assertRaises(RuntimeError):
                runner.run(
                    TaskSpec(id="t1", type="html", prompt="p"),
                    _model("o/m", "orchestrator"), _model("w/m", "worker"),
                )
            run_dir = Path(store.list_runs()[0].run_dir)
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("unknown", manifest["failure_reason"])
            self.assertTrue(manifest["finished_at"])
            last = json.loads((run_dir / "events.jsonl").read_text().splitlines()[-1])
            self.assertEqual(last["type"], "run.failed")

    def test_task_hash_changes_with_content(self):
        from orchestral.manifest import task_hash

        t1 = TaskSpec(id="t", type="html", prompt="a")
        t2 = TaskSpec(id="t", type="html", prompt="b")
        self.assertNotEqual(task_hash(t1), task_hash(t2))
        self.assertEqual(task_hash(t1), task_hash(TaskSpec(id="t", type="html", prompt="a")))


class TestLeaderboard(unittest.TestCase):
    def test_pairing_aggregates(self):
        runs = [
            _meta(run_id=f"a{i}", orchestrator="o/big", worker="w/cheap",
                  score=0.9, total_cost_usd=0.02, passes=i % 2 == 0)
            for i in range(4)
        ] + [
            _meta(run_id=f"b{i}", orchestrator="o/small", worker="w/mid",
                  score=0.5, total_cost_usd=0.005, passes=True)
            for i in range(3)
        ]
        board = pairing_leaderboard(runs, min_samples=10)
        self.assertEqual(len(board), 2)
        # w/mid is cheaper per pass: $0.015/3 < $0.08/2 → sorted first
        self.assertEqual(board[0].worker, "w/mid")
        a = next(p for p in board if p.orchestrator == "o/big")
        self.assertEqual(a.runs, 4)
        self.assertEqual(a.passed, 2)
        self.assertAlmostEqual(a.cost_per_pass or 0, 0.04)
        self.assertEqual(a.tasks_covered, 1)
        self.assertTrue(a.low_sample)  # 4 < 10

    def test_judge_axis_separate_from_mechanical_score(self):
        """`score_median` is the mechanical axis; `judge_score_median` reads
        meta.judge_score only — a run can carry both and they must not mix."""
        runs = [
            _meta(run_id=f"r{i}", score=0.5, judge_score=0.9, judge_passed=True)
            for i in range(3)
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertAlmostEqual(row.score_median or 0, 0.5)
        self.assertAlmostEqual(row.judge_score_median or 0, 0.9)
        # an unjudged run contributes no judge axis
        row2 = pairing_leaderboard([_meta(run_id="u", score=0.5, judge_score=None)])[0]
        self.assertIsNone(row2.judge_score_median)

    def test_low_sample_threshold_configurable(self):
        runs = [_meta(run_id=f"r{i}") for i in range(3)]
        self.assertFalse(pairing_leaderboard(runs)[0].low_sample)
        self.assertTrue(pairing_leaderboard(runs, min_samples=4)[0].low_sample)
        self.assertEqual(MIN_LEADERBOARD_SAMPLES, 3)

    def test_low_sample_never_outranks_evidence(self):
        """A 1/1 100% pairing is an anecdote — it tails the board even
        when an evidence-backed row has a lower pass rate."""
        runs = [_meta(run_id="lucky", orchestrator="o/lucky", worker="w/lucky")] + [
            _meta(run_id=f"m{i}", orchestrator="o/real", worker="w/real",
                  passes=i < 3)
            for i in range(6)
        ]
        board = pairing_leaderboard(runs)
        self.assertEqual(board[0].orchestrator, "o/real")
        self.assertFalse(board[0].low_sample)
        self.assertEqual(board[-1].orchestrator, "o/lucky")
        self.assertTrue(board[-1].low_sample)
        self.assertEqual(board[-1].pass_rate, 1.0)

    def test_unfinished_runs_count_in_n_but_not_medians(self):
        runs = [
            _meta(run_id="ok", total_cost_usd=0.02, latency_ms=100),
            _meta(run_id="bad", status="failed", passes=False,
                  failure_reason="exception:timeout", total_cost_usd=0.5,
                  latency_ms=9999),
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertEqual(row.runs, 2)
        self.assertEqual(row.finished, 1)
        self.assertAlmostEqual(row.cost_median, 0.02)  # failed run's cost excluded
        self.assertAlmostEqual(row.duration_median_ms, 100)
        self.assertAlmostEqual(row.failure_rate or 0, 0.5)
        self.assertEqual(row.failures.get("exception:timeout"), 1)

    def test_crashed_runs_are_not_evidence(self):
        """Three infra-failed runs and zero finished is not a 3-sample
        pairing — pass_rate is a capability axis over finished runs."""
        runs = [
            _meta(run_id=f"c{i}", status="failed", passes=False,
                  failure_reason="exception:timeout")
            for i in range(3)
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertEqual(row.runs, 3)
        self.assertEqual(row.finished, 0)
        self.assertIsNone(row.pass_rate)
        self.assertTrue(row.low_sample)      # 0 finished < 3: cannot rank

    def test_pass_rate_counts_finished_only(self):
        runs = [
            _meta(run_id="p", passes=True),
            _meta(run_id="f", passes=False),
            _meta(run_id="c1", status="failed", passes=False,
                  failure_reason="exception:x"),
            _meta(run_id="c2", status="failed", passes=False,
                  failure_reason="exception:x"),
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertAlmostEqual(row.pass_rate or 0, 0.5)   # 1/2 finished, not 1/4
        self.assertAlmostEqual(row.failure_rate or 0, 0.75)  # infra noise still visible

    def test_zero_cost_is_unmetered_not_free(self):
        """A $0 total means the meter read nothing — it must not outrank a
        pairing with a real (nonzero) cost_per_pass."""
        runs = [
            _meta(run_id=f"z{i}", orchestrator="o/free", worker="w/free",
                  passes=True, total_cost_usd=0.0)
            for i in range(3)
        ] + [
            _meta(run_id=f"p{i}", orchestrator="o/paid", worker="w/paid",
                  passes=True, total_cost_usd=0.01)
            for i in range(3)
        ]
        board = pairing_leaderboard(runs)
        free = next(p for p in board if p.orchestrator == "o/free")
        self.assertIsNone(free.cost_per_pass)
        self.assertEqual(board[0].orchestrator, "o/paid")

    def test_default_order_is_pass_rate_desc(self):
        """The headline question is 'which pairing performs best' — pass rate
        ranks first, cost per pass breaks ties."""
        runs = [
            _meta(run_id=f"hi{i}", orchestrator="o/hi", worker="w/hi",
                  passes=True, total_cost_usd=0.10)
            for i in range(3)
        ] + [
            _meta(run_id=f"lo{i}", orchestrator="o/lo", worker="w/lo",
                  passes=i < 2, total_cost_usd=0.001)
            for i in range(3)
        ]
        board = pairing_leaderboard(runs)
        self.assertEqual(board[0].orchestrator, "o/hi")   # 100% > 67% despite 100x cost


class TestExport(unittest.TestCase):
    def test_runs_csv(self):
        out = runs_csv([_meta(run_id="r1"), _meta(run_id="r2", passes=False)])
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("run_id", lines[0])
        self.assertIn("r1", lines[1])

    def test_leaderboard_csv(self):
        rows = pairing_leaderboard([_meta(run_id="r1")])
        out = leaderboard_csv(rows)
        self.assertIn("cost_per_pass", out)
        self.assertIn("o/m", out)

    def test_run_audit_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run1"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(json.dumps({
                "task_id": "t1", "task_hash": "abc123",
                "orchestrator_model": "o/m", "orchestrator_provider": "openrouter",
                "worker_model": "w/m", "worker_provider": "openrouter",
                "status": "passed", "started_at": "s", "finished_at": "f",
                "git_commit": "deadbeef", "harness_version": "0.3",
                "python_version": "3.11", "seed": None, "run_group": None,
                "replicate": None,
            }))
            (run_dir / "report.json").write_text(json.dumps({
                "checks": {"non_empty": True}, "score": 0.9, "errors": [],
            }))
            (run_dir / "artifact.html").write_text("<html>x</html>")
            md = run_audit_markdown(run_dir)
            self.assertIn("t1", md)
            self.assertIn("o/m", md)
            self.assertIn("non_empty: pass", md)
            self.assertIn("artifact.html", md)

    def test_run_audit_markdown_tolerates_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = run_audit_markdown(Path(tmp))
            self.assertIn("Run audit", md)


if __name__ == "__main__":
    unittest.main()
