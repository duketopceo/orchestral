"""Retroactive judge backfill: report.json marker, index score, idempotence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral import judge
from orchestral.config import ModelConfig, TaskSpec
from orchestral.judge import _judge_input, _NoJudgeableArtifact, backfill_judgments
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


class FakeJudgeClient:
    def __init__(self, score: float = 0.8):
        self.calls = 0
        self.score = score

    def chat(self, **kw):
        self.calls += 1
        return {
            "content": json.dumps({"score": self.score, "passed": self.score > 0.5,
                                   "reasoning": "fake judge"}),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 1.0,
        }


class TestBackfill(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "tasks").mkdir()
        (self.root / "tasks" / "t-task.yaml").write_text(
            "id: t-task\ntype: html\nprompt: make a page\nvalidation:\n  - non_empty\n")
        self.runs = self.root / "runs"
        self.judge = _model("j/model", "judge")

    def _seed(self, dry_run_meta: bool = False) -> str:
        meta = Runner(dry_run=True, runs_dir=str(self.runs),
                      store=RunStore(self.runs)).run(
            TaskSpec(id="t-task", type="html", prompt="make a page"),
            _model("o/model", "orchestrator"), _model("w/model", "worker"))
        # the Runner only produces artifacts in dry-run mode; flip the index
        # flag so backfill treats the seed as a real finished run
        if not dry_run_meta:
            meta.dry_run = False
            RunStore(self.runs).update_meta(meta)
        return meta.run_id

    def test_backfill_writes_judge_and_score(self):
        run_id = self._seed()
        store = RunStore(self.runs)
        res = backfill_judgments(store, self.judge, FakeJudgeClient(0.9),
                                 run_group=None, tasks_dir=self.root / "tasks")
        self.assertEqual(res["judged"], 1)
        run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
        report = json.loads((run_dir / "report.json").read_text())
        self.assertEqual(report["judge"]["score"], 0.9)
        self.assertTrue(report["judge_backfill"])
        meta = store.get_run(run_id)
        # the judge's number lands on the judge axis — the mechanical
        # score field is never overwritten by a judge result
        self.assertEqual(meta.judge_score, 0.9)
        self.assertTrue(meta.judge_passed)
        self.assertIsNone(meta.score)  # html has no mechanical score
        # the recorded mechanical verdict is untouched
        self.assertTrue(meta.passes)

    def test_second_pass_skips_judged(self):
        self._seed()
        store = RunStore(self.runs)
        client = FakeJudgeClient()
        backfill_judgments(store, self.judge, client, tasks_dir=self.root / "tasks")
        res = backfill_judgments(RunStore(self.runs), self.judge, client,
                                 tasks_dir=self.root / "tasks")
        self.assertEqual(res["judged"], 0)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(client.calls, 1)  # cached + dedup: one API call total

    def test_force_rejudges(self):
        self._seed()
        client = FakeJudgeClient()
        backfill_judgments(RunStore(self.runs), self.judge, client,
                           tasks_dir=self.root / "tasks")
        res = backfill_judgments(RunStore(self.runs), self.judge, client,
                                 force=True, tasks_dir=self.root / "tasks")
        # second pass hits the persistent judge cache — still one API call,
        # but the run is re-marked judged rather than skipped
        self.assertEqual(res["judged"], 1)
        self.assertEqual(client.calls, 1)

    def test_no_artifact_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            meta = Runner(dry_run=True, runs_dir=tmp, store=store).run(
                TaskSpec(id="t-task", type="html", prompt="p"),
                _model("o/model", "orchestrator"), _model("w/model", "worker"))
            meta.dry_run = False
            store.update_meta(meta)
            for a in Path(meta.run_dir).glob("artifact.*"):
                a.unlink()
            res = backfill_judgments(store, self.judge, FakeJudgeClient(),
                                     tasks_dir=self.root / "tasks")
            self.assertEqual(res["judged"], 0)
            self.assertIn("no artifact", res["results"][0]["skipped"])

    def test_inconclusive_report_is_rejudged(self):
        """An inconclusive judge block is a no-answer, not a lock — the next
        backfill pass must retry it without --force."""
        run_id = self._seed()
        store = RunStore(self.runs)
        run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
        report_path = run_dir / "report.json"
        report = json.loads(report_path.read_text())
        report["judge"] = {"inconclusive": True, "score": None, "passed": None,
                           "reasoning": "parse failure"}
        report_path.write_text(json.dumps(report))

        client = FakeJudgeClient(0.7)
        res = backfill_judgments(store, self.judge, client,
                                 tasks_dir=self.root / "tasks")
        self.assertEqual(res["judged"], 1)
        self.assertEqual(client.calls, 1)
        meta = store.get_run(run_id)
        self.assertEqual(meta.judge_score, 0.7)

    def test_inconclusive_backfill_keeps_meta_clean(self):
        """A judge that fails again writes no judge fields to the index."""
        self._seed()
        store = RunStore(self.runs)

        class BadClient:
            calls = 0

            def chat(self, **kw):
                self.calls += 1
                return {"content": "not json at all", "usage": {}, "latency_ms": 1}

        res = backfill_judgments(store, self.judge, BadClient(),
                                 tasks_dir=self.root / "tasks")
        self.assertEqual(res["judged"], 1)
        meta = store.list_runs()[0]
        self.assertIsNone(meta.judge_score)
        self.assertIsNone(meta.judge_passed)
        # and a second pass retries — inconclusive didn't lock the run
        res2 = backfill_judgments(RunStore(self.runs), self.judge,
                                  FakeJudgeClient(0.6), tasks_dir=self.root / "tasks")
        self.assertEqual(res2["judged"], 1)

    def test_skip_reconciles_index_with_report_verdict(self):
        """A run judged before the index had judge columns carries its verdict
        in report.json only — skipping it must still mirror the score into the
        index so aggregates can see it."""
        run_id = self._seed()
        store = RunStore(self.runs)
        run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
        report_path = run_dir / "report.json"
        report = json.loads(report_path.read_text())
        report["judge"] = {"score": 0.66, "passed": True, "reasoning": "pre-index"}
        report_path.write_text(json.dumps(report))

        client = FakeJudgeClient()
        res = backfill_judgments(store, self.judge, client,
                                 tasks_dir=self.root / "tasks")
        self.assertEqual(res["judged"], 0)
        self.assertEqual(client.calls, 0)
        meta = store.get_run(run_id)
        self.assertEqual(meta.judge_score, 0.66)
        self.assertTrue(meta.judge_passed)

    def test_dry_run_runs_never_reach_the_judge(self):
        """Dry-run stub artifacts are synthetic — spending real judge calls on
        them would pollute the corpus with verdicts on fake data."""
        self._seed(dry_run_meta=True)
        client = FakeJudgeClient()
        res = backfill_judgments(RunStore(self.runs), self.judge, client,
                                 tasks_dir=self.root / "tasks")
        self.assertEqual(res["runs_seen"], 0)
        self.assertEqual(client.calls, 0)

    def test_dry_run_writes_nothing(self):
        run_id = self._seed()
        store = RunStore(self.runs)
        res = backfill_judgments(store, self.judge, FakeJudgeClient(),
                                 dry_run=True, tasks_dir=self.root / "tasks")
        self.assertEqual(res["dry_run_judged"], 1)
        run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
        report = json.loads((run_dir / "report.json").read_text())
        self.assertNotIn("judge_backfill", report)


class TestJudgeInput(unittest.TestCase):
    def test_zip_listing_inlines_member_bodies(self):
        """Backfill sees the same bounded-content view the live path builds —
        a name-only listing let zip verdicts be computed blind."""
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "artifact.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("index.html", "<html>secret body</html>")
                zf.writestr("style.css", "body{}")
                zf.writestr("logo.bin", b"\xff\xfe\x00\x01")  # undecodable
            img, text, lang = _judge_input(Path(tmp))
            self.assertIsNone(img)
            self.assertEqual(lang, "text")
            self.assertIn("index.html", text)
            self.assertIn("secret body", text)   # real content reaches the judge
            self.assertIn("body{}", text)
            self.assertIn("logo.bin", text)      # binary keeps its header line
            self.assertNotIn("\\xff", text)

    def test_mp4_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "artifact.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
            with self.assertRaises(_NoJudgeableArtifact):
                _judge_input(Path(tmp))


class TestSpecAudit(unittest.TestCase):
    """Decisions-engine meta-audit of the task suite itself."""

    def _specs(self) -> list[TaskSpec]:
        return [
            TaskSpec(id="t-one", type="sql", prompt="q1",
                     metadata={"seed": [{"a": 1}], "reference_sql": "SELECT 1"}),
            TaskSpec(id="t-two", type="code", prompt="q2"),
        ]

    def test_dry_run_returns_rows_without_client(self):
        rows = judge.audit_specs(
            specs=self._specs(),
            judge=_model("~typesafe/jev-latest", "judge"),
            client=None, dry_run=True,
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["skipped"] == "dry-run" for r in rows))

    def test_non_decisions_judge_rejected(self):
        with self.assertRaises(ValueError):
            judge.audit_specs(
                specs=self._specs(),
                judge=_model("moonshotai/kimi-k2", "judge"),
                client=None, dry_run=False,
            )

    def test_audit_maps_typed_answers(self):
        class FakeDecisions:
            def decide(self, **kw):
                return {
                    "answers": {
                        "lowballs": {"noul": 0.7},
                        "sound": {"noul": 0.9},
                        "difficulty": {"score": 1.5, "confidence": 0.8},
                        "adversarial": {"score": 2.0},
                    },
                    "usage": {"cost": 0.0001},
                }

        rows = judge.audit_specs(
            specs=self._specs()[:1],
            judge=_model("~typesafe/jev-latest", "judge"),
            client=FakeDecisions(), dry_run=False, workers=1,
        )
        self.assertEqual(rows[0]["lowballs"], 0.7)
        self.assertEqual(rows[0]["sound"], 0.9)
        self.assertEqual(rows[0]["difficulty"], 1.5)
        self.assertEqual(rows[0]["adversarial"], 2.0)
        self.assertEqual(rows[0]["cost_usd"], 0.0001)

    def test_decide_error_is_row_not_crash(self):
        class Flaky:
            def decide(self, **kw):
                raise RuntimeError("provider 500")

        rows = judge.audit_specs(
            specs=self._specs()[:1],
            judge=_model("~typesafe/jev-latest", "judge"),
            client=Flaky(), dry_run=False, workers=1,
        )
        self.assertIn("error", rows[0])


class TestClaimsAudit(unittest.TestCase):
    """Decisions-engine audit of our own claims — the scorch battery."""

    def _claims(self) -> list[dict]:
        return [{
            "id": "c1", "statement": "the thing works",
            "context": "ctx", "evidence": {"n": 132},
            "caveats": ["small n"],
        }]

    def test_dry_run_needs_no_client(self):
        rows = judge.audit_claims(
            claims=self._claims(),
            judge=_model("~typesafe/jev-latest", "judge"),
            client=None, dry_run=True,
        )
        self.assertEqual(rows[0]["skipped"], "dry-run")

    def test_non_decisions_judge_rejected(self):
        with self.assertRaises(ValueError):
            judge.audit_claims(
                claims=self._claims(),
                judge=_model("moonshotai/kimi-k2", "judge"),
                client=None, dry_run=False,
            )

    def test_maps_verdict_fields(self):
        class FakeDecisions:
            def decide(self, **kw):
                return {
                    "answers": {
                        "supported": {"noul": 0.3, "confidence": 0.9},
                        "fatal_flaw": {"noul": 0.8},
                        "severity": {"score": 2.5},
                        "strength": {"score": 1.0},
                    },
                    "usage": {"cost": 0.0002},
                }

        rows = judge.audit_claims(
            claims=self._claims(),
            judge=_model("~typesafe/jev-latest", "judge"),
            client=FakeDecisions(), dry_run=False, workers=1,
        )
        r = rows[0]
        self.assertEqual(r["supported"], 0.3)
        self.assertEqual(r["fatal_flaw"], 0.8)
        self.assertEqual(r["severity"], 2.5)
        self.assertEqual(r["strength"], 1.0)
        self.assertEqual(r["confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()
