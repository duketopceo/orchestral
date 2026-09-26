"""Tests for the holdout arm: generation, seeding, publication, and reporting.

The load-bearing claims in this file are that a holdout arm is reproducible from
its seed, that two seeds give two different problems, and that a holdout run
cannot reach published output. Each is asserted against generated output rather
than restated in a docstring.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.audit import audit_suite, check_holdout_arm, default_holdout_probe, load_specs
from orchestral.config import ModelConfig, TaskSpec, load_task
from orchestral.holdout import (
    DEFAULT_SEED,
    HOLDOUT_TYPES,
    generate_arm,
    generate_spec,
    holdout_secrets,
    is_holdout,
    materialize,
    spec_seed,
)
from orchestral.privacy import HoldoutRunError, scrub_all, scrub_run
from orchestral.runner import Runner
from orchestral.sqlexec import run_sql_check
from orchestral.stats import contamination_gap, pairing_leaderboard
from orchestral.storage import RunMeta

# Enough seeds to make a "never collides" claim mean something without turning
# the suite into a fuzz run.
SEED_SWEEP = range(1, 13)


def _published_specs() -> list[TaskSpec]:
    root = Path(__file__).resolve().parent.parent / "tasks"
    return [spec for _, spec in load_specs(root)]


def _run(
    task_id: str,
    *,
    score: float | None,
    passes: bool | None,
    holdout: bool = False,
    task_type: str = "sql",
    status: str = "finished",
) -> RunMeta:
    return RunMeta(
        run_id=f"{task_id}-{int((score or 0) * 100)}",
        orchestrator="org/orch",
        task_id=task_id,
        worker="org/work",
        status=status,
        started_at="2026-01-01T00:00:00+00:00",
        score=score,
        passes=passes,
        config={"holdout": holdout, "task_type": task_type},
    )


class TestSeededGeneration(unittest.TestCase):
    def test_same_task_id_two_seeds_give_different_prompts(self):
        """The acceptance claim: --seed changes the problem, not just the label."""
        collisions = [
            (a, b, index)
            for a in SEED_SWEEP
            for b in SEED_SWEEP
            if a < b
            for index in range(12)
            if generate_spec(index, seed=a).prompt == generate_spec(index, seed=b).prompt
        ]
        self.assertEqual(collisions, [], f"seeds produced identical prompts: {collisions[:5]}")

    def test_task_id_is_independent_of_seed(self):
        for index in range(12):
            ids = {generate_spec(index, seed=s).id for s in SEED_SWEEP}
            self.assertEqual(len(ids), 1, f"slot {index} changed id across seeds: {ids}")

    def test_generation_is_deterministic_for_a_fixed_seed(self):
        for index in range(12):
            self.assertEqual(generate_spec(index, seed=99), generate_spec(index, seed=99))

    def test_spec_does_not_depend_on_arm_size_or_order(self):
        """A spec is a function of (seed, slot), so arm 4 and arm 40 agree."""
        big = generate_arm(40, seed=11)
        small = generate_arm(4, seed=11)
        self.assertEqual(big[:4], small)

    def test_every_generated_spec_is_marked_holdout(self):
        for spec in generate_arm(20, seed=3):
            self.assertTrue(is_holdout(spec), spec.id)
            self.assertIn(spec.type, HOLDOUT_TYPES, spec.id)

    def test_arm_spans_every_family_rather_than_one_template(self):
        """An arm built from one shape is one problem counted many times."""
        for size in (10, 20):
            shapes = {
                (spec.type, spec.metadata["generated_shape"])
                for spec in generate_arm(size, seed=4)
            }
            self.assertGreaterEqual(len(shapes), 4, f"only {shapes} at size {size}")

    def test_prompt_never_reveals_the_answer_key(self):
        for seed in SEED_SWEEP:
            for spec in generate_arm(12, seed=seed):
                for secret in holdout_secrets(spec):
                    head = secret.strip()[:60]
                    self.assertNotIn(head, spec.prompt, f"{spec.id} leaked its key into the prompt")

    def test_no_generated_prompt_duplicates_a_published_spec(self):
        published = {spec.prompt.strip() for spec in _published_specs()}
        for seed in (1, 7, DEFAULT_SEED):
            for spec in generate_arm(16, seed=seed):
                self.assertNotIn(spec.prompt.strip(), published, spec.id)

    def test_rejects_unknown_type_and_empty_arm(self):
        with self.assertRaises(ValueError):
            generate_spec(0, task_type="html")
        with self.assertRaises(ValueError):
            generate_arm(0)


class TestGeneratedSpecsAreAnswerable(unittest.TestCase):
    def test_sql_reference_query_matches_its_own_fixture(self):
        """A wrong key would grade every candidate wrong, so the key is checked."""
        for seed in range(1, 26):
            for spec in generate_arm(12, seed=seed):
                if spec.type != "sql":
                    continue
                report = run_sql_check(spec.metadata, str(spec.metadata["reference_sql"]))
                self.assertIsNone(report["error"], f"seed {seed} {spec.id}: {report['error']}")
                self.assertTrue(report["match"], f"seed {seed} {spec.id}")
                self.assertGreater(
                    report["rows_expected"] or 0, 0, f"seed {seed} {spec.id} has an empty answer"
                )

    def test_needle_answer_is_present_and_decoys_are_clean(self):
        for seed in range(1, 26):
            for spec in generate_arm(12, seed=seed):
                if spec.type != "needle":
                    continue
                meta = spec.metadata
                answer = meta["expected_answer"]
                self.assertIn(answer, meta["document"], f"seed {seed} {spec.id}")
                self.assertNotIn(answer, meta["forbidden"], f"seed {seed} {spec.id} is unanswerable")
                self.assertEqual(len(set(meta["forbidden"])), len(meta["forbidden"]))
                for decoy in meta["forbidden"]:
                    self.assertIn(decoy, meta["document"])
                self.assertEqual(meta["required"], [answer])

    def test_holdout_secrets_cover_the_key_and_nothing_else(self):
        sql = next(s for s in generate_arm(6, seed=2) if s.type == "sql")
        self.assertIn(str(sql.metadata["reference_sql"]), holdout_secrets(sql))
        needle = next(s for s in generate_arm(6, seed=2) if s.type == "needle")
        secrets = holdout_secrets(needle)
        self.assertIn(str(needle.metadata["expected_answer"]), secrets)
        for decoy in needle.metadata["forbidden"]:
            self.assertIn(decoy, secrets)

    def test_holdout_secrets_empty_for_a_published_spec(self):
        self.assertEqual(holdout_secrets(_published_specs()[0]), [])

    def test_spec_seed_reports_the_seed_the_data_came_from(self):
        self.assertEqual(spec_seed(generate_spec(0, seed=77)), 77)
        self.assertIsNone(spec_seed(_published_specs()[0]))

    def test_spec_seed_ignores_a_non_integer(self):
        spec = generate_spec(0, seed=1)
        spec.metadata["generated_seed"] = "not-a-seed"
        self.assertIsNone(spec_seed(spec))
        spec.metadata["generated_seed"] = True
        self.assertIsNone(spec_seed(spec), "bool is an int subclass and is not a seed")


class TestMaterialize(unittest.TestCase):
    def test_round_trips_through_the_loader(self):
        specs = generate_arm(6, seed=8)
        with tempfile.TemporaryDirectory() as tmp:
            written = materialize(specs, Path(tmp) / "arm")
            self.assertEqual(len(written), 6)
            for path, original in zip(written, specs, strict=True):
                loaded = load_task(path)
                self.assertEqual(loaded.id, original.id)
                self.assertEqual(loaded.prompt, original.prompt)
                self.assertTrue(is_holdout(loaded))
                self.assertEqual(loaded.metadata["reference_sql"] if loaded.type == "sql" else
                                 loaded.metadata["expected_answer"],
                                 original.metadata["reference_sql"] if original.type == "sql" else
                                 original.metadata["expected_answer"])

    def test_refuses_to_materialize_a_published_spec(self):
        published = _published_specs()[0]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            materialize([published], Path(tmp) / "arm")

    def test_materialized_arm_never_lands_in_the_task_tree(self):
        """The generator writes only where it is told — the repo is not that place."""
        specs = generate_arm(4, seed=6)
        with tempfile.TemporaryDirectory() as tmp:
            arm = Path(tmp) / "nested" / "arm"
            materialize(specs, arm)
            repo_tasks = Path(__file__).resolve().parent.parent / "tasks"
            generated_ids = {s.id for s in specs}
            committed = {spec.id for _, spec in load_specs(repo_tasks)}
            self.assertEqual(generated_ids & committed, set())


def _write_run(root: Path, rel: str, *, holdout: bool, payload: dict[str, str]) -> Path:
    run_dir = root / rel
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": rel.replace("/", "-"),
        "orchestrator": "org/orch",
        "task_id": "t",
        "worker": "org/work",
        "status": "finished",
        "score": 1.0,
        "passes": True,
    }))
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": rel.replace("/", "-"), "task_id": "t", "holdout": holdout,
    }))
    # plan.json is where the task prompt lands in a real run
    (run_dir / "plan.json").write_text(json.dumps({"plan": payload["plan"]}))
    (run_dir / "report.json").write_text(json.dumps({"checks": payload["report"]}))
    (run_dir / "worker-0.json").write_text(json.dumps({"content": payload["artifact"]}))
    return run_dir


class TestHoldoutNeverPublishes(unittest.TestCase):
    def test_holdout_run_is_absent_from_scrub_output(self):
        """The acceptance claim, asserted against the bytes scrub actually wrote."""
        needle = next(s for s in generate_arm(6, seed=5) if s.type == "needle")
        answer = str(needle.metadata["expected_answer"])
        sql = next(s for s in generate_arm(6, seed=5) if s.type == "sql")
        key_sql = str(sql.metadata["reference_sql"]).strip()

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            pub = Path(tmp) / "runs-pub"
            _write_run(runs, "o/pub/w/1", holdout=False, payload={
                "plan": "a published plan", "report": "{}", "artifact": "published output",
            })
            _write_run(runs, "o/hold/w/1", holdout=True, payload={
                "plan": f"find the token {answer}",
                "report": json.dumps({"expected": answer}),
                "artifact": answer,
            })
            _write_run(runs, "o/hold/w/2", holdout=True, payload={
                "plan": "write the query", "report": key_sql, "artifact": "SELECT 1",
            })

            copied = scrub_all(runs, pub)

            self.assertEqual(len(copied), 1, "only the published run should be copied")
            self.assertFalse((pub / "o" / "hold").exists(), "holdout tree reached published output")
            published_bytes = b"".join(
                p.read_bytes() for p in sorted(pub.rglob("*")) if p.is_file()
            ).decode("utf-8", "replace")
            self.assertNotIn(answer, published_bytes, "holdout answer key was published")
            self.assertNotIn(key_sql, published_bytes, "holdout reference query was published")
            self.assertIn("a published plan", published_bytes, "the published run went missing")

    def test_withheld_runs_are_labelled_in_the_published_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            pub = Path(tmp) / "runs-pub"
            _write_run(runs, "o/hold/w/1", holdout=True, payload={
                "plan": "p", "report": "{}", "artifact": "a",
            })
            scrub_all(runs, pub)
            manifest = json.loads((pub / "manifest.json").read_text())
            self.assertEqual(len(manifest), 1)
            entry = manifest[0]
            self.assertIn("withheld", entry)
            self.assertEqual(entry["withheld"]["status"], "holdout_not_published")
            self.assertIsNone(entry["run"], "a withheld run must not point at a published path")
            self.assertEqual(entry["withheld"]["source_run"], "o/hold/w/1")

    def test_scrub_run_refuses_a_holdout_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = _write_run(root / "runs", "o/hold/w/1", holdout=True, payload={
                "plan": "p", "report": "{}", "artifact": "a",
            })
            with self.assertRaises(HoldoutRunError):
                scrub_run(run_dir, root / "pub")

    def test_a_run_with_no_manifest_is_not_treated_as_holdout(self):
        """Withholding is for arms we can identify, not a blanket cull."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "o" / "t" / "w" / "1"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps({"run_id": "1", "status": "finished"}))
            self.assertEqual(len(scrub_all(root / "runs", root / "pub")), 1)


