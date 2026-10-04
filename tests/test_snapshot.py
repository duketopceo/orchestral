"""U6: the hosted key tree (`orchestral/web/snapshot.py`), rendered from the U23 corpus."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from orchestral.web import snapshot, state

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
_SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _SPEC and _SPEC.loader
corpus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(corpus)


def enc(s: str) -> str:
    return quote(s, safe="")


class TestSnapshot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.root, "full")
        from orchestral.storage import RunStore
        cls.store = RunStore(cls.root)
        cls.run_ids = [cls.manifest["failed_run_id"], cls.manifest["orphan_run_id"]]
        cls.snap = snapshot.build_snapshot(
            cls.store, FIXTURES / "tasks", FIXTURES / "models",
            groups_file=FIXTURES / "groups.yaml", run_ids=cls.run_ids,
            synced_at="2026-10-02T12:00:00+00:00", source_commit="abc1234")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_meta_says_hosted_and_carries_sync_time(self):
        meta = self.snap["meta.json"]
        self.assertEqual(meta["mode"], "hosted")
        self.assertEqual(meta["synced_at"], "2026-10-02T12:00:00+00:00")
        self.assertEqual(meta["source_commit"], "abc1234")
        self.assertFalse(any(meta["capabilities"].values()))

    def test_core_resources_match_the_payload_functions(self):
        self.assertEqual(self.snap["runs.json"],
                         json.loads(json.dumps(state.runs_payload(self.store, tasks_dir=FIXTURES / "tasks"), default=str)))
        for key in ("overview.json", "groups.json", "matrix.json", "leaderboard.json",
                    "pairings.json", "flags.json"):
            self.assertIn(key, self.snap)

    def test_one_run_key_per_requested_run(self):
        for rid in self.run_ids:
            self.assertEqual(self.snap[f"run/{rid}.json"]["meta"]["run_id"], rid)
            self.assertIn("rows", self.snap[f"run/{rid}/live.json"])
        self.assertNotIn("run/corp00000001.json", self.snap)

    def test_compare_for_every_ordered_pair_of_groups(self):
        groups = [g["group"] for g in self.snap["groups.json"]]
        self.assertLess(len(groups), snapshot.MAX_COMPARE_GROUPS)
        for a in groups:
            for b in groups:
                if a != b:
                    self.assertIn(f"compare.{enc(a)}.{enc(b)}.json", self.snap)

    def test_compare_is_skipped_at_thirty_groups_or_more(self):
        with patch.object(snapshot, "MAX_COMPARE_GROUPS", 2):
            snap = snapshot.build_snapshot(
                self.store, FIXTURES / "tasks", FIXTURES / "models",
                groups_file=FIXTURES / "groups.yaml", run_ids=[])
        self.assertFalse([k for k in snap if k.startswith("compare.")])

    def test_cards_per_lens_and_card_keys(self):
        for lens in (item["id"] for item in state.CARD_LENSES):
            cat = self.snap[f"cards.{lens}.json"]
            self.assertTrue(cat["cards"])
            for card in cat["cards"]:
                self.assertIn(f"card/{card['kind']}/{enc(card['target'])}.{lens}.json", self.snap)

    def test_pairings_per_group(self):
        for g in self.snap["groups.json"]:
            self.assertIn(f"pairings.{enc(g['group'])}.json", self.snap)

    def test_no_forbidden_body_keys(self):
        blob = json.dumps(self.snap)
        for key in ("input_json", "output_json", "call_previews"):
            self.assertNotIn(f'"{key}"', blob)

    def test_write_snapshot_lays_the_tree_under_api(self):
        with tempfile.TemporaryDirectory() as out:
            n = snapshot.write_snapshot(self.snap, Path(out))
            self.assertEqual(n, len(self.snap))
            self.assertEqual(json.loads((Path(out) / "api" / "meta.json").read_text())["mode"], "hosted")
            rid = self.run_ids[0]
            self.assertTrue((Path(out) / "api" / "run" / f"{rid}.json").is_file())

    def test_write_snapshot_rejects_escaping_keys(self):
        with tempfile.TemporaryDirectory() as out, self.assertRaises(ValueError):
            snapshot.write_snapshot({"../evil.json": {}}, Path(out))


if __name__ == "__main__":
    unittest.main()
