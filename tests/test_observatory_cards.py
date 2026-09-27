"""Story-envelope contracts for observatory cards, lenses, and proof."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig
from orchestral.judge import draft_thread
from orchestral.storage import RunStore
from orchestral.web import state


def _add_run(
    store: RunStore,
    *,
    group: str = "g1",
    orchestrator: str = "o/one",
    worker: str = "w/one",
    task_id: str = "t-code",
    passes: bool = True,
    judge_score: float | None = None,
    judge_passed: bool | None = None,
    cost_usd: float = 0.01,
    replicate: int | None = None,
    started_at: str = "2026-09-23T00:00:00+00:00",
    artifact: str | None = "artifact.py",
    artifact_body: str = "def answer():\n    return 42\n",
    execution_output: str | None = "Ran 2 tests in 0.01s\n\nOK\n",
) -> str:
    run_id, run_dir = store.new_run(
        orchestrator,
        task_id,
        worker,
        {"dry_run": False},
        run_group=group,
        replicate=replicate,
    )
    meta = store.get_run(run_id)
    assert meta is not None
    meta.status = "finished"
    meta.passes = passes
    meta.score = 1.0 if passes else 0.0
    meta.judge_score = judge_score
    meta.judge_passed = judge_passed
    meta.total_cost_usd = cost_usd
    meta.started_at = started_at
    meta.finished_at = started_at
    report: dict = {
        "passes": passes,
        "score": meta.score,
        "checks": {"tests_pass": passes},
    }
    if judge_score is not None:
        report["judge"] = {
            "score": judge_score,
            "passed": judge_passed,
            "model": "judge/model",
        }
    if execution_output is not None:
        report["execution"] = {
            "executed": True,
            "tests_run": 2,
            "ok": passes,
            "output_tail": execution_output,
        }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if artifact is not None:
        (run_dir / artifact).write_text(artifact_body, encoding="utf-8")
    store.update_meta(meta)
    return run_id


def _seed_lens_cohort(store: RunStore) -> None:
    for i in range(3):
        _add_run(
            store,
            orchestrator="o/reliable",
            worker="w/cheap",
            passes=True,
            judge_score=0.82,
            judge_passed=True,
            cost_usd=0.03,
            started_at=f"2026-09-23T01:0{i}:00+00:00",
        )
    for i in range(3):
        _add_run(
            store,
            orchestrator="o/reliable",
            worker="w/frontier",
            passes=i < 2,
            judge_score=0.9,
            judge_passed=True,
            cost_usd=0.03,
            started_at=f"2026-09-23T02:0{i}:00+00:00",
        )
    for i in range(3):
        _add_run(
            store,
            orchestrator="o/divergent",
            worker="w/semantic",
            passes=i == 0,
            judge_score=0.7,
            judge_passed=True,
            cost_usd=0.18,
            started_at=f"2026-09-23T03:0{i}:00+00:00",
        )
    _add_run(
        store,
        orchestrator="o/thin",
        worker="w/thin",
        passes=True,
        judge_score=1.0,
        judge_passed=True,
        cost_usd=0.0,
        started_at="2026-09-23T04:00:00+00:00",
    )


class TestCardLenses(unittest.TestCase):
    def test_lenses_rank_named_evidence_without_a_composite_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _seed_lens_cohort(store)

            payload = state.pairings_payload(store, group="g1")
            by_id = {lens["id"]: lens for lens in payload["lenses"]}

            self.assertEqual(
                set(by_id),
                {"overall", "high_cost", "low_cost", "sweet_spot", "divergence"},
            )
            self.assertEqual(by_id["overall"]["selected_target"], "o/reliable|w/cheap")
            self.assertEqual(by_id["divergence"]["selected_target"], "o/divergent|w/semantic")
            self.assertNotIn("o/thin|w/thin", by_id["overall"]["ranking"])
            for lens in payload["lenses"]:
                self.assertLessEqual(len(lens["ranking"]), 3)
                if lens["selected_target"]:
                    self.assertTrue(lens["reason"])
                else:
                    self.assertTrue(lens["empty_reason"])

    def test_pairing_annotations_round_trip_for_gallery_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.set_annotation("pairing", "o/one|w/one", "interesting", "candidate")
            self.assertEqual(store.annotations()[0]["flag"], "interesting")

    def test_card_catalog_filters_group_scope_and_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _seed_lens_cohort(store)
            store.set_annotation("group", "g1", "interesting", "keep this story")

            catalog = state.card_catalog_payload(
                store, group="g1", scope="group", lens="divergence",
            )

            self.assertEqual(len(catalog["cards"]), 1)
            self.assertEqual(catalog["cards"][0]["story"]["lens"]["id"], "divergence")
            self.assertEqual(catalog["filters"]["group"], "g1")
            flagged = state.card_catalog_payload(
                store, group="g1", scope="group", flagged=True,
            )
            self.assertEqual([card["flag"] for card in flagged["cards"]], ["interesting"])

    def test_ungrouped_sentinel_is_a_real_catalog_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _add_run(store, group=None)
            catalog = state.card_catalog_payload(
                store, group="(ungrouped)", scope="pairing",
            )
            self.assertEqual(len(catalog["cards"]), 1)
            self.assertEqual(catalog["cards"][0]["story"]["scope"], "pairing")


class TestStoryEnvelope(unittest.TestCase):
    def test_group_story_keeps_cohort_metrics_lens_signals_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _seed_lens_cohort(store)

            first = state.card_payload(store, "group", "g1", lens="divergence")
            second = state.card_payload(store, "group", "g1", lens="divergence")

            assert first is not None
            self.assertEqual(first["story"], second["story"])
            story = first["story"]
            self.assertEqual(story["scope"], "group")
            self.assertEqual(story["lens"]["id"], "divergence")
            self.assertEqual(story["cohort"]["orchestrators"], 3)
            self.assertEqual(story["cohort"]["workers"], 4)
            self.assertEqual(story["cohort"]["pairings"], 4)
            self.assertEqual(story["cohort"]["runs"], 10)
            self.assertIn("axis_divergence", {s["id"] for s in story["signals"]})
            self.assertTrue(story["proof"]["representative"])
            self.assertIn(story["proof"]["status"], {"available", "partial"})
            self.assertTrue(story["best_task"]["inspect_url"].startswith("/#/run/"))
            self.assertLessEqual(len(story["caption"]), 270)

    def test_run_story_uses_the_run_itself_as_non_representative_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _add_run(
                store,
                passes=True,
                judge_score=0.9,
                judge_passed=False,
                execution_output="Ran 1 test in 0.01s\n\nFAILED\n",
            )

            card = state.card_payload(store, "run", run_id, lens="overall")

            assert card is not None
            self.assertEqual(card["story"]["scope"], "run")
            self.assertEqual(card["story"]["proof"]["run_id"], run_id)
            self.assertFalse(card["story"]["proof"]["representative"])
            self.assertIn("axis_divergence", {s["id"] for s in card["story"]["signals"]})

    def test_group_story_reports_task_specialization_from_task_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            for task_id, passes in (("t-easy", True), ("t-hard", False)):
                hour = "05" if task_id == "t-easy" else "06"
                for index in range(3):
                    _add_run(store, task_id=task_id, passes=passes,
                             started_at=f"2026-09-23T{hour}:0{index}:00+00:00")

            card = state.card_payload(store, "group", "g1", lens="overall")

            assert card is not None
            signal = next(s for s in card["story"]["signals"] if s["id"] == "task_specialist")
            self.assertIn("t-easy", signal["claim"])
            self.assertIn("t-hard", signal["claim"])

    def test_missing_artifact_degrades_to_partial_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _add_run(store, artifact=None)
            card = state.card_payload(store, "run", run_id)

            assert card is not None
            self.assertEqual(card["story"]["proof"]["status"], "partial")
            self.assertIsNone(card["story"]["proof"]["artifact"])


class TestRunEvidence(unittest.TestCase):
    def test_evidence_is_bounded_and_uses_a_preferred_artifact_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _add_run(
                store,
                execution_output="\n".join(f"test_{i} ... ok" for i in range(80)) + "\nOK\n",
                artifact="artifact.zip",
                artifact_body="ignored",
            )
            run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
            import zipfile

            with zipfile.ZipFile(run_dir / "artifact.zip", "w") as archive:
                archive.writestr("README.md", "read me")
                archive.writestr("service.py", "print('hello')\n" * 80)
                archive.writestr("src/worker.py", "print('nested')\n")

            evidence = state.run_evidence_payload(store, run_id, max_bytes=400, max_lines=12)

            assert evidence is not None
            self.assertLessEqual(len(evidence["transcript"]["text"].encode()), 400)
            self.assertLessEqual(len(evidence["transcript"]["text"].splitlines()), 12)
            self.assertEqual(evidence["artifact"]["name"], "service.py")
            self.assertLessEqual(len(evidence["artifact"]["preview"].encode()), 400)
            self.assertNotIn(str(run_dir), json.dumps(evidence))

    def test_nested_archive_member_has_an_encoded_inspectable_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _add_run(store, artifact="artifact.zip", artifact_body="ignored")
            run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
            import zipfile

            with zipfile.ZipFile(run_dir / "artifact.zip", "w") as archive:
                archive.writestr("src/worker.py", "print('nested')\n")

            evidence = state.run_evidence_payload(store, run_id)

            assert evidence is not None
            self.assertEqual(evidence["artifact"]["name"], "src/worker.py")
            self.assertIn("src%2Fworker.py", evidence["artifact"]["url"])

    def test_binary_artifact_has_no_decorative_text_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _add_run(
                store,
                artifact="artifact.png",
                artifact_body="\x89PNG\r\n\x1a\nfake",
            )
            evidence = state.run_evidence_payload(store, run_id)

            assert evidence is not None
            self.assertEqual(evidence["artifact"]["media_type"], "image")
            self.assertIsNone(evidence["artifact"]["preview"])
            self.assertTrue(evidence["artifact"]["url"].endswith("/artifact"))

    def test_empty_artifact_is_partial_and_event_evidence_omits_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _add_run(
                store,
                artifact="artifact.txt",
                artifact_body="",
                execution_output=None,
            )
            run_dir = Path(store.get_run(run_id).run_dir)  # type: ignore[union-attr]
            (run_dir / "events.jsonl").write_text(
                json.dumps({
                    "timestamp": "2026-09-23T00:00:01+00:00",
                    "type": "artifact.saved",
                    "worker_id": None,
                    "output": {"path": str(run_dir / "artifact.txt")},
                }) + "\n",
                encoding="utf-8",
            )

            evidence = state.run_evidence_payload(store, run_id)

            assert evidence is not None
            self.assertEqual(evidence["status"], "partial")
            self.assertEqual(evidence["artifact"]["bytes"], 0)
            self.assertIsNone(evidence["artifact"]["preview"])
            self.assertNotIn(str(run_dir), json.dumps(evidence))

    def test_verdict_only_judgment_counts_in_pairing_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _add_run(store)
            meta = store.get_run(run_id)
            assert meta is not None
            report = json.loads((Path(meta.run_dir) / "report.json").read_text(encoding="utf-8"))
            report["judge"] = {"passed": True, "model": "judge/verdict-only"}
            (Path(meta.run_dir) / "report.json").write_text(json.dumps(report), encoding="utf-8")
            meta.judge_passed = True
            store.update_meta(meta)

            card = state.card_payload(store, "pairing", "o/one|w/one")

            assert card is not None
            self.assertEqual(card["judged"], 1)
            self.assertEqual(card["judge_approved"], 1)
            self.assertEqual(card["judge_pass_rate"], 1.0)


class _Writer:
    def __init__(self):
        self.messages = []

    def chat(self, **kwargs):
        self.messages.append(kwargs)
        return {"content": json.dumps({"posts": ["Measured result.", "Signal.", "Caveat."]})}


class TestSignalAwareThread(unittest.TestCase):
    def test_writer_receives_shared_signals_and_templates_use_the_same_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _seed_lens_cohort(store)
            card = state.card_payload(store, "group", "g1", lens="divergence")
            assert card is not None

            fallback = draft_thread(card=card, client=None, model=None, n=3)
            writer = _Writer()
            model = ModelConfig(
                slug="writer/model",
                name="writer",
                role="writer",
                input_price_per_mtok=0,
                output_price_per_mtok=0,
            )
            generated = draft_thread(card=card, client=writer, model=model, n=3)

            self.assertTrue(fallback["templated"])
            self.assertIn(card["story"]["claim"], fallback["posts"][1])
            self.assertTrue(all(len(post) <= 270 for post in fallback["posts"]))
            run_fallback = draft_thread(card=state.card_payload(
                store, "run", store.list_runs(limit=1)[0].run_id,
            ), client=None, model=None, n=2)
            self.assertIn("Cost $", run_fallback["posts"][1])
            self.assertFalse(generated["templated"])
            prompt = writer.messages[0]["messages"][0]["content"]
            self.assertIn("story.signals", prompt)
            self.assertIn("axis_divergence", prompt)
            self.assertNotIn("transcript\": {\"text", prompt)


if __name__ == "__main__":
    unittest.main()
