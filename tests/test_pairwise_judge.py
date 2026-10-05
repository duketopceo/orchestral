"""Pairwise judging + Bradley-Terry: position swap, battle pairing, storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.judge import (
    judge_pair,
    pairwise_battles,
    pairwise_judge,
    stored_battles,
)
from orchestral.runner import Runner
from orchestral.stats import bradley_terry
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


class _PairClient:
    """Answers winner by which artifact text carries a marker substring.

    `prefer` names the artifact slot that always wins ("A", "B") or a
    marker string: the artifact CONTAINING the marker wins. Two calls per
    battle (position swap) exercise the unmapping.
    """

    def __init__(self, prefer: str = "A"):
        self.calls = 0
        self.prefer = prefer

    def chat(self, **kw):
        self.calls += 1
        prompt = kw["messages"][-1]["content"]
        a_sec, _, b_sec = prompt.partition("Artifact A:")[2].partition("Artifact B:")
        if self.prefer in ("A", "B"):
            winner = self.prefer
        else:
            a_has = self.prefer in a_sec
            b_has = self.prefer in b_sec
            winner = "A" if a_has and not b_has else "B" if b_has and not a_has else "tie"
        return {
            "content": json.dumps({"winner": winner, "reasoning": "fake"}),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1.0,
        }


class _Logger:
    """Minimal stand-in for EventLogger — records nothing."""

    def log_llm_call(self, **kw):
        pass

    def log(self, **kw):
        pass


class TestJudgePair(unittest.TestCase):
    def _judge(self):
        return _model("j/model", "judge")

    def _task(self):
        return TaskSpec(id="t-task", type="html", prompt="make a page")

    def test_marker_winner_maps_back_to_caller_order(self):
        """The artifact carrying the marker wins regardless of the seat the
        judge saw it in — both swapped calls agree on `a`."""
        res, costs = judge_pair(
            logger=_Logger(), step=0, task=self._task(),
            artifact_a="the GOOD one", artifact_b="the bad one",
            judge=self._judge(), client=_PairClient("GOOD"), dry_run=False)
        self.assertEqual(res["winner"], "a")
        self.assertEqual(len(costs), 2)

    def test_position_biased_judge_resolves_to_tie(self):
        """A judge that always picks A is position-biased: the swap flips the
        apparent winner, and the combined verdict must be a tie."""
        res, _ = judge_pair(
            logger=_Logger(), step=0, task=self._task(),
            artifact_a="x", artifact_b="y",
            judge=self._judge(), client=_PairClient("A"), dry_run=False)
        self.assertEqual(res["winner"], "tie")
        self.assertTrue(res["split_verdict"])

    def test_parse_failure_is_inconclusive(self):
        class Bad:
            def chat(self, **kw):
                return {"content": "not json", "usage": {}, "latency_ms": 1}

        res, _ = judge_pair(
            logger=_Logger(), step=0, task=self._task(),
            artifact_a="x", artifact_b="y",
            judge=self._judge(), client=Bad(), dry_run=False)
        self.assertIsNone(res["winner"])
        self.assertTrue(res["inconclusive"])

    def test_dry_run_is_deterministic(self):
        r1, _ = judge_pair(
            logger=_Logger(), step=0, task=self._task(),
            artifact_a="x", artifact_b="y",
            judge=self._judge(), client=None, dry_run=True)
        r2, _ = judge_pair(
            logger=_Logger(), step=0, task=self._task(),
            artifact_a="x", artifact_b="y",
            judge=self._judge(), client=None, dry_run=True)
        self.assertEqual(r1["winner"], r2["winner"])
        self.assertIn(r1["winner"], ("a", "b", "tie"))


class TestBattlePairing(unittest.TestCase):
    def _meta(self, run_id, task, orch, worker, started=""):
        class M:
            pass
        m = M()
        m.run_id, m.task_id = run_id, task
        m.orchestrator, m.worker = orch, worker
        m.started_at = started
        return m

    def test_index_pairs_same_task_across_pairings(self):
        metas = [
            self._meta("a1", "t", "o/1", "w/1", "2026-01-01"),
            self._meta("a2", "t", "o/1", "w/1", "2026-01-02"),
            self._meta("b1", "t", "o/2", "w/1", "2026-01-01"),
            self._meta("b2", "t", "o/2", "w/1", "2026-01-02"),
        ]
        pairs = pairwise_battles(metas)
        self.assertEqual(len(pairs), 2)
        self.assertEqual({p[0].run_id for p in pairs}, {"a1", "a2"})

    def test_same_pairing_never_battles_itself(self):
        metas = [
            self._meta("a1", "t", "o/1", "w/1"),
            self._meta("a2", "t", "o/1", "w/1"),
        ]
        self.assertEqual(pairwise_battles(metas), [])

    def test_different_tasks_do_not_pair(self):
        metas = [
            self._meta("a1", "t1", "o/1", "w/1"),
            self._meta("b1", "t2", "o/2", "w/1"),
        ]
        self.assertEqual(pairwise_battles(metas), [])

    def test_unbalanced_replicates_pair_the_short_side(self):
        metas = [
            self._meta("a1", "t", "o/1", "w/1"),
            self._meta("a2", "t", "o/1", "w/1"),
            self._meta("a3", "t", "o/1", "w/1"),
            self._meta("b1", "t", "o/2", "w/1"),
        ]
        pairs = pairwise_battles(metas)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0].run_id, "a1")


class TestPairwiseJudge(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "tasks").mkdir()
        (self.root / "tasks" / "t-task.yaml").write_text(
            "id: t-task\ntype: html\nprompt: make a page\nvalidation:\n  - non_empty\n")
        self.runs = self.root / "runs"
        self.judge = _model("j/model", "judge")

    def _seed(self, orch: str, marker: str = "") -> str:
        meta = Runner(dry_run=True, runs_dir=str(self.runs),
                      store=RunStore(self.runs)).run(
            TaskSpec(id="t-task", type="html", prompt="make a page"),
            _model(orch, "orchestrator"), _model("w/model", "worker"))
        meta.dry_run = False
        RunStore(self.runs).update_meta(meta)
        if marker:
            for a in Path(meta.run_dir).glob("artifact.*"):
                a.write_text(f"<html>{marker} artifact</html>")
        return meta.run_id

    def test_battle_writes_verdicts_on_both_reports(self):
        ra = self._seed("o/a-model", marker="GOOD")
        rb = self._seed("o/b-model")
        store = RunStore(self.runs)
        res = pairwise_judge(store, self.judge, _PairClient("GOOD"),
                             tasks_dir=self.root / "tasks")
        self.assertEqual(res["judged"], 1)
        rep_a = json.loads((Path(store.get_run(ra).run_dir) / "report.json").read_text())
        rep_b = json.loads((Path(store.get_run(rb).run_dir) / "report.json").read_text())
        pw_a, pw_b = rep_a["pairwise"][0], rep_b["pairwise"][0]
        self.assertEqual(pw_a["winner"], "self")
        self.assertEqual(pw_b["winner"], "opponent")
        self.assertEqual(pw_a["vs"], rb)
        self.assertEqual(pw_b["vs"], ra)
        self.assertEqual(pw_a["judge"], "j/model")

    def test_second_pass_dedupes(self):
        self._seed("o/a-model")
        self._seed("o/b-model")
        client = _PairClient("GOOD")
        pairwise_judge(RunStore(self.runs), self.judge, client,
                       tasks_dir=self.root / "tasks")
        res = pairwise_judge(RunStore(self.runs), self.judge, client,
                             tasks_dir=self.root / "tasks")
        self.assertEqual(res["judged"], 0)
        self.assertEqual(client.calls, 2)  # one battle = two swapped calls

    def test_dry_run_run_is_never_a_battle_ground(self):
        """Synthetic artifacts must not leak verdicts into the corpus."""
        ra = self._seed("o/a-model")
        meta = RunStore(self.runs).get_run(ra)
        meta.dry_run = True
        RunStore(self.runs).update_meta(meta)
        self._seed("o/b-model")
        res = pairwise_judge(RunStore(self.runs), self.judge, _PairClient("GOOD"),
                             tasks_dir=self.root / "tasks")
        self.assertEqual(res["battles_seen"], 0)

    def test_multi_battle_run_keeps_every_verdict(self):
        """One run battling two opponents must keep both records — a lost
        update here silently drops half the BT evidence."""
        ra = self._seed("o/a-model")
        self._seed("o/b-model")
        self._seed("o/c-model")
        res = pairwise_judge(RunStore(self.runs), self.judge, _PairClient("GOOD"),
                             tasks_dir=self.root / "tasks", jobs=1)
        self.assertEqual(res["judged"], 3)  # a-b, a-c, b-c
        rep = json.loads(
            (Path(RunStore(self.runs).get_run(ra).run_dir) / "report.json").read_text())
        self.assertEqual(len(rep["pairwise"]), 2)

    def test_force_replaces_the_record_not_duplicates(self):
        """Re-judging with the same judge rewrites the (vs, judge) slot —
        appending would leave the stale verdict to win the BT dedup."""
        ra = self._seed("o/a-model", marker="GOOD")
        self._seed("o/b-model")
        pairwise_judge(RunStore(self.runs), self.judge, _PairClient("GOOD"),
                       tasks_dir=self.root / "tasks")
        # force re-runs the marking path; the judge cache still serves the
        # identical inputs (same semantics as backfill_judgments --force)
        res = pairwise_judge(RunStore(self.runs), self.judge, _PairClient("A"),
                             tasks_dir=self.root / "tasks", force=True)
        self.assertEqual(res["judged"], 1)
        rep = json.loads(
            (Path(RunStore(self.runs).get_run(ra).run_dir) / "report.json").read_text())
        self.assertEqual(len(rep["pairwise"]), 1)
        self.assertEqual(rep["pairwise"][0]["winner"], "self")

    def test_battle_cost_splits_between_the_two_runs(self):
        ra = self._seed("o/a-model")
        rb = self._seed("o/b-model")
        store = RunStore(self.runs)
        before = {m.run_id: m.total_cost_usd for m in store.list_runs()}
        pairwise_judge(store, self.judge, _PairClient("GOOD"),
                       tasks_dir=self.root / "tasks")
        after = {m.run_id: m.total_cost_usd
                 for m in RunStore(self.runs).list_runs()}
        d_a, d_b = after[ra] - before[ra], after[rb] - before[rb]
        self.assertGreater(d_a, 0)
        self.assertAlmostEqual(d_a, d_b)

    def test_stored_battles_dedupes_across_reports(self):
        """Each battle lives on both runs' reports — the BT frame must see
        it once, from the alphabetically-first player's seat."""
        self._seed("o/a-model", marker="GOOD")
        self._seed("o/b-model")
        store = RunStore(self.runs)
        pairwise_judge(store, self.judge, _PairClient("GOOD"),
                       tasks_dir=self.root / "tasks")
        metas = store.list_runs()
        battles = stored_battles(metas)
        self.assertEqual(len(battles), 1)
        a, b, outcome = battles[0]
        self.assertEqual((a, b), ("o/a-model|w/model", "o/b-model|w/model"))
        self.assertEqual(outcome, "a")

    def test_bt_arrives_in_the_summary(self):
        self._seed("o/a-model", marker="GOOD")
        self._seed("o/b-model")
        res = pairwise_judge(RunStore(self.runs), self.judge, _PairClient("GOOD"),
                             tasks_dir=self.root / "tasks")
        bt = res["bt"]
        self.assertGreater(bt["o/a-model|w/model"]["rating"],
                           bt["o/b-model|w/model"]["rating"])


