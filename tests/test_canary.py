"""Contamination canaries: token format, spec stamping, echo detection, audit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestral.audit import check_canary, check_canary_collisions
from orchestral.canary import (
    CANARY_RE,
    canary_echoes,
    canary_index,
    canary_token,
    spec_canary,
)
from orchestral.config import TaskSpec
from orchestral.holdout import generate_spec
from orchestral.storage import RunMeta


def _spec(sid: str, canary: str | None = None, prompt: str = "do the thing",
          metadata: dict | None = None) -> TaskSpec:
    meta = dict(metadata or {})
    if canary is not None:
        meta["canary"] = canary
    return TaskSpec(id=sid, type="sql", prompt=prompt, metadata=meta)


def _run(rid: str, run_dir: Path, task: str = "t-a",
         config: dict | None = None) -> RunMeta:
    return RunMeta(
        run_id=rid, orchestrator="o/m", task_id=task, worker="w/m",
        status="finished", started_at="2026-10-05T00:00:00",
        total_cost_usd=0.01, total_input_tokens=1, total_output_tokens=1,
        score=0.5, passes=True, latency_ms=1.0, run_group="g",
        run_dir=str(run_dir), config=config or {})


class TestToken(unittest.TestCase):
    def test_format_and_determinism(self):
        t = canary_token("sql-monthly-revenue")
        self.assertTrue(CANARY_RE.fullmatch(t))
        self.assertEqual(t, canary_token("sql-monthly-revenue"))
        self.assertNotEqual(t, canary_token("other-task"))

    def test_spec_canary_rejects_malformed(self):
        self.assertIsNone(spec_canary(_spec("s", "nope")))
        self.assertIsNotNone(spec_canary(_spec("s", canary_token("s"))))

    def test_index_maps_canary_to_owner(self):
        specs = [_spec("a", canary_token("a")), _spec("b")]
        index = canary_index(specs)
        self.assertEqual(index, {canary_token("a"): "a"})


class TestEchoes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write_run(self, rid: str, task: str, artifact_text: str) -> RunMeta:
        run_dir = self.root / rid
        run_dir.mkdir()
        (run_dir / "artifact.txt").write_text(artifact_text)
        return _run(rid, run_dir, task)

    def test_foreign_echo_flagged(self):
        token = canary_token("t-secret")
        index = {token: "t-secret"}
        run = self._write_run("r1", "t-a", f"the answer mentions {token} ok")
        hits = canary_echoes([run], index)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "foreign")
        self.assertEqual(hits[0].owner_task_id, "t-secret")

    def test_own_echo_flagged(self):
        token = canary_token("t-a")
        index = {token: "t-a"}
        run = self._write_run("r1", "t-a", f"echoing {token}")
        hits = canary_echoes([run], index)
        self.assertEqual(hits[0].kind, "own")

    def test_config_canary_classifies_holdout_runs(self):
        """A holdout spec lives nowhere on disk; the run's own config must
        carry its canary for the echo to classify as 'own', not 'unknown'."""
        token = canary_token("holdout:7:3")
        run_dir = self.root / "r1"
        run_dir.mkdir()
        (run_dir / "artifact.sql").write_text(f"select '{token}'")
        run = _run("r1", run_dir, task="holdout-0003",
                   config={"task_canary": token})
        hits = canary_echoes([run], {})
        self.assertEqual(hits[0].kind, "own")

    def test_unknown_token_still_flagged(self):
        run = self._write_run("r1", "t-a", "it said orc-canary-deadbeefcafe1234")
        hits = canary_echoes([run], {})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "unknown")
        self.assertIsNone(hits[0].owner_task_id)

    def test_clean_artifact_no_hits(self):
        run = self._write_run("r1", "t-a", "ordinary deliverable text")
        self.assertEqual(canary_echoes([run], canary_index([_spec("t-a", canary_token("t-a"))])), [])

    def test_unfinished_and_binary_skipped(self):
        run_dir = self.root / "r1"
        run_dir.mkdir()
        (run_dir / "artifact.zip").write_text("orc-canary-deadbeefcafe1234")
        (run_dir / "artifact.txt").write_text("clean")
        run = _run("r1", run_dir)
        self.assertEqual(canary_echoes([run], {}), [])

    def test_duplicate_token_in_text_counts_once(self):
        token = canary_token("t-a")
        run = self._write_run("r1", "t-a", f"{token} and again {token}")
        self.assertEqual(len(canary_echoes([run], {token: "t-a"})), 1)


class TestAudit(unittest.TestCase):
    def test_missing_canary_warns(self):
        found = check_canary(_spec("s-1"))
        self.assertEqual([f.rule for f in found], ["missing_canary"])
        self.assertEqual(found[0].severity, "warn")

    def test_malformed_canary_errors(self):
        found = check_canary(_spec("s-1", canary="hello"))
        self.assertEqual(found[0].rule, "malformed_canary")
        self.assertEqual(found[0].severity, "error")

    def test_canary_in_prompt_errors(self):
        token = canary_token("s-1")
        found = check_canary(_spec("s-1", canary=token, prompt=f"print {token}"))
        self.assertEqual(found[0].rule, "canary_in_prompt")
        self.assertEqual(found[0].severity, "error")

    def test_collision_errors(self):
        token = canary_token("shared")
        found = check_canary_collisions([
            _spec("a", token), _spec("b", token), _spec("c", canary_token("c"))])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule, "duplicate_canary")

    def test_committed_suite_has_unique_canaries(self):
        from orchestral.audit import load_specs
        specs = [s for _, s in load_specs("tasks")]
        missing = [s.id for s in specs if not spec_canary(s)]
        self.assertEqual(missing, [])
        self.assertEqual(check_canary_collisions(specs), [])


class TestHoldoutStamp(unittest.TestCase):
    def test_generated_spec_carries_canary(self):
        spec = generate_spec(0, seed=20260926)
        token = spec_canary(spec)
        self.assertIsNotNone(token)
        self.assertEqual(token, canary_token("holdout:20260926:0"))

    def test_canary_deterministic_per_seed_index(self):
        self.assertEqual(spec_canary(generate_spec(0, seed=1)),
                         spec_canary(generate_spec(0, seed=1)))
        self.assertNotEqual(spec_canary(generate_spec(0, seed=1)),
                            spec_canary(generate_spec(0, seed=2)))
        self.assertNotEqual(spec_canary(generate_spec(0, seed=1)),
                            spec_canary(generate_spec(1, seed=1)))

    def test_canary_not_in_prompt(self):
        for i in range(8):
            spec = generate_spec(i, seed=1)
            self.assertNotIn(spec_canary(spec) or "", spec.prompt)


if __name__ == "__main__":
    unittest.main()
