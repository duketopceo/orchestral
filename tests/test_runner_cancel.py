"""Runner cancellation propagation and exit-path cost accounting (U1).

These pin the groundwork bugs the agent-executor phase stands on: a
cancelled delegate must not respawn, a cancel between attempts must stop
the loop, and failed/cancelled runs must still persist their accrued
spend to cost.json and the index.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import harness
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import RunCancelled, Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str, retry_limit: int = 0) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role, retry_limit=retry_limit,
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


def _orch_client() -> MagicMock:
    """Orchestrator mock: returns a one-subtask plan, then an assemble."""
    client = MagicMock()
    client.chat.side_effect = [
        {"content": json.dumps({"subtasks": [{"id": 0, "description": "s"}]}),
         "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "latency_ms": 1, "id": "p"},
        {"content": "<html><title>t</title><body>x</body></html>",
         "usage": {"prompt_tokens": 200, "completion_tokens": 100}, "latency_ms": 1, "id": "a"},
    ]
    return client


class TestCancelPropagation(unittest.TestCase):
    def test_cancel_mid_attempt_not_retried(self):
        """A delegate raising RunCancelled must propagate — not be caught
        by the generic retry handler and respawned."""
        worker = _chat_client("")
        worker.chat.side_effect = RunCancelled("cancelled by user")
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store,
                cancel_event=threading.Event(),
                clients={"orchestrator": _orch_client(), "worker": worker},
            )
            meta = runner.run(
                TaskSpec(id="t1", type="html", prompt="p"),
                _model("o/m", "orchestrator"),
                _model("w/m", "worker", retry_limit=2),  # 3 attempts available
            )
            self.assertEqual(meta.status, "cancelled")
            self.assertEqual(meta.failure_reason, "cancelled")
            self.assertEqual(worker.chat.call_count, 1)  # no respawn

    def test_cancel_between_attempts_stops_loop(self):
        """cancel_event set during a failed attempt prevents the next attempt."""
        event = threading.Event()

        def fail_then_cancel(**_kw):
            event.set()
            raise ValueError("transient flake")

        worker = _chat_client("")
        worker.chat.side_effect = fail_then_cancel
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store, cancel_event=event,
                clients={"orchestrator": _orch_client(), "worker": worker},
            )
            meta = runner.run(
                TaskSpec(id="t1", type="html", prompt="p"),
                _model("o/m", "orchestrator"),
                _model("w/m", "worker", retry_limit=2),
            )
            self.assertEqual(meta.status, "cancelled")
            self.assertEqual(worker.chat.call_count, 1)  # attempt 2 never spawned


class TestExitPathAccounting(unittest.TestCase):
    def test_failed_run_persists_cost(self):
        """A run that dies after billable calls still writes cost.json and
        a non-zero meta.total_cost_usd — spend accounting can't leak."""
        worker = _chat_client("")
        worker.chat.side_effect = ValueError("worker down")
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store,
                clients={"orchestrator": _orch_client(), "worker": worker},
            )
            with self.assertRaises(ValueError):
                runner.run(
                    TaskSpec(id="t1", type="html", prompt="p"),
                    _model("o/m", "orchestrator"),
                    _model("w/m", "worker"),
                )
            meta = store.list_runs(limit=None)[0]
            self.assertEqual(meta.status, "failed")
            self.assertGreater(meta.total_cost_usd, 0)  # plan call was billed
            self.assertTrue((Path(meta.run_dir) / "cost.json").exists())
            self.assertEqual(store.spend_today(), meta.total_cost_usd)

    def test_cancelled_run_persists_cost(self):
        worker = _chat_client("")
        worker.chat.side_effect = RunCancelled("cancelled by user")
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store, cancel_event=threading.Event(),
                clients={"orchestrator": _orch_client(), "worker": worker},
            )
            meta = runner.run(
                TaskSpec(id="t1", type="html", prompt="p"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            self.assertEqual(meta.status, "cancelled")
            self.assertGreater(meta.total_cost_usd, 0)
            self.assertTrue((Path(meta.run_dir) / "cost.json").exists())

    def test_cancelled_run_not_exception_unknown(self):
        worker = _chat_client("")
        worker.chat.side_effect = RunCancelled("cancelled")
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store, cancel_event=threading.Event(),
                clients={"orchestrator": _orch_client(), "worker": worker},
            )
            meta = runner.run(
                TaskSpec(id="t1", type="html", prompt="p"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            self.assertNotIn("exception", meta.failure_reason or "")


class TestReplicatesBudget(unittest.TestCase):
    def test_replicates_recheck_uses_real_count(self):
        """run --replicates 10 --max-cost 0.05 must price ten launches —
        the preamble's n=1 check can't see the replicate count."""
        import argparse
        args = argparse.Namespace(
            task="landing-page-coffee",
            orchestrator="deepseek/deepseek-v4-flash-0731",
            worker="z-ai/glm-5.3-flash",
            planner="raw", judge=None, no_judge_cache=False,
            retry_limit=None, prompt_variant=None, jobs=1,
            dry_run=False, json=False, verbose=False,
            group=None, replicate=None, replicates=10, seed=None,
            runs_dir=None, tasks_dir="tasks", models_dir="models",
            daily_cap=0.0, max_cost=0.05,
        )
        with tempfile.TemporaryDirectory() as tmp:
            args.runs_dir = tmp
            store = RunStore(tmp)
            # seed one finished run so mean_run_cost = $0.01; 10x$0.01 > $0.05
            from orchestral.storage import RunMeta
            store.index_meta(RunMeta(
                run_id="seed", orchestrator="o/m", task_id="t", worker="w/m",
                status="finished", started_at="2020-01-01T00:00:00",
                total_cost_usd=0.01,
            ))
            err = io.StringIO()
            with (patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}),
                  redirect_stderr(err), redirect_stdout(io.StringIO()),
                  self.assertRaises(SystemExit)):
                harness.cmd_run(args)
            self.assertIn("exceeds --max-cost", err.getvalue())
            self.assertEqual(len(store.list_runs(limit=None)), 1)  # nothing launched


if __name__ == "__main__":
    unittest.main()
