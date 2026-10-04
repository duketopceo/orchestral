"""Tests for judge calibration — agreement metrics and label joining."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from orchestral.calibrate import (
    agreement_metrics,
    calibration_status,
    cohens_kappa,
    collect_pairs,
    emit_label_skeleton,
    load_labels,
    pearson,
    persist_calibration,
    spearman,
)
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task() -> TaskSpec:
    return TaskSpec(
        id="cal-test", type="html", prompt="Make a page.",
        validation=["non_empty"],
    )


class _JudgeClient:
    """Chat stand-in: plan → one subtask, worker → html, judge → 0.8/pass."""

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = "<html><body>page</body></html>"
        elif "candidates" in data or "prompt" in data:
            body = json.dumps({"subtasks": [{"id": 0, "description": "page"}]})
        else:
            body = json.dumps({"score": 0.8, "passed": True, "reasoning": "decent"})
        return {
            "content": body,
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            "latency_ms": 1, "id": "fake",
        }

    def close(self):
        pass


class TestMetrics(unittest.TestCase):
    def test_pearson_perfect(self):
        self.assertAlmostEqual(pearson([1, 2, 3], [2, 4, 6]), 1.0)

    def test_pearson_constant_input_none(self):
        self.assertIsNone(pearson([1, 1, 1], [1, 2, 3]))

    def test_spearman_handles_ties(self):
        rho = spearman([1, 1, 2, 3], [10, 10, 20, 30])
        self.assertAlmostEqual(rho, 1.0)

    def test_kappa_perfect_and_chance(self):
        self.assertAlmostEqual(cohens_kappa([True, False], [True, False]), 1.0)
        # judge always predicts True against a 50/50 truth → agreement at chance
        self.assertAlmostEqual(
            cohens_kappa([True, False, True, False], [True] * 4), 0.0
        )

    def test_score_metrics(self):
        pairs = [
            {"human_score": 1.0, "judge_score": 0.9, "human_passed": True, "judge_passed": True},
            {"human_score": 0.0, "judge_score": 0.2, "human_passed": False, "judge_passed": True},
        ]
        m = agreement_metrics(pairs)
        self.assertAlmostEqual(m["score"]["mae"], 0.15)
        self.assertEqual(m["verdict"]["n"], 2)
        self.assertEqual(m["verdict"]["fp"], 1)
        self.assertAlmostEqual(m["verdict"]["accuracy"], 0.5)

    def test_partial_coverage_only_counts_both_sides(self):
        pairs = [
            {"human_score": 1.0, "judge_score": None, "human_passed": True, "judge_passed": None},
        ]
        m = agreement_metrics(pairs)
        self.assertIsNone(m["score"])
        self.assertIsNone(m["verdict"])


class TestCollectPairs(unittest.TestCase):
    def _judged_run(self, tmp: str):
        client = _JudgeClient()
        meta = Runner(
            runs_dir=tmp, planner="raw",
            clients={"orchestrator": client, "worker": client, "judge": client},
        ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/y", "worker"),
              judge=_model("j/judge", "judge"))
        return meta

    def test_joins_labels_to_judge_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._judged_run(tmp)
            labels = [{"run_id": meta.run_id, "score": 0.9, "passed": True}]
            result = collect_pairs(RunStore(tmp), labels)
            self.assertEqual(result["coverage"]["matched"], 1)
            pair = result["pairs"][0]
            self.assertEqual(pair["judge_score"], 0.8)
            self.assertTrue(pair["judge_passed"])
            m = agreement_metrics(result["pairs"])
            self.assertAlmostEqual(m["score"]["mae"], 0.1)
            self.assertEqual(m["verdict"]["accuracy"], 1.0)

    def test_unmatched_labels_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._judged_run(tmp)
            result = collect_pairs(RunStore(tmp), [{"run_id": "doesnotexist"}])
            self.assertEqual(result["coverage"]["matched"], 0)
            self.assertEqual(result["coverage"]["unmatched"], ["doesnotexist"])

    def test_duplicate_labels_deduped_on_resolved_run(self):
        """Two label rows resolving to the same run count once — the first
        wins, the rest surface in coverage so nothing is silently dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._judged_run(tmp)
            labels = [
                {"run_id": meta.run_id, "passed": True},
                {"run_id": meta.run_id[:8], "passed": False},  # same run, prefix
            ]
            result = collect_pairs(RunStore(tmp), labels)
            self.assertEqual(len(result["pairs"]), 1)
            self.assertEqual(result["pairs"][0]["run_id"], meta.run_id)
            self.assertTrue(result["pairs"][0]["human_passed"])  # first wins
            self.assertEqual(result["coverage"]["duplicates"], [meta.run_id])

    def test_string_verdicts_coerce_explicitly(self):
        """bool("false") is True — label verdicts coerce through an explicit
        table, never Python truthiness."""
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._judged_run(tmp)
            result = collect_pairs(RunStore(tmp), [
                {"run_id": meta.run_id, "passed": "false", "score": "0.3"},
            ])
            pair = result["pairs"][0]
            self.assertIs(pair["human_passed"], False)
            self.assertEqual(pair["human_score"], 0.3)
            # garbage verdicts drop out of the metric, they don't corrupt it
            result2 = collect_pairs(RunStore(tmp), [
                {"run_id": meta.run_id, "passed": "maybe"},
            ])
            self.assertIsNone(result2["pairs"][0]["human_passed"])

    def test_ambiguous_prefix_is_unmatched_not_wrong(self):
        """A prefix matching several runs must not silently join the wrong
        run — it lands in unmatched."""
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._judged_run(tmp)
            result = collect_pairs(RunStore(tmp), [{"run_id": meta.run_id[:1]}])
            if len(result["pairs"]) == 0:
                self.assertEqual(result["coverage"]["unmatched"], [meta.run_id[:1]])
            else:  # only one run exists — a 1-char prefix resolves uniquely
                self.assertEqual(result["pairs"][0]["run_id"], meta.run_id)

    def test_load_labels_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.yaml"
            path.write_text("labels:\n  - run_id: abc\n    score: 0.5\n    passed: true\n")
            labels = load_labels(path)
            self.assertEqual(labels[0]["run_id"], "abc")
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("labels:\n  - score: 0.5\n")
            with self.assertRaises(ValueError):
                load_labels(bad)


