"""U12: the run detail payload. A failure summary, per-tab evidence states, lane
timeline data and liveness actions, all derived from the run directory.

Key-free: runs are written by hand into a temp store."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orchestral.storage import RunMeta, RunStore
from orchestral.tui.state import Job
from orchestral.web import state

CLOCK = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


def _ev(seq: int, typ: str, phase: str, offset_s: float, *, error: str | None = None,
        latency_ms: float = 0.0, role: str = "harness", worker_id: str | None = None,
        cost: dict | None = None) -> dict:
    return {"sequence": seq, "type": typ, "phase": phase, "role": role, "worker_id": worker_id,
            "timestamp": (CLOCK + timedelta(seconds=offset_s)).isoformat(), "latency_ms": latency_ms,
            "error": error, "input": {}, "output": {}, "cost": cost or {}, "model": "m"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RunStore(self.tmp)
        self.registry = state.JobRegistry(Path(self.tmp), Path(self.tmp) / "t", Path(self.tmp) / "m",
                                          self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, run_id: str, *, status: str = "finished", events: list[dict] | None = None,
             report: dict | None = None, files: dict[str, str] | None = None, dry: bool = False,
             holdout: bool = False, failure: str | None = None, started: datetime = CLOCK,
             calls: int = 0) -> Path:
        d = Path(self.tmp) / run_id
        d.mkdir(parents=True)
        if events is not None:
            (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        if report is not None:
            (d / "report.json").write_text(json.dumps(report))
        for name, text in (files or {}).items():
            (d / name).write_text(text)
        self.store.index_meta(RunMeta(
            run_id=run_id, orchestrator="o/m", task_id="t", worker="w/m", status=status,
            started_at=started.isoformat(), run_dir=str(d), dry_run=dry, failure_reason=failure,
            config={"holdout": holdout, "dry_run": dry}, passes=False if status != "running" else None))
        return d

    def detail(self, run_id: str, **kw):
        return state.run_detail_payload(self.store, run_id, **kw)


class TestFailureSummary(_Base):
    def test_failing_check_and_first_error_event(self):
        self.make("f1", status="failed", failure="validation",
                  events=[_ev(1, "run.started", "init", 0), _ev(2, "llm_call", "plan", 1),
                          _ev(3, "worker.failed", "delegate", 2, error="boom"),
                          _ev(4, "run.completed", "end", 3)],
                  report={"checks": {"non_empty": True, "has_cta": False}, "errors": ["no cta"]})
        f = self.detail("f1")["failure"]
        self.assertEqual(f["reason"], "validation")
        self.assertEqual(f["failing_check"], "has_cta")
        self.assertEqual(f["event"]["index"], 2)
        self.assertEqual(f["event"]["kind"], "error")
        self.assertEqual(f["event"]["type"], "worker.failed")
        self.assertFalse(f["no_detail"])

    def test_failed_without_error_event_points_at_the_last_event(self):
        self.make("f2", status="failed", failure="worker_timeout",
                  events=[_ev(1, "run.started", "init", 0), _ev(2, "run.completed", "end", 5)],
                  report={"checks": {"non_empty": False}, "errors": ["worker_timeout"]})
        f = self.detail("f2")["failure"]
        self.assertEqual(f["failing_check"], "non_empty")
        self.assertEqual(f["event"], {**f["event"], "index": 1, "kind": "last", "type": "run.completed"})

    def test_failed_before_the_first_event_says_there_is_no_detail(self):
        self.make("f3", status="failed", failure="config", events=[])
        f = self.detail("f3")["failure"]
        self.assertTrue(f["no_detail"])
        self.assertIsNone(f["event"])
        self.assertFalse(f["report_available"])

    def test_no_events_but_a_report_links_the_raw_report(self):
        self.make("f4", status="failed", failure="config", report={"errors": ["bad model"]})
        f = self.detail("f4")["failure"]
        self.assertTrue(f["no_detail"])
        self.assertTrue(f["report_available"])
        self.assertEqual(f["errors"], ["bad model"])

    def test_passing_and_running_runs_have_no_failure_block(self):
        self.make("ok", events=[_ev(1, "run.started", "init", 0)])
        self.make("go", status="running", events=[_ev(1, "run.started", "init", 0)])
        self.assertIsNone(self.detail("ok")["failure"])
        self.assertIsNone(self.detail("go")["failure"])


class TestSections(_Base):
    def sec(self, run_id: str, **kw):
        return self.detail(run_id, **kw)["sections"]

    def test_running_run_says_not_yet(self):
        self.make("r", status="running", events=[_ev(1, "run.started", "init", 0)])
        s = self.sec("r", now=CLOCK)
        for tab in ("artifact", "report", "plan", "manifest", "review", "calls"):
            self.assertEqual(s[tab]["state"], "not_yet", tab)
        self.assertEqual(s["events"]["state"], "ok")
        self.assertEqual(s["events"]["count"], 1)

    def test_stalled_run_is_missing_not_not_yet(self):
        self.make("st", status="running", started=CLOCK - timedelta(days=2),
                  events=[_ev(1, "run.started", "init", -2 * 86400)])
        s = self.sec("st", registry=self.registry, now=CLOCK)
        for tab in ("artifact", "report", "plan", "manifest", "review", "calls"):
            self.assertEqual(s[tab]["state"], "missing", tab)
            self.assertIn("stopped reporting", s[tab]["reason"])

    def test_dry_run_without_calls_is_empty_by_design(self):
        self.make("d", dry=True, events=[_ev(1, "run.started", "init", 0)])
        s = self.sec("d")
        self.assertEqual(s["calls"]["state"], "empty_by_design")
        self.assertEqual(s["artifact"]["state"], "empty_by_design")
        self.assertIn("dry", s["calls"]["reason"].lower())

    def test_finished_run_without_files_is_missing_with_a_reason(self):
        self.make("m", events=[_ev(1, "run.started", "init", 0)])
        s = self.sec("m")
        self.assertEqual(s["artifact"]["state"], "missing")
        self.assertTrue(s["artifact"]["reason"])
        self.assertEqual(s["report"]["state"], "missing")
        self.assertEqual(s["plan"]["state"], "missing")

    def test_present_files_are_ok_with_counts(self):
        self.make("p", events=[_ev(1, "run.started", "init", 0)], report={"checks": {}},
                  files={"artifact.html": "<p>x</p>", "manifest.json": "{}",
                         "plan.json": json.dumps({"plan": "p", "subtasks": [{"id": 1, "title": "a"}]})})
        s = self.sec("p")
        for tab in ("artifact", "report", "manifest", "plan"):
            self.assertEqual(s[tab]["state"], "ok", tab)
        self.assertEqual(s["plan"]["count"], 1)

    def test_plan_json_is_exposed_as_a_subtask_list(self):
        self.make("pj", events=[], files={"plan.json": json.dumps(
            {"plan": "Do it", "subtasks": [{"id": 1, "title": "Skeleton"}, {"id": 2, "title": "CSS"}]})})
        d = self.detail("pj")
        self.assertEqual([t["title"] for t in d["plan_json"]["subtasks"]], ["Skeleton", "CSS"])
        self.assertEqual(d["plan_json"]["plan"], "Do it")

    def test_corrupt_report_is_missing_with_that_reason_not_a_crash(self):
        self.make("bad", events=[], files={"report.json": "{not json"})
        s = self.sec("bad")
        self.assertEqual(s["report"]["state"], "missing")
        self.assertIn("unreadable", s["report"]["reason"].lower())

    def test_calls_count_and_events_total(self):
        self.make("c", events=[_ev(1, "run.started", "init", 0), _ev(2, "llm_call", "plan", 1)])
        d = self.detail("c")
        self.assertEqual(d["sections"]["events"]["count"], 2)
        self.assertEqual(d["sections"]["calls"]["count"], len(d["calls"]))

    def test_relative_run_dir_resolves_against_the_store_root(self):
        """DESIGN.md question 8: `ca10bd3f61e4` was indexed with a run_dir relative to the
        working directory it ran in. Read from another directory (or a copied runs dir) its
        artifact and events came back empty. The data was there; the path did not resolve."""
        d = Path(self.tmp) / "o" / "t" / "w" / "rel"
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text(json.dumps(_ev(1, "run.started", "init", 0)) + "\n")
        (d / "artifact.html").write_text("<p>x</p>")
        self.store.index_meta(RunMeta(
            run_id="rel", orchestrator="o/m", task_id="t", worker="w/m", status="finished",
            started_at=CLOCK.isoformat(), run_dir="gone-runs/o/t/w/rel"))
        out = self.detail("rel")
        self.assertIsNotNone(out["artifact"])
        self.assertEqual(out["sections"]["events"]["state"], "ok")


class TestHosted(_Base):
    def test_holdout_run_is_withheld_whole_on_hosted(self):
        self.make("h", holdout=True, events=[_ev(1, "run.started", "init", 0)],
                  report={"checks": {"a": True}}, files={"artifact.html": "<p>k</p>", "plan.json": "{}"})
        d = self.detail("h", hosted=True)
        for tab in ("artifact", "events", "calls", "report", "review", "plan", "manifest"):
            self.assertEqual(d["sections"][tab]["state"], "withheld", tab)
        self.assertIn("holdout", d["sections"]["artifact"]["reason"].lower())
        self.assertIsNone(d["artifact"])
        self.assertIsNone(d["report"])
        self.assertEqual(d["calls"], [])
        self.assertEqual(d["timeline"], [])
        self.assertEqual(d["lanes"]["lanes"], [])

    def test_holdout_is_shown_in_full_locally(self):
        self.make("h2", holdout=True, events=[_ev(1, "run.started", "init", 0)],
                  files={"artifact.html": "<p>k</p>"})
        d = self.detail("h2")
        self.assertEqual(d["sections"]["artifact"]["state"], "ok")
        self.assertTrue(d["holdout"])

    def test_hosted_plan_md_and_zip_are_withheld(self):
        d = self.make("z", events=[], files={"plan.md": "# plan"})
        with zipfile.ZipFile(d / "artifact.zip", "w") as zf:
            zf.writestr("index.html", "<p>x</p>")
        s = self.detail("z", hosted=True)["sections"]
        self.assertEqual(s["plan"]["state"], "withheld")
        self.assertEqual(s["artifact"]["state"], "withheld")
        self.assertIn("archive", s["artifact"]["reason"].lower())
        local = self.detail("z")["sections"]
        self.assertEqual(local["plan"]["state"], "ok")
        self.assertEqual(local["artifact"]["state"], "ok")


class TestSnapshot(_Base):
    def test_snapshot_keeps_a_withheld_stub_for_a_holdout_run(self):
        from orchestral.web import snapshot
        self.make("hs", holdout=True, events=[_ev(1, "run.started", "init", 0)],
                  report={"checks": {"a": True}},
                  files={"artifact.html": "<p>SECRET-ANSWER</p>", "manifest.json": json.dumps({"holdout": True})})
        out = snapshot.run_payloads(self.store, "hs", Path(self.tmp) / "tasks")
        self.assertEqual(list(out), ["run/hs.json"])
        stub = out["run/hs.json"]
        self.assertEqual(stub["sections"]["artifact"]["state"], "withheld")
        self.assertNotIn("SECRET-ANSWER", json.dumps(stub))


class TestLanes(_Base):
    def test_lanes_group_calls_by_orchestrator_worker_assemble_judge(self):
        evs = [_ev(1, "run.started", "init", 0),
               _ev(2, "llm_call", "plan", 10, latency_ms=8000, role="orchestrator", cost={"usd": 0.01}),
               _ev(3, "worker.started", "delegate", 10, role="worker", worker_id="worker-0"),
               _ev(4, "llm_call", "delegate", 30, latency_ms=15000, role="worker", cost={"usd": 0.02}),
               _ev(5, "worker.completed", "delegate", 30, role="worker", worker_id="worker-0"),
               _ev(6, "llm_call", "assemble", 40, latency_ms=5000, role="orchestrator"),
               _ev(7, "llm_call", "judge", 41, latency_ms=500, role="judge"),
               _ev(8, "run.completed", "end", 42)]
        self.make("l", events=evs)
        lanes = self.detail("l")["lanes"]
        self.assertEqual([x["id"] for x in lanes["lanes"]],
                         ["orchestrator", "worker-0", "assemble", "judge"])
        self.assertAlmostEqual(lanes["span_ms"], 42000)
        orch = lanes["lanes"][0]["bars"][0]
        self.assertAlmostEqual(orch["start_ms"], 2000)
        self.assertAlmostEqual(orch["dur_ms"], 8000)
        self.assertEqual(orch["event"], 1)
        self.assertEqual(orch["verdict"], "ok")

    def test_error_event_bar_carries_the_fail_verdict(self):
        evs = [_ev(1, "run.started", "init", 0),
               _ev(2, "llm_call", "plan", 5, latency_ms=1000, role="orchestrator", error="429")]
        self.make("le", status="failed", events=evs)
        bar = self.detail("le")["lanes"]["lanes"][0]["bars"][0]
        self.assertEqual(bar["verdict"], "fail")

    def test_stalled_run_has_no_live_lane(self):
        evs = [_ev(1, "run.started", "init", -7200), _ev(2, "llm_call", "plan", -7190, latency_ms=100,
                                                            role="orchestrator")]
        self.make("ls", status="running", started=CLOCK - timedelta(hours=2), events=evs)
        d = self.detail("ls", registry=self.registry, now=CLOCK)
        self.assertEqual(d["liveness"]["state"], "stalled")
        self.assertIsNone(d["lanes"]["live"])

    def test_no_events_means_no_lanes(self):
        self.make("e", events=[])
        self.assertEqual(self.detail("e")["lanes"]["lanes"], [])


class TestLiveness(_Base):
    def test_stalled_orphan_is_abandonable_not_cancellable(self):
        self.make("o", status="running", started=CLOCK - timedelta(days=2),
                  events=[{**_ev(1, "run.started", "init", -2 * 86400)}])
        lv = self.detail("o", registry=self.registry, now=CLOCK)["liveness"]
        self.assertEqual(lv["state"], "stalled")
        self.assertFalse(lv["owned"])
        self.assertFalse(lv["cancellable"])
        self.assertTrue(lv["abandonable"])

    def test_owned_job_is_cancellable(self):
        self.make("w", status="running", events=[_ev(1, "run.started", "init", -5)])
        job = Job(label="x")
        job.run_ids = ["w"]
        self.registry.jobs.append(job)
        lv = self.detail("w", registry=self.registry, now=CLOCK)["liveness"]
        self.assertEqual(lv["state"], "live")
        self.assertTrue(lv["owned"])
        self.assertTrue(lv["cancellable"])
        self.assertFalse(lv["abandonable"])

    def test_unowned_recent_run_is_neither(self):
        self.make("u", status="running", events=[_ev(1, "run.started", "init", -5)])
        lv = self.detail("u", registry=self.registry, now=CLOCK)["liveness"]
        self.assertEqual((lv["state"], lv["cancellable"], lv["abandonable"]), ("live", False, False))

    def test_abandoned_run_reports_abandoned(self):
        self.make("a", status="running", started=CLOCK - timedelta(days=2),
                  events=[_ev(1, "run.started", "init", -2 * 86400)])
        state.abandon_run(self.store, self.registry, "a", now=CLOCK)
        lv = self.detail("a", registry=self.registry, now=CLOCK)["liveness"]
        self.assertEqual(lv["state"], "abandoned")
        self.assertFalse(lv["abandonable"])

    def test_finished_run_is_done(self):
        self.make("fin", events=[])
        self.assertEqual(self.detail("fin", registry=self.registry)["liveness"]["state"], "done")


if __name__ == "__main__":
    unittest.main()
