"""Observatory substrate: sequenced lifecycle events, manifest, leaderboard, export."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.export import leaderboard_csv, run_audit_markdown, runs_csv
from orchestral.logger import LIFECYCLE_EVENTS, EventLogger
from orchestral.runner import Runner
from orchestral.stats import MIN_LEADERBOARD_SAMPLES, horizon_fit, pairing_leaderboard
from orchestral.storage import RunMeta, RunStore


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
    )


def _chat_client(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "latency_ms": 1,
        "id": "mock",
    }
    return client


def _plan_client() -> MagicMock:
    return _chat_client(json.dumps({"subtasks": [{"id": 0, "description": "do the thing"}]}))


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t1", "worker": "w/m",
        "status": "finished", "started_at": "t", "finished_at": "t",
        "total_cost_usd": 0.01, "total_input_tokens": 10, "total_output_tokens": 5,
        "score": 0.8, "passes": True, "latency_ms": 1000.0,
    }
    base.update(kw)
    return RunMeta(**base)


class TestEventSchemaV2(unittest.TestCase):
    def test_events_carry_sequence_run_id_worker_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(tmp, run_id="r1")
            e1 = logger.lifecycle("run.created", phase="init", run_id="r1")
            e2 = logger.lifecycle("worker.started", worker_id="worker-0", subtask_id=3)
            e3 = logger.log(
                phase="delegate", step=3, event_type="worker_error", model="w/m",
                role="worker", worker_id="worker-0", input_data={}, output_data={},
            )
            logger.close()
            self.assertEqual((e1["sequence"], e2["sequence"], e3["sequence"]), (1, 2, 3))
            self.assertEqual(e1["run_id"], "r1")
            self.assertEqual(e2["worker_id"], "worker-0")
            self.assertEqual(e3["worker_id"], "worker-0")
            self.assertEqual(e1["schema_version"], "2")
            self.assertEqual(e2["output"]["subtask_id"], 3)

    def test_lifecycle_rejects_unknown_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(tmp)
            with self.assertRaises(ValueError):
                logger.lifecycle("not.real")
            logger.close()

    def test_vocabulary_covers_terminal_and_phase_events(self):
        for needed in (
            "run.created", "run.started", "task.loaded",
            "orchestrator.started", "orchestrator.completed",
            "delegation.created", "worker.started", "worker.progress",
            "worker.completed", "worker.failed",
            "synthesis.started", "synthesis.completed",
            "evaluation.started", "evaluation.completed",
            "usage.recorded", "artifact.saved",
            "run.completed", "run.failed", "run.cancelled",
        ):
            self.assertIn(needed, LIFECYCLE_EVENTS)


class TestManifestAndLifecycle(unittest.TestCase):
    def test_dry_run_writes_manifest_and_lifecycle_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            runner = Runner(
                runs_dir=tmp, store=store, dry_run=True,
                run_group="g1", replicate=2, seed=9,
                clients={"orchestrator": _plan_client(), "worker": _chat_client("<html>x</html>")},
            )
            meta = runner.run(
                TaskSpec(id="t1", type="html", prompt="make a page"),
                _model("o/m", "orchestrator"), _model("w/m", "worker"),
            )
            run_dir = Path(meta.run_dir)

            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "passed")
            self.assertEqual(manifest["run_id"], meta.run_id)
            self.assertEqual(manifest["task_id"], "t1")
            self.assertEqual(manifest["orchestrator_model"], "o/m")
            self.assertEqual(manifest["worker_model"], "w/m")
            self.assertEqual(manifest["run_group"], "g1")
            self.assertEqual(manifest["replicate"], 2)
            self.assertEqual(manifest["seed"], 9)
            for field in ("task_hash", "config_hash", "orchestrator_prompt_hash",
                          "worker_prompt_hash", "git_commit", "harness_version",
                          "python_version", "finished_at"):
                self.assertTrue(manifest.get(field), f"manifest missing {field}")

            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            types = [e["type"] for e in events]
            for needed in (
                "run.created", "task.loaded", "run.started",
                "orchestrator.started", "orchestrator.completed",
                "delegation.created", "worker.started", "worker.completed",
                "synthesis.started", "synthesis.completed",
                "evaluation.started", "evaluation.completed",
                "usage.recorded", "artifact.saved", "run.completed",
            ):
                self.assertIn(needed, types)
            # sequence is monotonic and terminal event is last
            seqs = [e["sequence"] for e in events]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(types[-1], "run.completed")
            self.assertEqual(types.index("run.created"), 0)
            # lifecycle events carry their worker scope
            wstart = next(e for e in events if e["type"] == "worker.started")
            self.assertEqual(wstart["worker_id"], "worker-0")

    def test_manifest_status_failed_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            orch = MagicMock()
            orch.chat.side_effect = RuntimeError("boom")
            runner = Runner(
                runs_dir=tmp, store=store,
                clients={"orchestrator": orch, "worker": _chat_client("")},
            )
            with self.assertRaises(RuntimeError):
                runner.run(
                    TaskSpec(id="t1", type="html", prompt="p"),
                    _model("o/m", "orchestrator"), _model("w/m", "worker"),
                )
            run_dir = Path(store.list_runs()[0].run_dir)
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("unknown", manifest["failure_reason"])
            self.assertTrue(manifest["finished_at"])
            last = json.loads((run_dir / "events.jsonl").read_text().splitlines()[-1])
            self.assertEqual(last["type"], "run.failed")

    def test_task_hash_changes_with_content(self):
        from orchestral.manifest import task_hash

        t1 = TaskSpec(id="t", type="html", prompt="a")
        t2 = TaskSpec(id="t", type="html", prompt="b")
        self.assertNotEqual(task_hash(t1), task_hash(t2))
        self.assertEqual(task_hash(t1), task_hash(TaskSpec(id="t", type="html", prompt="a")))


class TestLeaderboard(unittest.TestCase):
    def test_pairing_aggregates(self):
        runs = [
            _meta(run_id=f"a{i}", orchestrator="o/big", worker="w/cheap",
                  score=0.9, total_cost_usd=0.02, passes=i % 2 == 0)
            for i in range(4)
        ] + [
            _meta(run_id=f"b{i}", orchestrator="o/small", worker="w/mid",
                  score=0.5, total_cost_usd=0.005, passes=True)
            for i in range(3)
        ]
        board = pairing_leaderboard(runs, min_samples=10)
        self.assertEqual(len(board), 2)
        # w/mid is cheaper per pass: $0.015/3 < $0.08/2 → sorted first
        self.assertEqual(board[0].worker, "w/mid")
        a = next(p for p in board if p.orchestrator == "o/big")
        self.assertEqual(a.runs, 4)
        self.assertEqual(a.passed, 2)
        self.assertAlmostEqual(a.cost_per_pass or 0, 0.04)
        self.assertEqual(a.tasks_covered, 1)
        self.assertTrue(a.low_sample)  # 4 < 10

    def test_judge_axis_separate_from_mechanical_score(self):
        """`score_median` is the mechanical axis; `judge_score_median` reads
        meta.judge_score only — a run can carry both and they must not mix."""
        runs = [
            _meta(run_id=f"r{i}", score=0.5, judge_score=0.9, judge_passed=True)
            for i in range(3)
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertAlmostEqual(row.score_median or 0, 0.5)
        self.assertAlmostEqual(row.judge_score_median or 0, 0.9)
        # an unjudged run contributes no judge axis
        row2 = pairing_leaderboard([_meta(run_id="u", score=0.5, judge_score=None)])[0]
        self.assertIsNone(row2.judge_score_median)

    def test_low_sample_threshold_configurable(self):
        runs = [_meta(run_id=f"r{i}") for i in range(3)]
        self.assertFalse(pairing_leaderboard(runs)[0].low_sample)
        self.assertTrue(pairing_leaderboard(runs, min_samples=4)[0].low_sample)
        self.assertEqual(MIN_LEADERBOARD_SAMPLES, 3)

    def test_low_sample_never_outranks_evidence(self):
        """A 1/1 100% pairing is an anecdote — it tails the board even
        when an evidence-backed row has a lower pass rate."""
        runs = [_meta(run_id="lucky", orchestrator="o/lucky", worker="w/lucky")] + [
            _meta(run_id=f"m{i}", orchestrator="o/real", worker="w/real",
                  passes=i < 3)
            for i in range(6)
        ]
        board = pairing_leaderboard(runs)
        self.assertEqual(board[0].orchestrator, "o/real")
        self.assertFalse(board[0].low_sample)
        self.assertEqual(board[-1].orchestrator, "o/lucky")
        self.assertTrue(board[-1].low_sample)
        self.assertEqual(board[-1].pass_rate, 1.0)

    def test_unfinished_runs_count_in_n_but_not_medians(self):
        runs = [
            _meta(run_id="ok", total_cost_usd=0.02, latency_ms=100),
            _meta(run_id="bad", status="failed", passes=False,
                  failure_reason="exception:timeout", total_cost_usd=0.5,
                  latency_ms=9999),
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertEqual(row.runs, 2)
        self.assertEqual(row.finished, 1)
        self.assertAlmostEqual(row.cost_median, 0.02)  # failed run's cost excluded
        self.assertAlmostEqual(row.duration_median_ms, 100)
        self.assertAlmostEqual(row.failure_rate or 0, 0.5)
        self.assertEqual(row.failures.get("exception:timeout"), 1)

    def test_crashed_runs_are_not_evidence(self):
        """Three infra-failed runs and zero finished is not a 3-sample
        pairing — pass_rate is a capability axis over finished runs."""
        runs = [
            _meta(run_id=f"c{i}", status="failed", passes=False,
                  failure_reason="exception:timeout")
            for i in range(3)
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertEqual(row.runs, 3)
        self.assertEqual(row.finished, 0)
        self.assertIsNone(row.pass_rate)
        self.assertTrue(row.low_sample)      # 0 finished < 3: cannot rank

    def test_pass_rate_counts_finished_only(self):
        runs = [
            _meta(run_id="p", passes=True),
            _meta(run_id="f", passes=False),
            _meta(run_id="c1", status="failed", passes=False,
                  failure_reason="exception:x"),
            _meta(run_id="c2", status="failed", passes=False,
                  failure_reason="exception:x"),
        ]
        row = pairing_leaderboard(runs)[0]
        self.assertAlmostEqual(row.pass_rate or 0, 0.5)   # 1/2 finished, not 1/4
        self.assertAlmostEqual(row.failure_rate or 0, 0.75)  # infra noise still visible

    def test_zero_cost_is_unmetered_not_free(self):
        """A $0 total means the meter read nothing — it must not outrank a
        pairing with a real (nonzero) cost_per_pass."""
        runs = [
            _meta(run_id=f"z{i}", orchestrator="o/free", worker="w/free",
                  passes=True, total_cost_usd=0.0)
            for i in range(3)
        ] + [
            _meta(run_id=f"p{i}", orchestrator="o/paid", worker="w/paid",
                  passes=True, total_cost_usd=0.01)
            for i in range(3)
        ]
        board = pairing_leaderboard(runs)
        free = next(p for p in board if p.orchestrator == "o/free")
        self.assertIsNone(free.cost_per_pass)
        self.assertEqual(board[0].orchestrator, "o/paid")

    def test_default_order_is_pass_rate_desc(self):
        """The headline question is 'which pairing performs best' — pass rate
        ranks first, cost per pass breaks ties."""
        runs = [
            _meta(run_id=f"hi{i}", orchestrator="o/hi", worker="w/hi",
                  passes=True, total_cost_usd=0.10)
            for i in range(3)
        ] + [
            _meta(run_id=f"lo{i}", orchestrator="o/lo", worker="w/lo",
                  passes=i < 2, total_cost_usd=0.001)
            for i in range(3)
        ]
        board = pairing_leaderboard(runs)
        self.assertEqual(board[0].orchestrator, "o/hi")   # 100% > 67% despite 100x cost

    def test_pareto_frontier_marks_non_dominated_rows(self):
        """Frontier = nobody is both better on quality and cheaper per pass.
        The expensive-but-best and cheap-but-good rows both stay; the
        worst-of-both row is dominated out."""
        runs = (
            [_meta(run_id=f"a{i}", orchestrator="o/big", worker="w/big",
                   passes=True, total_cost_usd=0.10) for i in range(3)]
            + [_meta(run_id=f"b{i}", orchestrator="o/mid", worker="w/mid",
                     passes=i < 2, total_cost_usd=0.02) for i in range(3)]
            + [_meta(run_id=f"c{i}", orchestrator="o/bad", worker="w/bad",
                     passes=i == 0, total_cost_usd=0.05) for i in range(3)]
        )
        board = {p.orchestrator: p for p in pairing_leaderboard(runs)}
        self.assertTrue(board["o/big"].on_frontier)    # best quality
        self.assertTrue(board["o/mid"].on_frontier)    # cheapest credible
        self.assertFalse(board["o/bad"].on_frontier)   # dominated by both

    def test_frontier_excludes_thin_and_unmetered_rows(self):
        """A lucky 1/1 cheap pairing is an anecdote and an unmetered row has
        no price — neither can nominate a frontier point, so the credible
        middle row keeps the mark either way."""
        runs = (
            [_meta(run_id="lucky", orchestrator="o/lucky", worker="w/lucky",
                   passes=True, total_cost_usd=0.001)]
            + [_meta(run_id=f"m{i}", orchestrator="o/mid", worker="w/mid",
                     passes=i < 2, total_cost_usd=0.02) for i in range(3)]
            + [_meta(run_id=f"u{i}", orchestrator="o/free", worker="w/free",
                     passes=True, total_cost_usd=0.0) for i in range(3)]
        )
        board = {p.orchestrator: p
                 for p in pairing_leaderboard(runs, unmetered_workers=["w/free"])}
        self.assertFalse(board["o/lucky"].on_frontier)  # low_sample
        self.assertFalse(board["o/free"].on_frontier)   # no measured price
        self.assertTrue(board["o/mid"].on_frontier)

    def test_zero_pass_pairing_is_off_frontier(self):
        """No passes means no cost_per_pass — an always-fail pairing cannot
        be a defensible pick at any price."""
        runs = (
            [_meta(run_id=f"z{i}", orchestrator="o/zero", worker="w/zero",
                   passes=False, total_cost_usd=0.001) for i in range(3)]
            + [_meta(run_id=f"g{i}", orchestrator="o/good", worker="w/good",
                     passes=True, total_cost_usd=0.05) for i in range(3)]
        )
        board = {p.orchestrator: p for p in pairing_leaderboard(runs)}
        self.assertFalse(board["o/zero"].on_frontier)
        self.assertTrue(board["o/good"].on_frontier)
        self.assertTrue(board["o/good"].to_dict()["on_frontier"])


class TestHorizonFit(unittest.TestCase):
    """METR-style t50: logistic pass rate over log task minutes."""

    def test_recovers_known_horizon(self):
        # pass short tasks, fail long ones; crossover at ~30 minutes
        pts = [(5.0, True)] * 4 + [(15.0, True)] * 4 + [(15.0, False)]
        pts += [(45.0, False)] * 3 + [(45.0, True)]
        pts += [(90.0, False)] * 4 + [(180.0, False)] * 4
        fit = horizon_fit(pts)
        self.assertIsNotNone(fit)
        self.assertGreater(fit["t50_minutes"], 15.0)
        self.assertLess(fit["t50_minutes"], 90.0)
        self.assertLess(fit["slope"], 0)
        self.assertEqual(fit["tasks"], 5)

    def test_single_outcome_or_duration_returns_none(self):
        self.assertIsNone(horizon_fit([(10.0, True)] * 10))          # all pass
        self.assertIsNone(horizon_fit([(10.0, True), (10.0, False)] * 5))  # one duration
        self.assertIsNone(horizon_fit([(10.0, True), (20.0, False)]))      # too few

    def test_no_decline_returns_none(self):
        # longer tasks pass more often: no decay, no horizon
        pts = [(5.0, False)] * 4 + [(60.0, True)] * 4
        self.assertIsNone(horizon_fit(pts))

    def test_zero_or_missing_minutes_ignored(self):
        pts = ([(0.0, True)] * 3 + [(10.0, True)] * 3 + [(10.0, False)]
               + [(90.0, False)] * 4)
        fit = horizon_fit(pts)
        self.assertIsNotNone(fit)
        self.assertEqual(fit["n"], 8)  # the zero-minute points dropped


class TestExport(unittest.TestCase):
    def test_runs_csv(self):
        out = runs_csv([_meta(run_id="r1"), _meta(run_id="r2", passes=False)])
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("run_id", lines[0])
        self.assertIn("r1", lines[1])

    def test_leaderboard_csv(self):
        rows = pairing_leaderboard([_meta(run_id="r1")])
        out = leaderboard_csv(rows)
        self.assertIn("cost_per_pass", out)
        self.assertIn("o/m", out)

    def test_run_audit_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run1"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(json.dumps({
                "task_id": "t1", "task_hash": "abc123",
                "orchestrator_model": "o/m", "orchestrator_provider": "openrouter",
                "worker_model": "w/m", "worker_provider": "openrouter",
                "status": "passed", "started_at": "s", "finished_at": "f",
                "git_commit": "deadbeef", "harness_version": "0.3",
                "python_version": "3.11", "seed": None, "run_group": None,
                "replicate": None,
            }))
            (run_dir / "report.json").write_text(json.dumps({
                "checks": {"non_empty": True}, "score": 0.9, "errors": [],
            }))
            (run_dir / "artifact.html").write_text("<html>x</html>")
            md = run_audit_markdown(run_dir)
            self.assertIn("t1", md)
            self.assertIn("o/m", md)
            self.assertIn("non_empty: pass", md)
            self.assertIn("artifact.html", md)

    def test_run_audit_markdown_tolerates_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = run_audit_markdown(Path(tmp))
            self.assertIn("Run audit", md)


class TestChartPayloads(unittest.TestCase):
    """U13: payloads the Pairings and Compare charts draw from."""

    def _store(self, tmp: str, metas: list[RunMeta]) -> RunStore:
        store = RunStore(Path(tmp))
        for m in metas:
            store.index_meta(m)
        return store

    @staticmethod
    def _runs(group: str, orch: str, worker: str, task: str, passes: int, fails: int,
              cost: float = 0.01, start: str = "2026-10-01T00:00:00+00:00") -> list[RunMeta]:
        out = []
        for i in range(passes + fails):
            ok = i < passes
            out.append(_meta(run_id=f"{group}-{orch}-{worker}-{task}-{i}", run_group=group,
                             orchestrator=orch, worker=worker, task_id=task, passes=ok,
                             total_cost_usd=cost, started_at=start))
        return out

    def test_default_scope_is_all_groups_when_no_group_holds_three_pairings(self):
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            metas = (self._runs("g1", "o1", "w1", "t", 3, 0) + self._runs("g1", "o1", "w2", "t", 3, 0)
                     + self._runs("g2", "o2", "w1", "t", 3, 0))
            payload = state.pairings_payload(self._store(tmp, metas))
        self.assertEqual(payload["default_group"], "")

    def test_default_scope_is_the_latest_group_with_three_pairings(self):
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            metas = (self._runs("old", "o1", "w1", "t", 3, 0, start="2026-09-01T00:00:00+00:00")
                     + self._runs("big", "o1", "w1", "t", 3, 0, start="2026-10-01T00:00:00+00:00")
                     + self._runs("big", "o1", "w2", "t", 3, 0, start="2026-10-01T00:00:00+00:00")
                     + self._runs("big", "o2", "w2", "t", 3, 0, start="2026-10-01T00:00:00+00:00")
                     + self._runs("solo", "o1", "w1", "t", 3, 0, start="2026-10-02T00:00:00+00:00"))
            payload = state.pairings_payload(self._store(tmp, metas))
        self.assertEqual(payload["default_group"], "big")

    def test_default_scope_helper_ignores_ungrouped_runs(self):
        from orchestral.web import state

        class R:
            def __init__(self, group, o, w, started):
                self.run_group, self.orchestrator, self.worker, self.started_at = group, o, w, started

        runs = [R("", "o", f"w{i}", "2026-10-03") for i in range(4)]
        runs += [R("b", "o", "w1", "2026-10-01"), R("b", "o", "w2", "2026-10-01")]
        self.assertEqual(state.default_pairing_group(runs), "")
        runs += [R("b", "o", "w3", "2026-10-01")]
        self.assertEqual(state.default_pairing_group(runs), "b")

    def test_pairings_summary_counts_the_degenerate_cohorts(self):
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            metas = (self._runs("g", "o", "wbig", "t", 8, 4)          # n=12, metered
                     + self._runs("g", "o", "wzero", "t", 0, 3)       # no passes
                     + self._runs("g", "o", "wfree", "t", 3, 0, cost=0.0))  # unmetered
            payload = state.pairings_payload(self._store(tmp, metas), group="g")
        summary = payload["summary"]
        self.assertEqual(summary["pairings"], 3)
        self.assertEqual(summary["metered"], 2)
        self.assertEqual(summary["unmetered"], 1)
        self.assertEqual(summary["best_eligible"], 1)
        self.assertEqual(summary["no_pass"], 1)
        flags = {r["worker"]: r["low_n_best"] for r in payload["rows"]}
        self.assertEqual(flags, {"wbig": False, "wzero": True, "wfree": True})

    def _cmp(self, specs):
        """specs: task -> (a_pass, a_fail, b_pass, b_fail); one-sided via None."""
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            metas = []
            for task, (ap, af, bp, bf) in specs.items():
                if ap is not None:
                    metas += self._runs("A", "o", "w", task, ap, af)
                if bp is not None:
                    metas += self._runs("B", "o", "w", task, bp, bf)
            payload = state.compare_payload(self._store(tmp, metas), "A", "B")
        return payload, {c["task_id"]: c for c in payload["cells"]}

    def test_separated_intervals_are_improved_or_regressed(self):
        _, cells = self._cmp({"up": (4, 16, 16, 4), "down": (18, 2, 4, 16)})
        self.assertEqual(cells["up"]["verdict"], "improved")
        self.assertEqual(cells["down"]["verdict"], "regressed")

    def test_overlapping_intervals_are_no_clear_difference_but_keep_the_delta(self):
        _, cells = self._cmp({"noise": (10, 10, 12, 8), "same": (10, 10, 10, 10)})
        self.assertEqual(cells["noise"]["verdict"], "no-clear-difference")
        self.assertAlmostEqual(cells["noise"]["delta"], 0.1)
        self.assertEqual(cells["same"]["verdict"], "no-clear-difference")

    def test_low_n_cells_never_get_improved_or_regressed(self):
        _, cells = self._cmp({"thin-a": (0, 2, 20, 0), "thin-b": (20, 0, 0, 2)})
        self.assertEqual(cells["thin-a"]["verdict"], "no-clear-difference")
        self.assertEqual(cells["thin-b"]["verdict"], "no-clear-difference")
        self.assertTrue(cells["thin-a"]["low_n"])

    def test_one_sided_keeps_its_label(self):
        _, cells = self._cmp({"only-a": (5, 5, None, None)})
        self.assertEqual(cells["only-a"]["verdict"], "one-sided")

    def test_compare_sorts_regressions_then_improvements_then_noise_then_one_sided(self):
        payload, _ = self._cmp({
            "t-noise": (10, 10, 12, 8), "t-up": (4, 16, 16, 4), "t-down": (18, 2, 4, 16),
            "t-only-a": (5, 5, None, None), "t-up2": (2, 18, 18, 2)})
        self.assertEqual([c["task_id"] for c in payload["cells"]],
                         ["t-down", "t-up2", "t-up", "t-noise", "t-only-a"])
        self.assertEqual(payload["verdicts"], {"regressed": 1, "improved": 2,
                                               "no-clear-difference": 1, "one-sided": 1})
        self.assertEqual(payload["shared"], 4)
        self.assertEqual(payload["one_sided"], 1)

    def test_compare_cells_carry_intervals_and_counts(self):
        _, cells = self._cmp({"down": (18, 2, 4, 16)})
        down = cells["down"]
        self.assertEqual((down["passed_a"], down["finished_a"], down["passed_b"], down["finished_b"]), (18, 20, 4, 20))
        self.assertAlmostEqual(down["delta"], -0.7)
        self.assertEqual(len(down["ci_a"]), 2)
        self.assertEqual(len(down["ci_b"]), 2)

    def test_compare_reports_a_cost_delta(self):
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            metas = (self._runs("A", "o", "w", "t", 4, 0, cost=0.01) + self._runs("B", "o", "w", "t", 4, 0, cost=0.03))
            payload = state.compare_payload(self._store(tmp, metas), "A", "B")
        self.assertAlmostEqual(payload["cost_delta"], 0.08)
        self.assertAlmostEqual(payload["cells"][0]["cost_delta"], 0.08)

    def test_compare_with_disjoint_task_sets_has_zero_shared_cells(self):
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            metas = self._runs("A", "o", "w", "t1", 3, 0) + self._runs("B", "o", "w", "t2", 3, 0)
            payload = state.compare_payload(self._store(tmp, metas), "A", "B")
        self.assertEqual(payload["shared"], 0)
        self.assertEqual(payload["one_sided"], 2)
        self.assertEqual({c["side"] for c in payload["cells"]}, {"baseline", "candidate"})

    def test_compare_blocks_a_group_against_itself(self):
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            payload = state.compare_payload(
                self._store(tmp, self._runs("A", "o", "w", "t", 3, 0)), "A", "A")
        self.assertEqual(payload["cells"], [])
        self.assertIn("different", payload["blocked"])

    def test_compare_resolves_the_ungrouped_bucket(self):
        from orchestral.web import state
        with tempfile.TemporaryDirectory() as tmp:
            metas = self._runs("", "o", "w", "t", 3, 0) + self._runs("B", "o", "w", "t", 3, 0)
            payload = state.compare_payload(self._store(tmp, metas), "(ungrouped)", "B")
        self.assertEqual(payload["shared"], 1)


if __name__ == "__main__":
    unittest.main()
