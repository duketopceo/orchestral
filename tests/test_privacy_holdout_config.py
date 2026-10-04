"""A holdout run is withheld from every hosted surface even when only its
`config` says so.

The runner writes the flag in both `manifest.json` and the run `config`, but
corpus and real holdout runs can carry it in `config` alone (`run.json` and the
index row). `privacy.run_is_holdout` once read only the manifest, so such a run
was scrubbed and published whole. Every test here uses a run whose manifest
never mentions holdout."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestral import cf, privacy
from orchestral.privacy import HoldoutRunError
from orchestral.storage import RunMeta, RunStore
from orchestral.web import snapshot, state

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus)

SECRET = "CANARY-HOLDOUT-KEY-7d41c2e9"


def _dump(payload) -> str:
    return json.dumps(payload, default=str)


class TestRunIsHoldout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _dir(self, *, run_json=None, manifest=None) -> Path:
        d = self.tmp / f"r{len(list(self.tmp.iterdir()))}"
        d.mkdir()
        if run_json is not None:
            (d / "run.json").write_text(json.dumps(run_json))
        if manifest is not None:
            (d / "manifest.json").write_text(json.dumps(manifest))
        return d

    def test_manifest_flag(self):
        self.assertTrue(privacy.run_is_holdout(self._dir(manifest={"holdout": True})))

    def test_config_only_flag_in_run_json(self):
        d = self._dir(run_json={"config": {"holdout": True}}, manifest={"holdout": False})
        self.assertTrue(privacy.run_is_holdout(d))

    def test_config_only_flag_without_any_manifest(self):
        self.assertTrue(privacy.run_is_holdout(self._dir(run_json={"config": {"holdout": True}})))

    def test_config_passed_by_the_caller_counts(self):
        d = self._dir()
        self.assertFalse(privacy.run_is_holdout(d))
        self.assertTrue(privacy.run_is_holdout(d, config={"holdout": True}))

    def test_nested_manifest_config(self):
        self.assertTrue(privacy.run_is_holdout(self._dir(manifest={"config": {"holdout": True}})))

    def test_unflagged_and_unreadable_runs_are_not_holdout(self):
        self.assertFalse(privacy.run_is_holdout(self._dir(
            run_json={"config": {"holdout": False}}, manifest={"holdout": False})))
        self.assertFalse(privacy.run_is_holdout(self._dir()))
        d = self._dir()
        (d / "run.json").write_text("{not json")
        (d / "manifest.json").write_text("[]")
        self.assertFalse(privacy.run_is_holdout(d))

    def test_scrub_run_refuses_a_config_only_holdout_run(self):
        d = self._dir(run_json={"config": {"holdout": True}})
        (d / "artifact.html").write_text(SECRET)
        with self.assertRaises(HoldoutRunError):
            privacy.scrub_run(d, self.tmp / "out")
        self.assertFalse((self.tmp / "out").exists())

    def test_scrub_all_withholds_a_config_only_holdout_run(self):
        runs, out = self.tmp / "runs", self.tmp / "pub"
        d = runs / "o" / "t" / "w" / "r1"
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({
            "run_id": "r1", "orchestrator": "o", "worker": "w", "task_id": "t",
            "status": "finished", "config": {"holdout": True}}))
        (d / "artifact.html").write_text(SECRET)
        privacy.scrub_all(runs, out)
        published = "".join(p.read_text(errors="ignore") for p in out.rglob("*") if p.is_file())
        self.assertNotIn(SECRET, published)
        self.assertFalse((out / "o" / "t" / "w" / "r1").exists())


class TestSyncSurfaces(unittest.TestCase):
    """cf.py: D1 rows, the pushed file tree and the sync loop."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = RunStore(self.tmp / "runs")
        self.run_id, self.run_dir = self.store.new_run(
            "org/orch", "task-x", "org/work", config={"holdout": True})
        (self.run_dir / "artifact.html").write_text(SECRET)
        (self.run_dir / "events.jsonl").write_text(json.dumps(
            {"type": "llm_call", "input": {"messages": [{"content": SECRET}]}}) + "\n")
        meta = self.store.get_run(self.run_id)
        assert meta is not None
        meta.status = "finished"
        self.store.update_meta(meta)
        self.store.record_call(
            run_id=self.run_id, phase="delegate", step=1, role="worker", model="org/work",
            input_tokens=1, output_tokens=1, cost_usd=0.001,
            input_json=json.dumps({"m": SECRET}), output_json=json.dumps({"m": SECRET}))

    def test_manifest_is_not_flagged_but_the_run_is_holdout(self):
        self.assertFalse((self.run_dir / "manifest.json").exists())
        self.assertTrue(privacy.run_is_holdout(self.run_dir))

    def test_d1_projection_has_no_rows_or_calls(self):
        proj = cf.d1_projection(self.store, [self.run_id])
        self.assertEqual(proj["runs"], [])
        self.assertEqual(proj["calls"], [])
        self.assertEqual(cf.d1_projection(self.store)["runs"], [])

    def test_scrub_to_dir_refuses(self):
        with self.assertRaises(cf.HoldoutRunError):
            cf.scrub_to_dir(self.run_dir, self.tmp / "pub")

    def test_hosted_run_payloads_are_a_withheld_stub_without_the_canary(self):
        out = snapshot.run_payloads(self.store, self.run_id, self.tmp / "tasks")
        self.assertEqual(list(out), [f"run/{self.run_id}.json"])
        stub = out[f"run/{self.run_id}.json"]
        self.assertTrue(stub["holdout"])
        self.assertEqual({s["state"] for s in stub["sections"].values()}, {"withheld"})
        self.assertNotIn(SECRET, _dump(out))

    def test_the_withheld_stub_carries_no_outcome_in_its_meta(self):
        meta = self.store.get_run(self.run_id)
        assert meta is not None
        meta.passes, meta.score, meta.failure_reason = False, 0.7412, "outcome-canary"
        self.store.update_meta(meta)
        stub = snapshot.run_payloads(self.store, self.run_id, self.tmp / "tasks")[f"run/{self.run_id}.json"]
        for field in ("passes", "score", "judge_score", "judge_passed", "failure_reason"):
            self.assertIsNone(stub["meta"][field], field)
        self.assertTrue(stub["meta"]["holdout"])  # the verdict chip reads this: "Withheld", not "Fail"
        self.assertNotIn("0.7412", _dump(stub))
        self.assertNotIn("outcome-canary", _dump(stub))

    def test_a_run_whose_run_json_is_gone_is_still_withheld_by_its_index_config(self):
        (self.run_dir / "run.json").unlink()
        self.assertTrue(privacy.run_is_holdout(
            self.run_dir, config=(self.store.get_run(self.run_id) or RunMeta(
                run_id="", orchestrator="", task_id="", worker="", status="", started_at="")).config))
        out = snapshot.run_payloads(self.store, self.run_id, self.tmp / "tasks")
        self.assertEqual(list(out), [f"run/{self.run_id}.json"])
        self.assertNotIn(SECRET, _dump(out))


