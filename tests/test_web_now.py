"""U10: the Now payload (changes, needs-a-look items, group auto-labels) and
the experiments list. Zero network; the key-free U23 corpus plus a few runs."""

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
from orchestral.web import state

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus)


class TestAutoLabel(unittest.TestCase):
    def test_experiment_group_gets_a_human_label(self):
        label = state.auto_group_label("jev-ab:code-fizzbuzz:deepseek/deepseek-v4-pro:z-ai/glm-5.3-flash:jev")
        self.assertEqual(label, "jev-ab · code-fizzbuzz · deepseek-v4-pro / glm-5.3-flash · jev")

    def test_other_groups_have_no_auto_label(self):
        self.assertEqual(state.auto_group_label("corpus-main:r0"), "")
        self.assertEqual(state.auto_group_label("(ungrouped)"), "")
        self.assertEqual(state.auto_group_label("a:b:c:d:neither"), "")

    def test_groups_payload_display_label_prefers_the_curated_label(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = RunStore(tmp)
        for i, grp in enumerate(("ab:t1:o/m:w/m:baseline", "plain")):
            store.index_meta(RunMeta(
                run_id=f"r{i}", orchestrator="o/m", task_id="t1", worker="w/m", status="finished",
                started_at="2026-10-01T00:00:00+00:00", run_group=grp, passes=True))
        groups = {g["group"]: g for g in state.groups_payload(store)}
        self.assertEqual(groups["ab:t1:o/m:w/m:baseline"]["display_label"], "ab · t1 · m / m · baseline")
        self.assertEqual(groups["plain"]["display_label"], "plain")
        gf = Path(tmp) / "groups.yaml"
        gf.write_text("groups:\n  ab:t1:o/m:w/m:baseline:\n    label: Curated\n", encoding="utf-8")
        with_label = {g["group"]: g for g in state.groups_payload(store, gf)}
        self.assertEqual(with_label["ab:t1:o/m:w/m:baseline"]["display_label"], "Curated")


class TestNowPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.root, "full")
        cls.store = RunStore(cls.root)
        cls.registry = state.JobRegistry(cls.root, FIXTURES / "tasks", FIXTURES / "models", cls.store)
        now = datetime.now(UTC)
        cls.now = now
        cls._add(cls, "u10-fresh-pass", "finished", now - timedelta(minutes=5), passes=True, cost=0.02)
        cls._add(cls, "u10-fresh-fail", "failed", now - timedelta(minutes=4), passes=False,
                 cost=0.01, reason="rate_limit")
        cls._add(cls, "u10-fresh-dry", "finished", now - timedelta(minutes=3), passes=True, dry=True)
        cls._add(cls, "u10-inconclusive", "finished", now - timedelta(minutes=2), passes=False,
                 judge={"inconclusive": True, "reasoning": "no usable verdict"})
        cls.store.set_annotation("group", "corpus-solo", "interesting", note="look at this")
        cls.payload = state.overview_payload(
            cls.store, cls.registry, tasks_dir=FIXTURES / "tasks")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _add(self, run_id, status, finished, *, passes, cost=0.0, reason=None, dry=False, judge=None):
        run_dir = self.root / run_id
        run_dir.mkdir()
        if judge is not None:
            (run_dir / "report.json").write_text(json.dumps({"judge": judge}))
        self.store.index_meta(RunMeta(
            run_id=run_id, orchestrator="corpus/orch-a", task_id="corpus-landing-page",
            worker="corpus/worker-cheap", status=status,
            started_at=(finished - timedelta(minutes=1)).isoformat(), finished_at=finished.isoformat(),
            passes=passes, total_cost_usd=cost, failure_reason=reason, dry_run=dry,
            run_dir=str(run_dir), run_group="u10-group"))

    def test_generated_at_is_server_time(self):
        got = datetime.fromisoformat(self.payload["generated_at"])
        self.assertLess(abs((got - self.now).total_seconds()), 60)

    def test_changes_are_finished_real_runs_newest_first(self):
        ids = [c["run_id"] for c in self.payload["changes"]]
        self.assertEqual(ids[:3], ["u10-inconclusive", "u10-fresh-fail", "u10-fresh-pass"])
        self.assertNotIn("u10-fresh-dry", ids)
        self.assertNotIn(self.manifest["orphan_run_id"], ids)  # running rows are Live, not changes
        row = next(c for c in self.payload["changes"] if c["run_id"] == "u10-fresh-fail")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failure_reason"], "rate_limit")
        self.assertEqual(row["cost_usd"], 0.01)
        self.assertTrue(row["finished_at"])
        self.assertNotIn("run_dir", row)

    def test_needs_look_covers_every_kind(self):
        kinds = {i["kind"] for i in self.payload["needs_look"]}
        self.assertEqual(kinds, {"inconclusive_judge", "infra_error", "flagged", "stalled", "pricing_drift"})

    def test_needs_look_items_point_somewhere_and_say_why(self):
        by = {}
        for i in self.payload["needs_look"]:
            by.setdefault(i["kind"], []).append(i)
            self.assertTrue(i["title"] and i["href"].startswith("#/"), i)
        self.assertEqual(by["inconclusive_judge"][0]["run_id"], "u10-inconclusive")
        self.assertTrue(any(i["run_id"] == "u10-fresh-fail" for i in by["infra_error"]))
        self.assertEqual(by["stalled"][0]["run_id"], self.manifest["orphan_run_id"])
        self.assertEqual(by["flagged"][0]["href"], "#/runs?group=corpus-solo")
        self.assertIn("corpus/worker-hot", " ".join(i["title"] for i in by["pricing_drift"]))

    def test_plain_failures_are_not_infra_errors(self):
        self._add("u10-validation", "failed", self.now, passes=False, reason="validation")
        try:
            p = state.overview_payload(self.store, self.registry, tasks_dir=FIXTURES / "tasks")
            self.assertFalse(any(i.get("run_id") == "u10-validation" for i in p["needs_look"]))
        finally:
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(self.root / "index.db")) as conn, conn:
                conn.execute("DELETE FROM runs WHERE run_id='u10-validation'")

    def test_needs_look_is_capped_with_a_total(self):
        self.assertLessEqual(len(self.payload["needs_look"]), 50)
        self.assertEqual(self.payload["needs_look_total"] >= len(self.payload["needs_look"]), True)

    def test_groups_carry_display_label(self):
        g = {x["group"]: x for x in state.groups_payload(self.store)}
        self.assertEqual(g["corpus-solo"]["display_label"], "corpus-solo")


