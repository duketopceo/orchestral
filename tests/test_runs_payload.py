"""U11: the Runs payload's facets, sort and derived fields over the U23 corpus.

Zero network; the corpus builder reads no provider key. Pagination is
client-side in both modes (KTD5), so it is covered by the browser suite.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import browser_corpus as bc

from orchestral.storage import RunStore
from orchestral.web import state

TASKS = bc.FIXTURES / "tasks"
CLOCK = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


class TestRunsPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = bc.shared()
        cls.store = RunStore(cls.srv.root)
        cls.m = cls.srv.manifest

    def rows(self, **kw):
        kw.setdefault("tasks_dir", TASKS)
        kw.setdefault("now", CLOCK)
        return state.runs_payload(self.store, **kw)

    def test_unfiltered_returns_every_run_newest_first(self):
        rows = self.rows()
        self.assertGreaterEqual(len(rows), 1000)
        starts = [r["started_at"] or "" for r in rows]
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_stalled_lists_only_the_orphan(self):
        rows = self.rows(status="stalled")
        self.assertEqual([r["run_id"] for r in rows], [self.m["orphan_run_id"]])
        self.assertTrue(rows[0]["stalled"])

    def test_running_rows_carry_a_stalled_flag_and_others_do_not(self):
        rows = self.rows()
        self.assertEqual([r["run_id"] for r in rows if r["stalled"]], [self.m["orphan_run_id"]])
        live = self.rows(status="running", now=datetime.now(UTC))
        self.assertTrue(any(r["run_id"] == "cli-live0001" and not r["stalled"] for r in live))

    def test_abandoned_orphan_is_terminal_not_stalled(self):
        srv = bc.CorpusServer("full")
        self.addCleanup(srv.close)
        store = RunStore(srv.root)
        store.set_annotation("run", srv.manifest["orphan_run_id"], "aborted")
        rows = state.runs_payload(store, tasks_dir=TASKS, now=CLOCK, status="stalled")
        self.assertEqual(rows, [])

    def test_pairing_facet_matches_orchestrator_and_worker(self):
        rows = self.rows(pairing="corpus/orch-b|corpus/worker-hot")
        self.assertTrue(rows)
        self.assertTrue(all(r["orchestrator"] == "corpus/orch-b" and r["worker"] == "corpus/worker-hot"
                            for r in rows))

    def test_pairing_facet_accepts_the_legacy_arrow_form(self):
        """U10 heatmap links once used the matrix payload key `orch → worker`."""
        want = self.rows(pairing="corpus/orch-b|corpus/worker-hot")
        got = self.rows(pairing="corpus/orch-b \u2192 corpus/worker-hot")
        self.assertTrue(want)
        self.assertEqual([r["run_id"] for r in got], [r["run_id"] for r in want])

    def test_judge_facet_filters_on_judge_state(self):
        rows = self.rows(judge="judged")
        self.assertTrue(rows)
        self.assertEqual({r["judge_state"] for r in rows}, {"judged"})
        none = self.rows(judge="not_judged")
        self.assertEqual({r["judge_state"] for r in none}, {"not_judged"})

    def test_type_facet_uses_the_task_spec_and_unknown_difficulty_is_empty(self):
        self.assertEqual(len(self.rows(type="html")), len(self.rows()))
        self.assertEqual(self.rows(type="sql"), [])
        self.assertEqual(self.rows(difficulty="hard"), [])

    def test_facets_combine(self):
        rows = self.rows(group=self.m["long_group"], status="passed")
        self.assertTrue(all(r["run_group"] == self.m["long_group"] for r in rows))
        both = self.rows(group=self.m["long_group"], task="corpus-landing-page")
        self.assertEqual(len(both), 5)

    def test_rows_carry_group_label_and_type(self):
        r = self.rows(group=self.m["long_group"])[0]
        self.assertEqual(r["type"], "html")
        self.assertIn("group_label", r)

    def test_sort_by_cost_both_directions_puts_unknown_last(self):
        desc = [r["billed_cost_usd"] for r in self.rows(sort="cost")]
        known = [c for c in desc if c is not None]
        self.assertEqual(known, sorted(known, reverse=True))
        asc = [r["billed_cost_usd"] for r in self.rows(sort="cost", direction="asc")]
        known_asc = [c for c in asc if c is not None]
        self.assertEqual(known_asc, sorted(known_asc))
        for seq in (desc, asc):
            seen_none = False
            for c in seq:
                seen_none = seen_none or c is None
                self.assertFalse(seen_none and c is not None, "unknown cost sorted before a known one")

    def test_sort_by_task_is_ascending_by_default_and_unknown_sort_falls_back(self):
        tasks = [r["task_id"] for r in self.rows(sort="task")]
        self.assertEqual(tasks, sorted(tasks))
        default = [r["run_id"] for r in self.rows()]
        self.assertEqual([r["run_id"] for r in self.rows(sort="bogus")], default)

    def test_http_route_passes_facets_and_sort_through(self):
        got = self.srv.api("/api/runs?status=stalled")
        self.assertEqual([r["run_id"] for r in got], [self.m["orphan_run_id"]])
        got = self.srv.api("/api/runs?pairing=corpus%2Forch-b%7Ccorpus%2Fworker-hot&sort=duration&dir=asc")
        self.assertTrue(got)
        lat = [r["latency_ms"] for r in got if r["latency_ms"] is not None]
        self.assertEqual(lat, sorted(lat))


if __name__ == "__main__":
    unittest.main()