class TestRunRecordsTheArm(unittest.TestCase):
    """A run has to carry its arm membership and the seed behind its task data."""

    def _run_once(self, spec: TaskSpec, runs_dir: Path, seed: int | None = None) -> dict:
        model = ModelConfig(
            slug="org/model", name="org/model", role="orchestrator",
            input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
        )
        runner = Runner(dry_run=True, runs_dir=runs_dir, seed=seed)
        meta = runner.run(spec, model, model)
        run_json = json.loads((Path(meta.run_dir) / "run.json").read_text())
        manifest = json.loads((Path(meta.run_dir) / "manifest.json").read_text())
        return {"run": run_json, "manifest": manifest}

    def test_generated_spec_records_its_own_seed_and_the_holdout_flag(self):
        spec = generate_spec(0, seed=4242)
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_once(spec, Path(tmp))
        self.assertEqual(out["run"]["config"]["holdout"], True)
        self.assertEqual(out["run"]["config"]["task_type"], spec.type)
        self.assertEqual(out["run"]["config"]["seed"], 4242)
        self.assertTrue(out["manifest"]["holdout"])

    def test_an_explicit_seed_still_wins(self):
        spec = generate_spec(0, seed=4242)
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_once(spec, Path(tmp), seed=7)
        self.assertEqual(out["run"]["config"]["seed"], 7)

    def test_a_published_spec_records_no_arm_and_no_seed(self):
        published = _published_specs()[0]
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_once(published, Path(tmp))
        self.assertEqual(out["run"]["config"]["holdout"], False)
        self.assertIsNone(out["run"]["config"]["seed"])
        self.assertFalse(out["manifest"]["holdout"])