class TestHostedSnapshot(unittest.TestCase):
    """The whole key tree over the corpus, whose corpus-holdout runs are config-only."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        cls.store = RunStore(cls.tmp / "runs")
        runs = cls.store.list_runs(limit=None)
        cls.held = [r for r in runs if (r.config or {}).get("holdout")]
        cls.held_ids = {r.run_id for r in cls.held}
        cls.open_runs = [r for r in runs if r.run_id not in cls.held_ids]
        for r in cls.held:
            (Path(r.run_dir) / "artifact.html").write_text(f"<p>{SECRET}</p>")
            (Path(r.run_dir) / "plan.md").write_text(SECRET)
        cls.snap = snapshot.build_snapshot(
            cls.store, FIXTURES / "tasks", FIXTURES / "models", groups_file=FIXTURES / "groups.yaml",
            run_ids=[*sorted(cls.held_ids), cls.open_runs[0].run_id],
            synced_at="2026-10-02T12:00:00+00:00", source_commit="abc1234")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_fixture_holdout_runs_are_config_only(self):
        self.assertGreaterEqual(len(self.held), 4)
        for r in self.held:
            self.assertFalse((Path(r.run_dir) / "manifest.json").exists())
            self.assertTrue(privacy.run_is_holdout(Path(r.run_dir)))

    def test_no_key_anywhere_carries_the_canary(self):
        for key, payload in self.snap.items():
            self.assertNotIn(SECRET, _dump(payload), key)

    def test_per_run_keys_are_only_withheld_stubs(self):
        for rid in self.held_ids:
            self.assertIn(f"run/{rid}.json", self.snap)
            self.assertNotIn(f"run/{rid}/live.json", self.snap)
            self.assertNotIn(f"run/{rid}/evidence.json", self.snap)
            stub = self.snap[f"run/{rid}.json"]
            self.assertTrue(stub["holdout"])
            self.assertEqual({s["state"] for s in stub["sections"].values()}, {"withheld"})

    def test_runs_list_names_the_run_but_carries_no_outcome(self):
        rows = {r["run_id"]: r for r in self.snap["runs.json"]}
        for rid in self.held_ids:
            row = rows[rid]  # the withheld page stays reachable from the list
            self.assertTrue(row["holdout"])
            for field in ("passes", "score", "judge_score", "judge_passed", "failure_reason"):
                self.assertIsNone(row[field], field)
        self.assertFalse(any(r.get("holdout") for rid, r in rows.items() if rid not in self.held_ids))

    def test_aggregates_ignore_holdout_runs(self):
        groups = {g["group"] for g in self.snap["groups.json"]}
        self.assertNotIn("corpus-holdout", groups)
        n_open = len([r for r in self.store.list_runs(limit=None) if r.run_id not in self.held_ids])
        self.assertEqual(sum(g["runs"] for g in self.snap["groups.json"]), n_open)
        for key in ("leaderboard.json",):
            self.assertFalse(any(r.get("holdout_only") for r in self.snap[key]), key)
        for row in self.snap["pairings.json"]["rows"]:
            self.assertFalse(row.get("holdout_only"))
            self.assertEqual(row["holdout_runs"], 0)
        cell_runs = sum(c["n"] for t in self.snap["matrix.json"]["tasks"] for c in t["cells"].values())
        self.assertEqual(cell_runs, n_open)

    def test_compare_cards_and_overview_never_name_the_holdout_group(self):
        for key, payload in self.snap.items():
            if key.startswith(("compare.", "cards.", "card/", "overview")):
                self.assertNotIn("corpus-holdout", _dump(payload), key)
        self.assertFalse(any(k.startswith("compare.") and "corpus-holdout" in k for k in self.snap))
        self.assertFalse(any(k.startswith("pairings.corpus-holdout") for k in self.snap))
        for rid in self.held_ids:
            self.assertNotIn(rid, _dump(self.snap["overview.json"]))
            self.assertFalse(any(rid in _dump(v) for k, v in self.snap.items()
                                 if k.startswith(("cards.", "card/", "compare."))))


class TestHostedExperiments(unittest.TestCase):
    def test_a_config_only_holdout_arm_run_is_not_counted_in_the_ledger(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = RunStore(tmp / "runs")
        tasks = tmp / "tasks"
        tasks.mkdir()
        exp = tmp / "experiments"
        exp.mkdir()
        orch, worker, task = "org/o", "org/w", "t-task"
        (exp / "ab.yaml").write_text(
            f"name: ab\norchestrators: [{orch}]\nworkers: [{worker}]\ntasks: [{task}]\n")
        for arm in ("baseline", "jev"):
            for i in range(3):
                rid = f"{arm}{i}"
                store.index_meta(RunMeta(
                    run_id=rid, orchestrator=orch, task_id=task, worker=worker, status="finished",
                    started_at="2026-10-01T00:00:00+00:00", finished_at="2026-10-01T00:01:00+00:00",
                    passes=True, total_cost_usd=0.01, run_group=f"ab:{task}:{orch}:{worker}:{arm}",
                    replicate=i, config={"jev_assist": arm == "jev", "holdout": arm == "jev" and i == 0}))
        snap = snapshot.build_snapshot(store, tasks, tmp / "models", run_ids=[],
                                       synced_at="x", source_commit="y")
        payload = snap["experiment.ab.json"]
        jev_n = [c["jev"]["n"] for c in payload["cells"] if c.get("jev")]
        self.assertEqual(jev_n, [2], "the holdout jev run must not be counted")
        local = state.experiment_payload(store, exp / "ab.yaml", tasks_dir=tasks)
        assert local is not None
        self.assertEqual([c["jev"]["n"] for c in local["cells"]], [3])


if __name__ == "__main__":
    unittest.main()