class TestExperimentsList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RunStore(self.root / "runs")
        self.exp = self.root / "experiments"
        self.exp.mkdir()

    def _matrix(self, name, budget=None):
        extra = f"budget: {budget}\n" if budget is not None else ""
        (self.exp / f"{name}.yaml").write_text(
            f"name: {name}\n{extra}orchestrators: [o/m]\nworkers: [w/m]\ntasks: [t]\n", encoding="utf-8")

    def test_empty_directory_is_an_empty_list(self):
        self.assertEqual(state.experiments_list(self.store, self.exp), [])
        self.assertEqual(state.experiments_list(self.store, self.root / "missing"), [])

    def test_lists_each_matrix_with_a_summary(self):
        self._matrix("alpha")
        self._matrix("beta")
        rows = state.experiments_list(self.store, self.exp)
        self.assertEqual([r["name"] for r in rows], ["alpha", "beta"])
        self.assertEqual(rows[0]["cells"], 1)
        self.assertEqual(rows[0]["states"], {"pending": 1})
        self.assertEqual(rows[0]["spend"], 0)

    def test_a_broken_matrix_is_skipped_not_fatal(self):
        self._matrix("ok")
        (self.exp / "bad.yaml").write_text("- not a mapping\n", encoding="utf-8")
        self.assertEqual([r["name"] for r in state.experiments_list(self.store, self.exp)], ["ok"])

    def test_budget_is_reported_only_when_the_spec_records_one(self):
        self._matrix("none")
        self._matrix("capped", budget=12.5)
        p = state.experiment_payload(self.store, self.exp / "none.yaml")
        assert p is not None
        self.assertEqual(p["budget"], {"usd": None, "recorded": False})
        q = state.experiment_payload(self.store, self.exp / "capped.yaml")
        assert q is not None
        self.assertEqual(q["budget"], {"usd": 12.5, "recorded": True})


if __name__ == "__main__":
    unittest.main()