class TestLeaderboardArmVisibility(unittest.TestCase):
    def test_holdout_runs_are_excluded_from_every_published_figure(self):
        runs = [
            _run("published-1", score=1.0, passes=True),
            _run("published-2", score=1.0, passes=True),
            _run("holdout-1", score=0.0, passes=False, holdout=True),
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertEqual(row.runs, 2)
        self.assertEqual(row.score_mean, 1.0)
        self.assertEqual(row.pass_rate, 1.0)
        self.assertEqual(row.holdout_runs, 1)

    def test_a_holdout_only_pairing_stays_visible(self):
        """A filtered pairing must not look like one that was never measured."""
        rows = pairing_leaderboard([_run("holdout-1", score=0.0, passes=False, holdout=True)])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].holdout_only)
        self.assertEqual(rows[0].runs, 0)
        self.assertEqual(rows[0].holdout_runs, 1)


class TestContaminationGap(unittest.TestCase):
    def test_gap_is_published_mean_minus_holdout_mean_within_a_type(self):
        rows = contamination_gap([
            _run("p1", score=1.0, passes=True, task_type="sql"),
            _run("p2", score=0.5, passes=True, task_type="sql"),
            _run("h1", score=0.0, passes=False, holdout=True, task_type="sql"),
        ])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row.comparable)
        self.assertAlmostEqual(row.published_mean, 0.75)
        self.assertAlmostEqual(row.holdout_mean, 0.0)
        self.assertAlmostEqual(row.gap, 0.75)
        self.assertEqual((row.published_n, row.holdout_n), (2, 1))

    def test_a_type_missing_from_one_arm_has_no_gap(self):
        """Across types the difference is subject, not contamination."""
        rows = contamination_gap([
            _run("p1", score=1.0, passes=True, task_type="html"),
            _run("h1", score=0.0, passes=False, holdout=True, task_type="sql"),
        ])
        by_type = {r.task_type: r for r in rows}
        self.assertFalse(by_type["html"].comparable)
        self.assertIsNone(by_type["html"].gap)
        self.assertEqual(by_type["html"].holdout_n, 0)
        self.assertFalse(by_type["sql"].comparable)
        self.assertIsNone(by_type["sql"].gap)

    def test_unfinished_and_unscored_runs_are_left_out(self):
        rows = contamination_gap([
            _run("p1", score=1.0, passes=True, task_type="sql"),
            _run("p2", score=1.0, passes=True, task_type="sql", status="running"),
            _run("p3", score=None, passes=None, task_type="sql"),
        ])
        self.assertEqual(rows[0].published_n, 1)

    def test_pass_fail_only_runs_still_count(self):
        rows = contamination_gap([
            _run("p1", score=None, passes=True, task_type="needle"),
            _run("h1", score=None, passes=False, holdout=True, task_type="needle"),
        ])
        self.assertEqual(rows[0].published_n, 1)
        self.assertAlmostEqual(rows[0].gap, 1.0)

    def test_no_runs_yields_no_rows(self):
        self.assertEqual(contamination_gap([]), [])


