"""outcome_state: the single honest "what happened" axis for a run.

Sibling of judge_state — outcome answers "what kind of result", judge_state
answers "did the judge produce a verdict". Zero network; RunMeta is a plain
dataclass so fixtures are direct construction.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestral.stats import pairing_leaderboard
from orchestral.storage import RunMeta, RunStore
from orchestral.web import state

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


def meta(**kw) -> RunMeta:
    base = {"run_id": "t00000000001", "orchestrator": "a/b", "task_id": "task-x",
            "worker": "c/d", "status": "finished", "started_at": "2026-10-08T10:00:00+00:00",
            "run_dir": "", "dry_run": False}
    base.update(kw)
    return RunMeta(**base)


class TestOutcomeState(unittest.TestCase):
    def out(self, m: RunMeta) -> tuple[str, str]:
        return state.outcome_state(m, now=NOW)

    def test_pass(self):
        self.assertEqual(self.out(meta(passes=True))[0], "pass")

    def test_fail(self):
        self.assertEqual(self.out(meta(passes=False))[0], "fail")

    def test_scored_validation_failure_is_a_fail(self):
        # bare `validation` on a finished run is the deterministic contract
        # failing it — a scored fail, not an exception.
        self.assertEqual(self.out(meta(passes=False, failure_reason="validation"))[0], "fail")

    def test_inconclusive_finished_without_verdict(self):
        o, why = self.out(meta())
        self.assertEqual(o, "inconclusive")
        self.assertTrue(why)

    def test_dry_run_never_reads_as_missing_verdict(self):
        self.assertEqual(self.out(meta(dry_run=True, passes=None))[0], "dry")

    def test_malformed_output_is_invalid(self):
        o, why = self.out(meta(status="failed", failure_reason="exception:malformed_output"))
        self.assertEqual((o, "judgeable" in why), ("invalid", True))

    def test_empty_output_is_invalid(self):
        self.assertEqual(self.out(meta(status="failed", failure_reason="exception:empty_output"))[0],
                         "invalid")

    def test_exception_validation_is_invalid_not_fail(self):
        # the exception: prefix means the run died at the check, not that the
        # contract scored it — same word, different bucket.
        self.assertEqual(self.out(meta(status="failed", failure_reason="exception:validation"))[0],
                         "invalid")

    def test_transport_is_infra(self):
        o, why = self.out(meta(status="failed", failure_reason="exception:transport"))
        self.assertEqual(o, "infra")
        self.assertIn("never got a fair attempt", why)

    def test_failed_without_reason_is_invalid(self):
        self.assertEqual(self.out(meta(status="failed"))[0], "invalid")

    def test_cancelled(self):
        self.assertEqual(self.out(meta(status="cancelled"))[0], "cancelled")

    def test_running_fresh_heartbeat(self):
        started = (NOW - timedelta(minutes=5)).isoformat()
        self.assertEqual(self.out(meta(status="running", started_at=started)), ("running", ""))

    def test_running_past_lost_after_is_lost(self):
        started = (NOW - timedelta(hours=2)).isoformat()
        o, why = self.out(meta(status="running", started_at=started))
        self.assertEqual(o, "lost")
        self.assertIn("heartbeat", why)

    def test_unknown_status(self):
        o, why = self.out(meta(status="weird"))
        self.assertEqual(o, "unknown")
        self.assertIn("weird", why)


class TestOutcomeSets(unittest.TestCase):
    def test_enum_membership_is_total(self):
        # every emitted outcome belongs to a declared bucket or is live/unknown
        emitted = {"pass", "fail", "inconclusive", "invalid", "infra",
                   "dry", "lost", "cancelled", "running", "unknown"}
        declared = (state.OUTCOME_EVIDENCE | state.OUTCOME_HIDDEN
                    | state.OUTCOME_EXCLUDED | {"running", "unknown"})
        self.assertEqual(emitted, declared)

    def test_evidence_is_pass_and_fail_only(self):
        self.assertEqual(state.OUTCOME_EVIDENCE, frozenset({"pass", "fail"}))


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "observatory"


class TestEvidenceDenominators(unittest.TestCase):
    """A cell that is half ghosts and dry-runs must not read as a confident
    failure: n is the ledger, evidence is what the rate is made of."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        cls.store = RunStore(cls.root)
        cls.registry = state.JobRegistry(cls.root, FIXTURES / "tasks",
                                         FIXTURES / "models", cls.store)
        cls._add("ev-pass", status="finished", passes=True)
        cls._add("ev-fail", status="finished", passes=False)
        cls._add("ev-dry", status="finished", passes=None, dry_run=True)
        cls._add("ev-inconclusive", status="finished", passes=None)
        cls._add("ev-invalid", status="failed",
                 failure_reason="exception:malformed_output")
        cls._add("ev-lost", status="running",
                 started_at=(NOW - timedelta(hours=3)).isoformat())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _add(cls, run_id: str, **kw):
        base = {"run_id": run_id, "orchestrator": "a/o", "task_id": "task-x",
                "worker": "c/w", "status": "finished",
                "started_at": "2026-10-08T10:00:00+00:00",
                "finished_at": "2026-10-08T10:05:00+00:00",
                "run_dir": "", "run_group": "g1"}
        base.update(kw)
        cls.store.index_meta(RunMeta(**base))

    def test_matrix_cell_counts_total_and_evidence(self):
        mx = state.task_matrix_payload(self.store)
        cell = mx["tasks"][0]["cells"]["a/o → c/w"]
        self.assertEqual(cell["n"], 6)
        self.assertEqual(cell["evidence"], 2)
        self.assertEqual(cell["pass_rate"], 0.5)
        self.assertEqual(cell["outcomes"]["invalid"], 1)
        self.assertEqual(cell["outcomes"]["dry"], 1)
        self.assertEqual(cell["outcomes"]["lost"], 1)
        self.assertEqual(cell["outcomes"]["inconclusive"], 1)

    def test_leaderboard_pass_rate_ignores_dry_and_inconclusive(self):
        board = pairing_leaderboard(self.store.list_runs(limit=None))
        row = board[0]
        self.assertEqual(row.evidence, 2)
        self.assertEqual(row.pass_rate, 0.5)
        self.assertTrue(row.low_sample)

    def test_groups_pass_rate_uses_evidence(self):
        g = state.groups_payload(self.store)[0]
        self.assertEqual(g["evidence"], 2)
        self.assertEqual(g["pass_rate"], 0.5)
        self.assertEqual(g["runs"], 6)

    def test_lost_row_leaves_live_lane(self):
        live = state.live_runs(self.store, self.registry, now=NOW)
        row = next(r for r in live if r["run_id"] == "ev-lost")
        self.assertEqual(row["state"], "lost")
        self.assertTrue(row["stalled"])
        self.assertTrue(row["lost"])

    def test_runs_filter_by_outcome(self):
        rows = state.runs_payload(self.store, outcome="invalid", now=NOW)
        self.assertEqual([r["run_id"] for r in rows], ["ev-invalid"])
        rows = state.runs_payload(self.store, outcome="lost", now=NOW)
        self.assertEqual([r["run_id"] for r in rows], ["ev-lost"])

    def test_needs_look_collapses_lost_rows(self):
        live = state.live_runs(self.store, self.registry, now=NOW)
        items = state.needs_look_items(self.store, FIXTURES / "models", live)
        lost_items = [i for i in items if i["kind"] == "lost"]
        self.assertEqual(len(lost_items), 1)
        self.assertIsNone(lost_items[0]["run_id"])
        self.assertFalse(any(i["kind"] == "stalled" and i["run_id"] == "ev-lost"
                             for i in items))


if __name__ == "__main__":
    unittest.main()