class TestBradleyTerry(unittest.TestCase):
    def test_dominant_player_rates_highest(self):
        battles = [("a", "b", "a"), ("a", "b", "a"), ("a", "b", "a"), ("a", "b", "b")]
        bt = bradley_terry(battles)
        self.assertGreater(bt["a"]["rating"], bt["b"]["rating"])
        self.assertEqual(bt["a"]["battles"], 4)
        self.assertEqual(bt["a"]["wins"], 3.0)

    def test_ties_pull_toward_even(self):
        battles = [("a", "b", "tie"), ("a", "b", "tie")]
        bt = bradley_terry(battles)
        self.assertAlmostEqual(bt["a"]["rating"], bt["b"]["rating"], places=6)
        self.assertEqual(bt["a"]["wins"], 1.0)

    def test_self_battles_and_bad_outcomes_dropped(self):
        self.assertEqual(bradley_terry([("a", "a", "a"), ("a", "b", "x")]), {})

    def test_empty(self):
        self.assertEqual(bradley_terry([]), {})

    def test_three_way_round_robin(self):
        """a beats b, b beats c, a beats c — ratings must order a>b>c."""
        # outcome names the winning SLOT ("a" = first player, "b" = second)
        battles = [("a", "b", "a"), ("b", "c", "a"), ("a", "c", "a")] * 3
        bt = bradley_terry(battles)
        self.assertGreater(bt["a"]["rating"], bt["b"]["rating"])
        self.assertGreater(bt["b"]["rating"], bt["c"]["rating"])
        # geometric-mean-1 normalization
        prod = bt["a"]["rating"] * bt["b"]["rating"] * bt["c"]["rating"]
        self.assertAlmostEqual(prod, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