class TestJudgeOnlyJoins(unittest.TestCase):
    """U6: pairs join labels to real judge verdicts — never to the
    mechanical fallback that used to inflate agreement."""

    def _run(self, tmp: str, judge: ModelConfig | None,
             judge_client=None, run_group: str | None = None,
             task_id: str = "cal-test"):
        client = judge_client or _JudgeClient()
        runner = Runner(
            runs_dir=tmp, planner="raw", run_group=run_group,
            clients={"orchestrator": _JudgeClient(), "worker": _JudgeClient(),
                     "judge": client},
        )
        return runner.run(
            TaskSpec(id=task_id, type="html", prompt="Make a page.",
                     validation=["non_empty"]),
            _model("org/x", "orchestrator"), _model("wrk/y", "worker"),
            judge=judge)

    def test_unjudged_run_excluded_from_pairs(self):
        """No report.judge -> coverage bucket, not a pair: the mechanical
        verdict must never stand in for a judge answer."""
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._run(tmp, judge=None)
            labels = [{"run_id": meta.run_id, "score": 0.9, "passed": True}]
            result = collect_pairs(RunStore(tmp), labels)
            self.assertEqual(result["coverage"]["matched"], 1)
            self.assertEqual(result["coverage"]["unjudged"], [meta.run_id])
            self.assertEqual(result["pairs"], [])

    def test_inconclusive_judge_joins_but_counts_verdictless(self):
        """A judged-but-verdictless run keeps its pair slot (score may
        still carry signal) but contributes nothing to verdict metrics."""
        with tempfile.TemporaryDirectory() as tmp:
            flaky = _JudgeClient()
            orig_chat = flaky.chat
            def chat(**kw):
                # the judge call is identifiable by its system prompt
                msgs = kw.get("messages") or []
                if msgs and msgs[0].get("role") == "system" \
                        and "expert judge" in str(msgs[0].get("content")):
                    return {"content": "not json", "usage": {}, "latency_ms": 1}
                return orig_chat(**kw)
            flaky.chat = chat
            meta = self._run(tmp, _model("j/judge", "judge"), flaky)
            result = collect_pairs(RunStore(tmp),
                                   [{"run_id": meta.run_id, "passed": True}])
            pair = result["pairs"][0]
            self.assertIsNone(pair["judge_passed"])
            self.assertEqual(pair["judge_model"], "j/judge")
            self.assertEqual(agreement_metrics(result["pairs"])["verdict"], None)

    def test_pairs_carry_judge_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._run(tmp, _model("j/judge", "judge"))
            result = collect_pairs(RunStore(tmp),
                                   [{"run_id": meta.run_id, "passed": True}])
            self.assertEqual(result["pairs"][0]["judge_model"], "j/judge")


