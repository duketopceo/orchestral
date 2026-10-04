"""U9: liveness (KTD8). Live rows are index `running` rows; liveness comes from
the last event time and registry ownership. `stalled` is derived, never written
to the index. Marking an orphan abandoned writes an `aborted` run annotation."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from orchestral.storage import RunMeta, RunStore
from orchestral.tui.state import Job, JobStatus
from orchestral.web import state

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus)

CLOCK = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


def _running_run(store: RunStore, run_id: str, *, last_event_ago_s: float | None,
                 status: str = "running", now: datetime = CLOCK) -> RunMeta:
    run_dir = Path(store.root) / run_id
    run_dir.mkdir(parents=True)
    started = now - timedelta(hours=1)
    if last_event_ago_s is not None:
        ts = (now - timedelta(seconds=last_event_ago_s)).isoformat()
        events = [
            {"type": "run.started", "timestamp": started.isoformat()},
            {"type": "llm_call", "timestamp": ts, "cost": {"usd": 0.02, "api_cost_usd": 0.03}},
        ]
        (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    meta = RunMeta(
        run_id=run_id, orchestrator="o/model", task_id="t-task", worker="w/model",
        status=status, started_at=started.isoformat(), run_dir=str(run_dir))
    store.index_meta(meta)
    return meta


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RunStore(self.tmp)
        self.registry = state.JobRegistry(Path(self.tmp), Path(self.tmp) / "tasks",
                                          Path(self.tmp) / "models", self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        return {r["run_id"]: r for r in state.live_runs(self.store, self.registry, now=CLOCK)}


class TestThreshold(unittest.TestCase):
    def test_named_constant_is_ten_minutes(self):
        self.assertEqual(state.STALL_AFTER_S, 600)

    def test_helper_boundaries(self):
        self.assertEqual(state.liveness_state(0), "live")
        self.assertEqual(state.liveness_state(state.STALL_AFTER_S), "live")
        self.assertEqual(state.liveness_state(state.STALL_AFTER_S + 1), "stalled")
        self.assertEqual(state.liveness_state(None), "stalled")


class TestLiveRuns(_Base):
    def test_recent_event_and_no_owner_is_live_and_unowned(self):
        _running_run(self.store, "r-recent", last_event_ago_s=120)
        row = self.rows()["r-recent"]
        self.assertEqual(row["state"], "live")
        self.assertFalse(row["stalled"])
        self.assertFalse(row["owned"])
        self.assertFalse(row["cancellable"])
        self.assertFalse(row["abandonable"])
        self.assertEqual(row["idle_s"], 120)

    def test_old_event_is_stalled(self):
        _running_run(self.store, "r-old", last_event_ago_s=3600)
        row = self.rows()["r-old"]
        self.assertTrue(row["stalled"])
        self.assertEqual(row["state"], "stalled")
        self.assertTrue(row["abandonable"])

    def test_missing_events_falls_back_to_file_mtime_then_started_at(self):
        _running_run(self.store, "r-bare", last_event_ago_s=None)
        # no events file at all: heartbeat is the run's started_at, an hour ago
        self.assertTrue(self.rows()["r-bare"]["stalled"])
        run_dir = Path(self.tmp) / "r-bare"
        (run_dir / "events.jsonl").write_text("not json\n")
        os.utime(run_dir / "events.jsonl", (CLOCK.timestamp() - 30, CLOCK.timestamp() - 30))
        self.assertFalse(self.rows()["r-bare"]["stalled"])

    def test_finished_runs_are_not_live(self):
        _running_run(self.store, "r-done", last_event_ago_s=5, status="finished")
        self.assertEqual(self.rows(), {})

    def test_row_carries_phase_elapsed_and_spend(self):
        _running_run(self.store, "r-spend", last_event_ago_s=5)
        row = self.rows()["r-spend"]
        self.assertEqual(row["phase"], "starting")
        self.assertEqual(row["elapsed_s"], 3600)
        self.assertAlmostEqual(row["spend_usd"], 0.03)

    def test_registry_owned_job_appears_once(self):
        _running_run(self.store, "r-owned", last_event_ago_s=5)
        job = Job(label="t-task·model")
        job.run_ids.append("r-owned")
        self.registry.jobs.append(job)
        rows = state.live_runs(self.store, self.registry, now=CLOCK)
        self.assertEqual([r["run_id"] for r in rows], ["r-owned"])
        self.assertTrue(rows[0]["owned"])
        self.assertTrue(rows[0]["cancellable"])
        self.assertFalse(rows[0]["abandonable"])

    def test_owned_job_with_no_run_yet_still_shows(self):
        job = Job(label="starting-up")
        self.registry.jobs.append(job)
        rows = state.live_runs(self.store, self.registry, now=CLOCK)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["run_id"])
        self.assertTrue(rows[0]["owned"])

    def test_finished_job_is_not_listed(self):
        job = Job(label="old")
        job.status = JobStatus.SUCCEEDED
        self.registry.jobs.append(job)
        self.assertEqual(state.live_runs(self.store, self.registry, now=CLOCK), [])

    def test_overview_jobs_come_from_live_runs(self):
        _running_run(self.store, "r-a", last_event_ago_s=5)
        _running_run(self.store, "r-b", last_event_ago_s=7200)
        with patch.object(state, "_now", return_value=CLOCK):
            jobs = state.overview_payload(self.store, self.registry)["jobs"]
        self.assertEqual({j["run_id"] for j in jobs}, {"r-a", "r-b"})
        by = {j["run_id"]: j for j in jobs}
        self.assertTrue(by["r-b"]["stalled"])
        for key in ("label", "status", "detail", "run_ids", "cancellable", "owned", "state"):
            self.assertIn(key, by["r-a"])

    def test_stalled_is_never_written_to_the_index(self):
        _running_run(self.store, "r-old", last_event_ago_s=3600)
        self.rows()
        self.assertEqual(self.store.get_run("r-old").status, "running")


class TestAbandon(_Base):
    def test_abandon_retires_stalled_unowned_and_keeps_status(self):
        _running_run(self.store, "r-old", last_event_ago_s=3600)
        out = state.abandon_run(self.store, self.registry, "r-old", now=CLOCK)
        self.assertTrue(out["abandoned"])
        self.assertNotIn("r-old", self.rows())
        self.assertEqual(self.store.get_run("r-old").status, "running")
        ann = [a for a in self.store.annotations() if a["kind"] == "run" and a["target"] == "r-old"]
        self.assertEqual([a["flag"] for a in ann], ["aborted"])

    def test_abandon_is_idempotent(self):
        _running_run(self.store, "r-old", last_event_ago_s=3600)
        state.abandon_run(self.store, self.registry, "r-old", now=CLOCK)
        again = state.abandon_run(self.store, self.registry, "r-old", now=CLOCK)
        self.assertTrue(again["abandoned"])
        self.assertTrue(again["already"])
        self.assertEqual(len([a for a in self.store.annotations() if a["target"] == "r-old"]), 1)

    def test_abandon_refuses_a_recent_event(self):
        _running_run(self.store, "r-live", last_event_ago_s=30)
        with self.assertRaises(state.LivenessRefusal) as cm:
            state.abandon_run(self.store, self.registry, "r-live", now=CLOCK)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("recent", str(cm.exception))
        self.assertIn("r-live", self.rows())
        self.assertEqual(self.store.annotations(), [])

    def test_abandon_never_touches_finished_runs(self):
        _running_run(self.store, "r-done", last_event_ago_s=99999, status="finished")
        with self.assertRaises(state.LivenessRefusal) as cm:
            state.abandon_run(self.store, self.registry, "r-done", now=CLOCK)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.store.annotations(), [])

    def test_abandon_refuses_an_owned_run(self):
        _running_run(self.store, "r-owned", last_event_ago_s=99999)
        job = Job(label="x")
        job.run_ids.append("r-owned")
        self.registry.jobs.append(job)
        with self.assertRaises(state.LivenessRefusal) as cm:
            state.abandon_run(self.store, self.registry, "r-owned", now=CLOCK)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.store.annotations(), [])

    def test_abandon_unknown_run_is_404(self):
        with self.assertRaises(state.LivenessRefusal) as cm:
            state.abandon_run(self.store, self.registry, "ghost", now=CLOCK)
        self.assertEqual(cm.exception.status, 404)


class TestCorpusOrphan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.root, "full")
        cls.store = RunStore(cls.root)
        cls.registry = state.JobRegistry(
            cls.root, cls.tmp / "tasks", cls.tmp / "models", cls.store)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_corpus_orphan_is_stalled_and_unowned(self):
        rid = self.manifest["orphan_run_id"]
        now = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
        rows = state.live_runs(self.store, self.registry, now=now)
        self.assertEqual([r["run_id"] for r in rows], [rid])
        self.assertTrue(rows[0]["stalled"])
        self.assertFalse(rows[0]["owned"])
        self.assertGreater(rows[0]["idle_s"], 24 * 3600)

    def test_abandoning_the_corpus_orphan_empties_live_runs(self):
        # own copy: abandon writes an annotation
        copy = self.tmp / "copy"
        shutil.copytree(self.root, copy)
        store = RunStore(copy)
        reg = state.JobRegistry(copy, self.tmp / "tasks", self.tmp / "models", store)
        rid = self.manifest["orphan_run_id"]
        before = store.get_run(rid)
        assert before is not None
        state.abandon_run(store, reg, rid)
        self.assertEqual(state.live_runs(store, reg), [])
        after = store.get_run(rid)
        assert after is not None
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.total_cost_usd, before.total_cost_usd)


if __name__ == "__main__":
    unittest.main()
