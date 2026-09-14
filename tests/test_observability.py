"""Observability v2: taxonomy, debug channel, calls table, metrics, labels."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from orchestral.config import ModelConfig, TaskSpec
from orchestral.fileset import FilesetError
from orchestral.logger import EventLogger
from orchestral.metrics import build_metrics
from orchestral.openrouter import (
    OpenRouterVideoJobError,
    OpenRouterVideoSubmittedError,
    ProviderConfigError,
)
from orchestral.runner import Runner, ValidationError
from orchestral.storage import RunStore
from orchestral.taxonomy import CATEGORIES, classify_exception


class TestTaxonomy(unittest.TestCase):
    def _status_error(self, code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://api.example.test/x")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError("err", request=req, response=resp)

    def test_submitted_job_wins_over_wrapped_transport(self):
        self.assertEqual(classify_exception(OpenRouterVideoSubmittedError("x")), "submitted_job")
        self.assertEqual(classify_exception(OpenRouterVideoJobError("x", status="failed")), "submitted_job")

    def test_http_status_categories(self):
        self.assertEqual(classify_exception(self._status_error(429)), "rate_limit")
        self.assertEqual(classify_exception(self._status_error(401)), "auth")
        self.assertEqual(classify_exception(self._status_error(403)), "auth")
        self.assertEqual(classify_exception(self._status_error(500)), "provider_error")
        self.assertEqual(classify_exception(self._status_error(400)), "provider_error")

    def test_timeout_and_transport(self):
        self.assertEqual(classify_exception(httpx.ReadTimeout("t")), "timeout")
        self.assertEqual(classify_exception(TimeoutError("t")), "timeout")
        self.assertEqual(classify_exception(httpx.ProxyError("p")), "transport")

    def test_validation_vs_empty_output(self):
        self.assertEqual(classify_exception(ValidationError("Worker x produced no output")), "empty_output")
        self.assertEqual(classify_exception(ValidationError("check has_title failed")), "validation")

    def test_malformed_and_config(self):
        self.assertEqual(classify_exception(FilesetError("bad")), "malformed_output")
        self.assertEqual(classify_exception(json.JSONDecodeError("m", "d", 0)), "malformed_output")
        self.assertEqual(classify_exception(ProviderConfigError("no env")), "config")
        self.assertEqual(classify_exception(KeyError("k")), "config")
        self.assertEqual(classify_exception(RuntimeError("?")), "unknown")

    def test_every_category_reachable(self):
        self.assertGreaterEqual(len(set(CATEGORIES)), 10)

    def test_wrapped_httpx_via_cause_chain(self):
        """OpenRouterError re-raises httpx failures with `from` — the real
        cause is on __cause__, and classification must follow it."""
        from orchestral.openrouter import OpenRouterError

        try:
            raise self._status_error(429)
        except httpx.HTTPStatusError as inner:
            wrapped = OpenRouterError("OpenRouter request failed after retries")
            wrapped.__cause__ = inner
        self.assertEqual(classify_exception(wrapped), "rate_limit")

        try:
            raise httpx.ReadTimeout("slow")
        except httpx.ReadTimeout as inner2:
            wrapped2 = OpenRouterError("request failed")
            wrapped2.__cause__ = inner2
        self.assertEqual(classify_exception(wrapped2), "timeout")

        # OpenRouterError with no httpx cause is still an API failure
        self.assertEqual(classify_exception(OpenRouterError("bad response")), "provider_error")


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


class TestDebugChannel(unittest.TestCase):
    def test_debug_records_are_separate_and_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(tmp)
            rec = logger.log_debug("openrouter", "video_poll", job_id="j1", status="processing")
            logger.close()
            lines = (Path(tmp) / "debug.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            parsed = json.loads(lines[0])
            self.assertEqual(parsed["seq"], 1)
            self.assertEqual(parsed["component"], "openrouter")
            self.assertEqual(parsed["fields"]["job_id"], "j1")
            self.assertEqual(rec["seq"], 1)
            # debug records never enter the semantic stream
            self.assertFalse((Path(tmp) / "events.jsonl").read_text().strip())

    def test_verbose_echoes_events_and_debug(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stderr(buf):
                logger = EventLogger(tmp, verbose=True)
                logger.log_llm_call(
                    phase="delegate", step=1, model="m/x", role="worker",
                    messages=[], completion={}, cost_usd=0.01, latency_ms=5,
                )
                logger.log_debug("openrouter", "http_retry", attempt=1)
                logger.close()
            err = buf.getvalue()
            self.assertIn("[delegate/llm_call]", err)
            self.assertIn("m/x", err)
            self.assertIn("dbg[openrouter] http_retry", err)
            self.assertIn("attempt=1", err)


class TestCallsTable(unittest.TestCase):
    def test_llm_calls_and_errors_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            logger = EventLogger(tmp, store=store, run_id="r1", dry_run=True)
            logger.log_llm_call(
                phase="plan", step=1, model="o/m", role="orchestrator",
                messages=[], completion={}, input_tokens=10, output_tokens=5,
                cost_usd=0.002, latency_ms=42.0, pricing_source="configured",
                api_cost_usd=0.003,
            )
            logger.log(
                phase="delegate", step=2, event_type="worker_error",
                model="w/m", role="worker", input_data={}, output_data={"attempt": 2},
                error="boom", metadata={"error_category": "timeout"},
            )
            # non-call events are not indexed
            logger.log(phase="init", step=0, event_type="run.started", model="",
                       role="harness", input_data={}, output_data={})
            logger.close()

            calls = store.calls_for_run("r1")
            self.assertEqual(len(calls), 2)
            llm = calls[0]
            self.assertEqual(llm["pricing_source"], "configured")
            self.assertEqual(llm["api_cost_usd"], 0.003)
            self.assertEqual(llm["latency_ms"], 42.0)
            self.assertEqual(llm["dry_run"], 1)
            err = calls[1]
            self.assertEqual(err["error_category"], "timeout")
            self.assertEqual(err["attempt"], 2)
            self.assertEqual(err["error"], "boom")

    def test_old_db_migrates(self):
        """A 14-column pre-v2 index gains the new columns without data loss."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY, orchestrator TEXT, task_id TEXT,
                    worker TEXT, status TEXT, started_at TEXT, finished_at TEXT,
                    total_cost_usd REAL, total_input_tokens INTEGER,
                    total_output_tokens INTEGER, score REAL, passes INTEGER,
                    run_dir TEXT, config TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO runs VALUES ('old1','o','t','w','finished','x','y',1.0,1,1,9.0,1,'d','{}')"
            )
            conn.commit()
            conn.close()

            store = RunStore(tmp)  # migration runs here
            meta = store.get_run("old1")
            self.assertIsNotNone(meta)
            self.assertEqual(meta.score, 9.0)
            self.assertIsNone(meta.failure_reason)
            self.assertEqual(meta.latency_ms, 0.0)
            self.assertEqual(meta.env, {})

            cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(runs)")}
            self.assertTrue({"latency_ms", "failure_reason", "env", "run_group", "replicate"} <= cols)
            # calls table exists on migrated DBs too
            store.record_call(run_id="old1", phase="plan", step=1, role="orchestrator", model="m")
            self.assertEqual(len(store.calls_for_run("old1")), 1)