class TestAgreementSlices(unittest.TestCase):
    def test_by_judge_and_by_task(self):
        pairs = [
            {"human_passed": True, "judge_passed": True, "human_score": 1.0,
             "judge_score": 0.9, "judge_model": "a/x", "task_id": "t1"},
            {"human_passed": False, "judge_passed": True, "human_score": 0.0,
             "judge_score": 0.8, "judge_model": "a/x", "task_id": "t2"},
            {"human_passed": True, "judge_passed": True, "human_score": 1.0,
             "judge_score": 0.95, "judge_model": "b/y", "task_id": "t1"},
        ]
        m = agreement_metrics(pairs)
        self.assertEqual(set(m["by_judge"]), {"a/x", "b/y"})
        self.assertEqual(m["by_judge"]["b/y"]["verdict"]["accuracy"], 1.0)
        self.assertEqual(m["by_judge"]["a/x"]["verdict"]["n"], 2)
        self.assertEqual(set(m["by_task"]), {"t1", "t2"})
        self.assertEqual(m["by_task"]["t1"]["verdict"]["accuracy"], 1.0)


class TestEmitSkeleton(unittest.TestCase):
    def test_emit_lists_finished_runs_with_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(
                runs_dir=tmp, planner="raw", run_group="g1",
                clients={"orchestrator": _JudgeClient(), "worker": _JudgeClient(),
                         "judge": _JudgeClient()})
            meta = runner.run(
                TaskSpec(id="cal-test", type="html", prompt="p",
                         validation=["non_empty"]),
                _model("o/x", "orchestrator"), _model("w/y", "worker"),
                judge=_model("j/j", "judge"))
            yaml_text = emit_label_skeleton(RunStore(tmp), run_group="g1")
            # ids are always single-quoted: an all-digit uuid prefix would
            # otherwise be quoted (or not) at the dumper's whim
            self.assertIn(f"run_id: '{meta.run_id}'", yaml_text)
            self.assertEqual(
                yaml.safe_load(yaml_text)["labels"][0]["run_id"], meta.run_id)
            self.assertIn("task_id: cal-test", yaml_text)
            self.assertIn("artifact:", yaml_text)
            self.assertIn("passed:", yaml_text)  # blank for the human
            # group exclusion is covered by
            # test_emit_excludes_unjudged_and_other_groups — a bare
            # substring check here false-positives whenever the random
            # tmp path happens to contain "g2"

    def test_emit_excludes_unjudged_and_other_groups(self):
        """Skeletons only list judged runs — labeling an unjudged run can
        never produce a calibration pair."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(
                runs_dir=tmp, planner="raw", run_group="g1",
                clients={"orchestrator": _JudgeClient(), "worker": _JudgeClient()})
            m1 = runner.run(TaskSpec(id="cal-test", type="html", prompt="p"),
                            _model("o/x", "orchestrator"), _model("w/y", "worker"))
            runner2 = Runner(
                runs_dir=tmp, planner="raw", run_group="g2",
                clients={"orchestrator": _JudgeClient(), "worker": _JudgeClient(),
                         "judge": _JudgeClient()})
            m2 = runner2.run(TaskSpec(id="cal-test", type="html", prompt="p"),
                             _model("o/x", "orchestrator"), _model("w/y", "worker"),
                             judge=_model("j/j", "judge"))
            yaml_text = emit_label_skeleton(RunStore(tmp), run_group="g1")
            self.assertNotIn(m2.run_id, yaml_text)
            # g1's run was never judged — it can't calibrate anything
            self.assertNotIn(m1.run_id, yaml_text)
            self.assertIn(m2.run_id,
                          emit_label_skeleton(RunStore(tmp), run_group="g2"))


EDGE_IDS = [
    "123456789012",    # all digits -> int
    "012345678901",    # leading zero
    "0123",            # YAML 1.1 octal
    "1e10", "1e1000000000", "123e456789ab",
    "true", "false", "yes", "no", "on", "off", "y", "n",
    "null", "~", "2026-10-03", "1_000", "0x1f", "0o17", ".inf", ".nan",
]


class TestRunIdEdgeCases(unittest.TestCase):
    """Run ids are uuid4 hex prefixes: ~0.4% are all digits, and many more
    look like numbers/dates. They must survive emit -> edit -> load."""

    def test_skeleton_roundtrips_every_edge_id(self):
        import orchestral.calibrate as cal

        class _M:
            def __init__(self, rid: str, d: str) -> None:
                self.run_id, self.task_id, self.status = rid, "t", "finished"
                self.run_dir = d

        class _S:
            def __init__(self, metas): self._m = metas
            def list_runs(self, run_group=None): return self._m

        with tempfile.TemporaryDirectory() as tmp:
            metas = []
            for i, rid in enumerate(EDGE_IDS):
                d = Path(tmp) / str(i)
                d.mkdir()
                (d / "report.json").write_text(json.dumps({"judge": {"model": "j"}}))
                metas.append(_M(rid, str(d)))
            text = cal.emit_label_skeleton(_S(metas))  # type: ignore[arg-type]
            loaded = yaml.safe_load(text)["labels"]
            self.assertEqual([e["run_id"] for e in loaded], EDGE_IDS)
            for rid in EDGE_IDS:
                self.assertIn(f"run_id: '{rid}'", text)

    def test_loader_keeps_unquoted_hand_edited_ids_as_text(self):
        for rid in EDGE_IDS:
            if rid in ("null", "~"):
                continue  # unquoted null is "no id", covered below
            with self.subTest(rid=rid), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "labels.yaml"
                p.write_text(f"labels:\n  - run_id: {rid}\n    passed: true\n")
                labels = load_labels(p)
                self.assertEqual(labels[0]["run_id"], rid)
                self.assertIsInstance(labels[0]["run_id"], str)
                self.assertIs(labels[0]["passed"], True)

    def test_loader_quoted_ids_and_missing_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "labels.yaml"
            p.write_text("labels:\n  - run_id: 'null'\n  - run_id: \"~\"\n")
            self.assertEqual([x["run_id"] for x in load_labels(p)], ["null", "~"])
            for bad in ("run_id:", "run_id: ~", "run_id: null", "passed: true"):
                p.write_text(f"labels:\n  - {bad}\n")
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    load_labels(p)

    def test_all_digit_run_id_end_to_end(self):
        """Seeded regression for the original flake: force an all-digit id."""
        import uuid as _uuid
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("orchestral.storage.uuid.uuid4",
                           return_value=_uuid.UUID("12345678-9012-3456-7890-123456789012")):
            runner = Runner(
                runs_dir=tmp, planner="raw", run_group="g1",
                clients={"orchestrator": _JudgeClient(), "worker": _JudgeClient(),
                         "judge": _JudgeClient()})
            meta = runner.run(
                TaskSpec(id="cal-test", type="html", prompt="p",
                         validation=["non_empty"]),
                _model("o/x", "orchestrator"), _model("w/y", "worker"),
                judge=_model("j/j", "judge"))
            self.assertTrue(meta.run_id.isdigit(), meta.run_id)
            text = emit_label_skeleton(RunStore(tmp), run_group="g1")
            self.assertIn(f"run_id: '{meta.run_id}'", text)
            p = Path(tmp) / "l.yaml"
            p.write_text(text)
            self.assertEqual(load_labels(p)[0]["run_id"], meta.run_id)


class TestCalibrationPersistence(unittest.TestCase):
    def test_persist_writes_judge_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            labels = Path(tmp) / "labels.yaml"
            labels.write_text("labels:\n  - run_id: abc\n    passed: true\n")
            pairs = [{"human_passed": True, "judge_passed": True,
                      "judge_model": "~typesafe/jev-latest", "task_id": "t"}]
            metrics = agreement_metrics(pairs)
            path = persist_calibration(
                reports, labels_path=labels, pairs=pairs, metrics=metrics)
            self.assertTrue(path.name.startswith("calibration-"))
            data = json.loads(path.read_text())
            self.assertIn("created_at", data)
            self.assertIn("labels_sha256", data)
            self.assertEqual(data["pairs"], 1)
            self.assertIn("~typesafe/jev-latest", data["judge_models"])
            self.assertIn("verdict", data["metrics"])

    def _seed_report(self, reports: Path, kappa_n: tuple[int, bool] = (30, True)):
        """Persist a synthetic calibration report with n verdict pairs."""
        n, agree = kappa_n
        pairs = [
            {"human_passed": i < 20, "judge_passed": (i < 20) if agree else (i % 3 == 0),
             "judge_model": "~typesafe/jev-latest", "task_id": "t"}
            for i in range(n)
        ]
        persist_calibration(reports, labels_path=None, pairs=pairs,
                            metrics=agreement_metrics(pairs))

    def test_status_uncalibrated_below_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            self._seed_report(reports, kappa_n=(29, True))  # n < 30
            status = calibration_status(reports, "~typesafe/jev-latest")
            self.assertFalse(status["calibrated"])
            self.assertEqual(status["verdict_pairs"], 29)

    def test_status_calibrated_at_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            self._seed_report(reports, kappa_n=(30, True))  # kappa 1.0, n=30
            status = calibration_status(reports, "~typesafe/jev-latest")
            self.assertTrue(status["calibrated"])
            self.assertGreaterEqual(status["kappa"], 0.7)

    def test_status_uncalibrated_low_kappa(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            self._seed_report(reports, kappa_n=(30, False))  # disagreement
            status = calibration_status(reports, "~typesafe/jev-latest")
            self.assertFalse(status["calibrated"])

    def test_status_uses_newest_report_containing_slug(self):
        """A newer report that lacks this judge must not reset its status —
        status reads the newest report that actually contains the slug."""
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            reports.mkdir()
            # older report: jev calibrated with 30 agreeing pairs
            old = {
                "created_at": "2026-01-01T00:00:00+00:00",
                "metrics": {"by_judge": {"~typesafe/jev-latest": {
                    "verdict": {"n": 30, "kappa": 0.9}}}},
            }
            (reports / "calibration-20260101-000000.json").write_text(json.dumps(old))
            # newer report: a different judge only — no jev block
            new = {
                "created_at": "2026-02-01T00:00:00+00:00",
                "metrics": {"by_judge": {"j/other": {
                    "verdict": {"n": 40, "kappa": 0.8}}}},
            }
            (reports / "calibration-20260201-000000.json").write_text(json.dumps(new))
            status = calibration_status(reports, "~typesafe/jev-latest")
            self.assertTrue(status["calibrated"])
            self.assertEqual(status["verdict_pairs"], 30)
            # and the other judge reads its own newest block
            other = calibration_status(reports, "j/other")
            self.assertTrue(other["calibrated"])
            self.assertEqual(other["verdict_pairs"], 40)

    def test_status_none_when_no_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = calibration_status(Path(tmp) / "reports", "~typesafe/jev-latest")
            self.assertFalse(status["calibrated"])
            self.assertIsNone(status["kappa"])


class TestCalibrateCLI(unittest.TestCase):
    """harness-level: --emit writes a skeleton, --labels persists a
    timestamped report, and neither-arg exits cleanly."""

    def _args(self, tmp: str, **kw):
        import harness
        defaults = {"labels": None, "emit": None, "runs_dir": tmp,
                    "reports_dir": str(Path(tmp) / "reports"), "json": False}
        defaults.update(kw)
        return harness.argparse.Namespace(**defaults)

    def _judged_run(self, tmp: str, group: str = "g1"):
        runner = Runner(
            runs_dir=tmp, planner="raw", run_group=group,
            clients={"orchestrator": _JudgeClient(), "worker": _JudgeClient(),
                     "judge": _JudgeClient()})
        return runner.run(
            TaskSpec(id="cal-test", type="html", prompt="p",
                     validation=["non_empty"]),
            _model("o/x", "orchestrator"), _model("w/y", "worker"),
            judge=_model("j/j", "judge"))

    def test_emit_writes_skeleton_without_labels(self):
        import harness
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._judged_run(tmp)
            harness.cmd_calibrate(self._args(tmp, emit="g1"))
            skeletons = list((Path(tmp) / "reports").glob("labels-g1-*.yaml"))
            self.assertEqual(len(skeletons), 1)
            self.assertIn(meta.run_id, skeletons[0].read_text())

    def test_labels_run_persists_report(self):
        import harness
        with tempfile.TemporaryDirectory() as tmp:
            meta = self._judged_run(tmp)
            labels = Path(tmp) / "labels.yaml"
            labels.write_text(
                f"labels:\n  - run_id: {meta.run_id}\n    passed: true\n")
            harness.cmd_calibrate(self._args(tmp, labels=str(labels)))
            reports = list((Path(tmp) / "reports").glob("calibration-*.json"))
            self.assertEqual(len(reports), 1)
            data = json.loads(reports[0].read_text())
            self.assertEqual(data["judge_models"], ["j/j"])
            self.assertEqual(data["verdict_pairs"], 1)

    def test_no_labels_no_emit_exits(self):
        import harness
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            harness.cmd_calibrate(self._args(tmp))


class TestContractSlices(unittest.TestCase):
    """v2 judge contract slices: per-criterion rates and the
    claimed-vs-derived disagreement gate metric."""

    def test_contract_block_counts_disagreements(self):
        pairs = [
            {"run_id": "r1", "task_id": "t", "judge_model": "j",
             "judge_passed": False, "judge_claimed_passed": True},
            {"run_id": "r2", "task_id": "t", "judge_model": "j",
             "judge_passed": True, "judge_claimed_passed": True},
            {"run_id": "r3", "task_id": "t", "judge_model": "j",
             "judge_passed": True},  # pre-v2 — no claim recorded
        ]
        contract = agreement_metrics(pairs)["contract"]
        self.assertEqual(contract["n"], 2)
        self.assertEqual(contract["disagreements"], 1)
        self.assertEqual(contract["runs"], ["r1"])
        self.assertEqual(contract["disagreement_rate"], 0.5)

    def test_contract_block_empty_without_v2_pairs(self):
        m = agreement_metrics([{"run_id": "r", "judge_passed": True}])
        self.assertEqual(m["contract"]["n"], 0)
        self.assertIsNone(m["contract"]["disagreement_rate"])

    def test_criteria_block_rates_by_task_criterion(self):
        pairs = [
            {"run_id": "r1", "task_id": "t", "criteria": {
                "a": {"satisfied": True, "supported": True},
                "b": {"satisfied": True, "supported": False},
            }},
            {"run_id": "r2", "task_id": "t", "criteria": {
                "a": {"satisfied": False, "supported": None},
                "b": {"satisfied": None, "supported": None},
            }},
        ]
        crit = agreement_metrics(pairs)["by_criterion"]
        self.assertEqual(crit["t:a"]["satisfied_rate"], 0.5)
        self.assertEqual(crit["t:a"]["supported_rate"], 0.5)
        self.assertEqual(crit["t:b"]["unsupported_rate"], 0.5)
        self.assertEqual(crit["t:b"]["unassessed_rate"], 0.5)

    def test_collect_pairs_carries_claimed_and_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = TestCollectPairs._judged_run(self, tmp)
            report_path = Path(meta.run_dir) / "report.json"
            report = json.loads(report_path.read_text())
            report["judge"]["claimed_passed"] = True
            report["judge"]["claimed_score"] = 0.9
            report["judge"]["judge_contract"] = "v2"
            report["judge"]["criteria"] = [
                {"id": "a", "satisfied": True, "supported": True},
            ]
            report_path.write_text(json.dumps(report))
            result = collect_pairs(
                RunStore(tmp), [{"run_id": meta.run_id, "passed": True}])
            (p,) = result["pairs"]
            self.assertTrue(p["judge_claimed_passed"])
            self.assertEqual(p["judge_claimed_score"], 0.9)
            self.assertEqual(p["judge_contract"], "v2")
            self.assertEqual(
                p["criteria"], {"a": {"satisfied": True, "supported": True}})


if __name__ == "__main__":
    unittest.main()