class TestAuditArmRule(unittest.TestCase):
    def test_a_generated_arm_satisfies_the_rule(self):
        published = _published_specs()
        # the shipped tree has no committed holdout, so the rule stands until a
        # real arm is attached
        self.assertEqual([f.rule for f in check_holdout_arm(published)], ["no_holdout_arm"])
        self.assertEqual(check_holdout_arm(published, probe=default_holdout_probe), [])

    def test_a_committed_holdout_spec_satisfies_the_rule(self):
        spec = _published_specs()[0]
        spec.metadata["holdout"] = True
        try:
            self.assertEqual(check_holdout_arm([spec]), [])
        finally:
            spec.metadata.pop("holdout")

    def test_the_rule_fails_closed_when_there_is_no_arm(self):
        findings = check_holdout_arm(_published_specs())
        self.assertEqual([f.rule for f in findings], ["no_holdout_arm"])

    def test_a_broken_generator_does_not_count_as_an_arm(self):
        def broken() -> list[TaskSpec]:
            raise RuntimeError("generator exploded")

        findings = check_holdout_arm(_published_specs(), probe=broken)
        self.assertEqual([f.rule for f in findings], ["no_holdout_arm"])
        self.assertIn("generator exploded", findings[0].detail)

    def test_a_generator_that_replays_published_prompts_is_not_an_arm(self):
        published = _published_specs()
        findings = check_holdout_arm(published, probe=lambda: list(published[:2]))
        self.assertEqual([f.rule for f in findings], ["no_holdout_arm"])

    def test_a_generator_that_forgets_the_holdout_flag_is_not_an_arm(self):
        def unmarked() -> list[TaskSpec]:
            return [TaskSpec(id="x", type="sql", prompt="a brand new prompt", metadata={})]

        findings = check_holdout_arm(_published_specs(), probe=unmarked)
        self.assertEqual([f.rule for f in findings], ["no_holdout_arm"])

    def test_shipped_tree_reports_no_errors_with_the_generator_attached(self):
        report = audit_suite(_published_specs(), holdout_probe=default_holdout_probe)
        self.assertTrue(report.ok, report.by_rule().get("unknown_validation_check"))
        self.assertNotIn("no_holdout_arm", report.by_rule())


if __name__ == "__main__":
    unittest.main()