class TestMetrics(unittest.TestCase):
    def test_aggregates_by_phase_and_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            events = [
                {"type": "llm_call", "phase": "plan", "role": "orchestrator",
                 "cost": {"input_tokens": 100, "output_tokens": 50, "usd": 0.01, "pricing_source": "configured"},
                 "latency_ms": 100.0, "metadata": {}},
                {"type": "llm_call", "phase": "delegate", "role": "worker",
                 "cost": {"input_tokens": 10, "output_tokens": 5, "usd": 0.001},
                 "latency_ms": 50.0, "metadata": {}},
                {"type": "llm_call", "phase": "delegate", "role": "worker",
                 "cost": {"input_tokens": 10, "output_tokens": 5, "usd": 0.001},
                 "latency_ms": 150.0, "metadata": {}},
                {"type": "worker_error", "metadata": {"error_category": "timeout"}},
                {"type": "worker_retry", "metadata": {}},
                {"type": "run_failed", "metadata": {"error_category": "empty_output"}},
            ]
            p.write_text("\n".join(json.dumps(e) for e in events))
            m = build_metrics(p)
            self.assertEqual(m["events"], 6)
            self.assertEqual(m["phases"]["delegate"]["worker"]["calls"], 2)
            self.assertEqual(m["phases"]["delegate"]["worker"]["latency_ms"]["max"], 150.0)
            self.assertEqual(m["phases"]["plan"]["orchestrator"]["cost_usd"], 0.01)
            # error_counts counts worker_error events only — run_failed is the
            # run-level label (runs.failure_reason), counting it doubles up
            self.assertEqual(m["error_counts"], {"timeout": 1})
            self.assertEqual(m["retries"], 1)
            self.assertEqual(m["pricing_sources"]["configured"], 1)
            self.assertEqual(m["pricing_sources"]["unlabeled"], 2)

    def test_tolerates_odd_field_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text("\n".join([
                json.dumps({"type": "llm_call", "metadata": "oops", "cost": "oops", "latency_ms": "x"}),
                "not json",
                json.dumps(["a", "list"]),
                json.dumps({"type": "llm_call", "phase": "plan", "role": "orchestrator",
                            "cost": {"usd": 0.5}, "latency_ms": 10.0}),
            ]))
            m = build_metrics(p)
            self.assertEqual(m["events"], 2)  # unparseable + non-dict lines skipped
            self.assertEqual(m["phases"]["plan"]["orchestrator"]["cost_usd"], 0.5)


class TestRunnerObservability(unittest.TestCase):
    def test_run_labels_and_metrics_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store, run_group="g1", replicate=3, seed=42,
                clients={"orchestrator": _chat_client(
                    json.dumps({"subtasks": [{"id": 0, "description": "x"}]}))},
            )
            worker = _chat_client("<html><head><title>T</title></head><body>ok</body></html>")
            runner._injected_clients["worker"] = worker
            # assemble also uses the orchestrator client; give it 2 responses
            runner._injected_clients["orchestrator"].chat.side_effect = [
                {"content": json.dumps({"subtasks": [{"id": 0, "description": "x"}]}),
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "latency_ms": 1, "id": "p"},
                {"content": "<html><head><title>T</title></head><body>ok</body></html>",
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "latency_ms": 1, "id": "a"},
            ]
            meta = runner.run(
                TaskSpec(id="t1", type="html", prompt="p"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            self.assertEqual(meta.status, "finished")
            self.assertEqual(meta.run_group, "g1")
            self.assertEqual(meta.replicate, 3)
            self.assertGreaterEqual(meta.latency_ms, 0)
            self.assertIn("git_sha", meta.env)
            self.assertIn("python", meta.env)
            self.assertIsNone(meta.failure_reason)

            run_dir = Path(meta.run_dir)
            self.assertTrue((run_dir / "metrics.json").exists())
            self.assertTrue((run_dir / "debug.jsonl").exists())
            metrics = json.loads((run_dir / "metrics.json").read_text())
            self.assertGreater(metrics["phases"]["plan"]["orchestrator"]["calls"], 0)

            # calls table has the three llm_calls
            calls = store.calls_for_run(meta.run_id)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(c["pricing_source"] == "configured" for c in calls))

            # run.json + run.started carry the labels
            run_json = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(run_json["config"]["seed"], 42)
            events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
            started = next(e for e in events if e["type"] == "run.started")
            self.assertIn("env", started["output"])

    def test_failed_run_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            orch = MagicMock()
            orch.chat.side_effect = httpx.ReadTimeout("slow")
            runner = Runner(
                runs_dir=tmp, store=store,
                clients={"orchestrator": orch, "worker": _chat_client("")},
            )
            with self.assertRaises(httpx.ReadTimeout):
                runner.run(
                    TaskSpec(id="t1", type="html", prompt="p"),
                    _model("o/m", "orchestrator"), _model("w/m", "worker"),
                )
            runs = store.list_runs()
            self.assertEqual(runs[0].status, "failed")
            self.assertEqual(runs[0].failure_reason, "exception:timeout")
            run_dir = Path(runs[0].run_dir)
            # metrics.json still written on failure; the run.failed event is
            # the last line so it lands in the aggregate
            self.assertTrue((run_dir / "metrics.json").exists())
            metrics = json.loads((run_dir / "metrics.json").read_text())
            self.assertGreaterEqual(metrics["events"], 2)
            last = json.loads((run_dir / "events.jsonl").read_text().splitlines()[-1])
            self.assertEqual(last["type"], "run.failed")
            self.assertEqual(last["metadata"]["error_category"], "timeout")


class TestScrub(unittest.TestCase):
    def test_debug_omitted_metrics_published(self):
        from orchestral.privacy import scrub_all

        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            run_dir = runs_dir / "o" / "t" / "w" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps({"run_id": "r1", "status": "finished"}))
            (run_dir / "events.jsonl").write_text('{"type": "run.started"}\n')
            (run_dir / "metrics.json").write_text('{"events": 1}')
            (run_dir / "debug.jsonl").write_text('{"component": "openrouter"}\n')
            (run_dir / "raw").mkdir()
            out = Path(tmp) / "pub"
            scrub_all(runs_dir, out)
            manifest = json.loads((out / "manifest.json").read_text())
            pub_run = out / "o" / "t" / "w" / "r1"
            self.assertTrue((pub_run / "metrics.json").exists())
            self.assertTrue((pub_run / "events.jsonl").exists())
            self.assertFalse((pub_run / "debug.jsonl").exists())
            self.assertFalse((pub_run / "raw").exists())
            omissions = json.dumps(manifest, default=str)
            self.assertIn("debug.jsonl", omissions)
            self.assertIn("raw/", omissions)


if __name__ == "__main__":
    unittest.main()
