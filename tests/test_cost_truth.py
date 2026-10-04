"""U8: billed cost truth, per-model calibration and spend safety.

Everything runs against the U23 fixture corpus or tiny hand-seeded stores. No
provider is constructed and no key is read.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import statistics
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import harness
from orchestral.experiment import Cell, rep_target
from orchestral.stats import aggregate, pairing_leaderboard
from orchestral.storage import (
    MIN_OWN_RATIO_CALLS,
    RunMeta,
    RunStore,
)
from orchestral.web import state

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert _spec and _spec.loader
corpus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(corpus)
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"


def _meta(rid: str, *, total: float = 0.01, status: str = "finished", dry: bool = False,
          started: str | None = None, orch: str = "o/m", worker: str = "w/m",
          task: str = "t", passes: bool | None = True, group: str = "g") -> RunMeta:
    return RunMeta(
        run_id=rid, orchestrator=orch, task_id=task, worker=worker, status=status,
        started_at=started or datetime.now(UTC).isoformat(), total_cost_usd=total,
        total_input_tokens=10, total_output_tokens=20, latency_ms=100.0, passes=passes,
        score=0.8, run_group=group, dry_run=dry, config={"dry_run": dry})


def _call(store: RunStore, rid: str, model: str, cost: float, api: float | None,
          *, dry: bool = False) -> None:
    store.record_call(run_id=rid, phase="delegate", step=1, role="worker", model=model,
                      cost_usd=cost, api_cost_usd=api, dry_run=dry,
                      pricing_source="api_reported" if api is not None else "none")


def _models_dir(root: Path, scale: float = 1.0) -> Path:
    d = root / "models"
    d.mkdir(exist_ok=True)
    (d / "m.yaml").write_text(
        "models:\n"
        f"  - slug: o/m\n    name: o\n    role: orchestrator\n"
        f"    input_price_per_mtok: {0.03 * scale}\n    output_price_per_mtok: {0.10 * scale}\n"
        f"  - slug: w/m\n    name: w\n    role: worker\n"
        f"    input_price_per_mtok: {0.03 * scale}\n    output_price_per_mtok: {0.10 * scale}\n")
    return d


class _Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "runs"
        cls.manifest = corpus.build_corpus(cls.root, "full")
        cls.store = RunStore(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestBilledRunCost(_Corpus):
    def test_failed_run_shows_billed_not_recorded_cost(self):
        meta = self.store.get_run(self.manifest["failed_run_id"])
        assert meta is not None
        self.assertAlmostEqual(meta.total_cost_usd, 0.11)
        self.assertAlmostEqual(meta.billed_cost_usd, 0.74)
        self.assertEqual(meta.cost_basis, "billed")

    def test_fully_priced_run_equals_sum_of_api_costs(self):
        checked = 0
        for meta in self.store.list_runs(limit=None):
            calls = [c for c in self.store.calls_for_run(meta.run_id) if not c["dry_run"]]
            if calls and meta.billed_cost_usd is not None and all(
                    c["api_cost_usd"] is not None for c in calls):
                self.assertAlmostEqual(
                    meta.billed_cost_usd, sum(c["api_cost_usd"] for c in calls), places=9)
                self.assertEqual(meta.cost_basis, "billed")
                checked += 1
        self.assertGreater(checked, 500)

    def test_mixed_and_calibrated_basis(self):
        bases = {m.cost_basis for m in self.store.list_runs(limit=None)}
        self.assertTrue({"billed", "mixed", "calibrated"} <= bases, bases)

    def test_dry_runs_bill_nothing(self):
        dry = [m for m in self.store.list_runs(limit=None) if m.dry_run]
        self.assertTrue(dry)
        self.assertTrue(all(m.billed_cost_usd == 0.0 for m in dry))

    def test_billed_fields_never_leak_into_serialised_meta(self):
        meta = self.store.get_run(self.manifest["failed_run_id"])
        assert meta is not None
        for d in (meta.to_dict(), meta.to_public_dict()):
            self.assertNotIn("billed_cost_usd", d)
            self.assertNotIn("cost_basis", d)

    def test_cost_per_pass_includes_failed_runs_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("ok", total=0.05))
            s.index_meta(_meta("bad", total=0.0, status="failed", passes=False))
            _call(s, "ok", "w/m", 0.05, 0.10)
            _call(s, "bad", "w/m", 0.0, 0.74)
            rows = pairing_leaderboard(s.list_runs(limit=None))
            self.assertAlmostEqual(rows[0].cost_per_pass, 0.84)


class TestCalibration(unittest.TestCase):
    def _store(self, n_own: int, n_other: int = 25) -> RunStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = RunStore(tmp.name)
        s.index_meta(_meta("r"))
        for _ in range(n_own):
            _call(s, "r", "m/own", 0.01, 0.03)       # ratio 3.0
        for _ in range(n_other):
            _call(s, "r", "m/other", 0.01, 0.02)     # ratio 2.0
        return s

    def test_twenty_priced_calls_use_the_models_own_ratio(self):
        choice = self._store(MIN_OWN_RATIO_CALLS).cost_calibration().ratio_for("m/own")
        self.assertEqual(choice.source, "own")
        self.assertAlmostEqual(choice.ratio, 3.0)
        self.assertEqual(choice.n, MIN_OWN_RATIO_CALLS)

    def test_nineteen_fall_back_to_the_global_ratio(self):
        cal = self._store(MIN_OWN_RATIO_CALLS - 1).cost_calibration()
        choice = cal.ratio_for("m/own")
        self.assertEqual(choice.source, "global")
        total_api = 19 * 0.03 + 25 * 0.02
        total_cost = 44 * 0.01
        self.assertAlmostEqual(choice.ratio, total_api / total_cost)

    def test_no_priced_calls_anywhere_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("r"))
            _call(s, "r", "m/x", 0.01, None)
            choice = s.cost_calibration().ratio_for("m/x")
            self.assertEqual(choice.source, "none")
            self.assertIsNone(choice.ratio)

    def test_dry_run_calls_are_not_calibration_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("r", dry=True))
            for _ in range(30):
                _call(s, "r", "m/x", 0.01, 0.5, dry=True)
            self.assertEqual(s.cost_calibration().ratio_for("m/x").source, "none")

    def test_unpriced_call_is_scaled_by_its_models_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("priced"))
            s.index_meta(_meta("mix"))
            for _ in range(MIN_OWN_RATIO_CALLS):
                _call(s, "priced", "m/x", 0.01, 0.04)   # ratio 4
            _call(s, "mix", "m/x", 0.01, 0.05)          # billed
            _call(s, "mix", "m/x", 0.02, None)          # calibrated: 0.02 * 4
            meta = s.get_run("mix")
            assert meta is not None
            ratio = (MIN_OWN_RATIO_CALLS * 0.04 + 0.05) / ((MIN_OWN_RATIO_CALLS + 1) * 0.01)
            self.assertAlmostEqual(meta.billed_cost_usd, 0.05 + 0.02 * ratio)
            self.assertEqual(meta.cost_basis, "mixed")

    def test_unpriced_only_run_is_calibrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("p"))
            for _ in range(MIN_OWN_RATIO_CALLS):
                _call(s, "p", "m/x", 0.01, 0.02)
            s.index_meta(_meta("u"))
            _call(s, "u", "m/x", 0.10, None)
            meta = s.get_run("u")
            assert meta is not None
            self.assertAlmostEqual(meta.billed_cost_usd, 0.20)  # 0.10 x ratio 2.0
            self.assertEqual(meta.cost_basis, "calibrated")


class TestLaunchEstimate(_Corpus):
    PAIR: ClassVar[dict[str, str]] = {"task": "corpus-landing-page", "orchestrator": "corpus/orch-a",
            "worker": "corpus/worker-cheap", "replicates": "3"}

    def test_estimate_shape(self):
        est = state.launch_estimate(self.store, FIXTURES / "models", dict(self.PAIR))
        self.assertEqual(est["basis"], "task_pairing")
        self.assertLessEqual(est["low_usd"], est["per_run_usd"])
        self.assertLessEqual(est["per_run_usd"], est["high_usd"])
        self.assertAlmostEqual(est["total_usd"], est["per_run_usd"] * 3)
        self.assertAlmostEqual(est["total_low_usd"], est["low_usd"] * 3)
        self.assertAlmostEqual(est["total_high_usd"], est["high_usd"] * 3)
        self.assertIn("billed", est["basis_label"].lower())
        self.assertRegex(est["basis_label"], r"n=\d+")
        self.assertEqual(est["monthly_cap_usd"], 50.0)
        self.assertIn("month_to_date_billed_usd", est)
        self.assertIsInstance(est["ratios"], list)
        self.assertNotIn("floor", est["caveat"].lower())

    def test_unknown_when_pairing_has_no_history(self):
        est = state.launch_estimate(self.store, FIXTURES / "models", {
            "task": "corpus-landing-page", "orchestrator": "corpus/orch-b",
            "worker": "corpus/worker-local", "replicates": "2"})
        self.assertEqual(est["basis"], "unknown")
        self.assertIsNone(est["per_run_usd"])
        self.assertIsNone(est["low_usd"])
        self.assertIsNone(est["high_usd"])
        self.assertIsNone(est["total_usd"])

    def test_dry_run_estimate_is_zero(self):
        est = state.launch_estimate(self.store, FIXTURES / "models",
                                    {**self.PAIR, "dry_run": True})
        self.assertEqual(est["total_usd"], 0.0)

    def test_estimate_independent_of_yaml_rate_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(Path(tmp) / "runs")
            for i in range(MIN_OWN_RATIO_CALLS):
                s.index_meta(_meta(f"p{i}"))
                _call(s, f"p{i}", "w/m", 0.01, 0.03)
            s.index_meta(_meta("u"))
            _call(s, "u", "w/m", 0.01, None)
            spec = {"task": "t", "orchestrator": "o/m", "worker": "w/m"}
            a = state.launch_estimate(s, _models_dir(Path(tmp), 1.0), spec)
            b = state.launch_estimate(s, _models_dir(Path(tmp), 10.0), spec)
            self.assertEqual(a["per_run_usd"], b["per_run_usd"])
            self.assertEqual(a["low_usd"], b["low_usd"])

    def test_global_ratio_label_names_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(Path(tmp) / "runs")
            s.index_meta(_meta("a"))
            for _ in range(5):
                _call(s, "a", "w/m", 0.01, 0.05)
            est = state.launch_estimate(s, _models_dir(Path(tmp)), {
                "task": "t", "orchestrator": "o/m", "worker": "w/m"})
            sources = {r["model"]: r["source"] for r in est["ratios"]}
            self.assertEqual(sources["w/m"], "global")
            self.assertIn("global", est["basis_label"])

    def test_month_to_date_counts_billed_spend_of_non_dry_runs_this_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            now = datetime(2026, 10, 15, tzinfo=UTC)
            s.index_meta(_meta("this", total=0.0, started="2026-10-03T00:00:00+00:00"))
            s.index_meta(_meta("old", total=0.0, started="2026-09-30T23:59:00+00:00"))
            s.index_meta(_meta("dry", total=0.0, dry=True, started="2026-10-04T00:00:00+00:00"))
            _call(s, "this", "w/m", 0.0, 1.25)
            _call(s, "old", "w/m", 0.0, 9.0)
            _call(s, "dry", "w/m", 0.5, None, dry=True)
            self.assertAlmostEqual(s.month_to_date_billed_usd(now), 1.25)

    def test_thread_estimate_is_calibrated_and_carries_month_to_date(self):
        est = state.thread_estimate(
            FIXTURES / "models", "corpus/judge", provider_ready=True, store=self.store)
        rate_card = (state.THREAD_INPUT_TOKENS_MAX * 0.20
                     + state.THREAD_OUTPUT_TOKENS_MAX * 0.80) / 1e6
        self.assertAlmostEqual(est["max_usd"], rate_card)
        self.assertIsNotNone(est["high_usd"])
        self.assertGreaterEqual(est["high_usd"], est["max_usd"] * 0.99)
        self.assertEqual(est["monthly_cap_usd"], 50.0)
        self.assertIn("month_to_date_billed_usd", est)


class TestBacktest(_Corpus):
    def test_held_out_estimates_contain_observed_cell_means(self):
        with self.store._connect() as conn:
            cells = conn.execute(
                "SELECT task_id, orchestrator, worker FROM runs WHERE COALESCE(dry_run, 0) = 0 "
                "AND status != 'running' GROUP BY 1, 2, 3").fetchall()
        runs = [m for m in self.store.list_runs(limit=None)
                if not m.dry_run and m.status != "running"]
        scored = hits = 0
        worst_ratio = 0.0
        for task, orch, worker in cells:
            est = self.store.billed_estimate(task, orch, worker, exclude_task_id=task)
            if est.per_run_usd is None:
                continue
            observed = statistics.mean(
                m.billed_cost_usd for m in runs
                if (m.task_id, m.orchestrator, m.worker) == (task, orch, worker))
            scored += 1
            hits += est.low_usd <= observed <= est.high_usd
            self.assertLessEqual(est.high_usd / est.low_usd, 4.0 + 1e-9)
            worst_ratio = max(worst_ratio, est.high_usd / est.low_usd)
        self.assertGreaterEqual(scored, 24)
        self.assertGreaterEqual(hits / scored, 0.80, f"{hits}/{scored}")


class TestStatsBasis(unittest.TestCase):
    def _store(self) -> RunStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = RunStore(tmp.name)
        s.index_meta(_meta("a", total=0.01))
        s.index_meta(_meta("b", total=0.0, status="failed", passes=False))
        _call(s, "a", "w/m", 0.01, 0.07)
        _call(s, "b", "w/m", 0.0, 0.74)
        return s

    def test_cells_default_to_billed_and_json_callers_keep_the_rate_card(self):
        runs = self._store().list_runs(limit=None)
        self.assertAlmostEqual(aggregate(runs)[0].cost_total, 0.81)
        self.assertAlmostEqual(aggregate(runs, cost_basis="rate_card")[0].cost_total, 0.01)

    def test_report_json_is_unchanged(self):
        s = self._store()

        def report(*flags: str) -> Any:
            args = harness.build_parser().parse_args(
                ["--runs-dir", str(s.root), "report", "--json", *flags])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                args.func(args)
            return json.loads(out.getvalue())

        runs = report()
        self.assertEqual({r["run_id"]: r["total_cost_usd"] for r in runs},
                         {"a": 0.01, "b": 0.0})
        for r in runs:
            self.assertNotIn("billed_cost_usd", r)
        board = report("--leaderboard")
        self.assertAlmostEqual(board[0]["cost_total"], 0.01)   # rate-card basis, as before
        self.assertAlmostEqual(board[0]["cost_per_pass"], 0.01)
        cells = report("--groups")
        self.assertAlmostEqual(cells[0]["cost_total"], 0.01)


class TestGroupBudget(unittest.TestCase):
    def test_group_spend_reads_billed_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("a", group="m:x"))
            _call(s, "a", "w/m", 0.01, 0.50)           # rate card 0.01, billed 0.50
            _call(s, "a", "w/m", 0.01, None)           # unpriced: 0.01 x global ratio (50x)
            self.assertAlmostEqual(s.group_spend("m:"), 0.50 + 0.01 * 50.0)

    def test_rate_card_under_budget_while_billed_is_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("big", group="m:t:o/m:w/m:baseline"))
            _call(s, "big", "w/m", 0.01, 1.20)
            self.assertGreaterEqual(s.group_spend("m:"), 1.0)
            with s._connect() as conn:
                rate_card = conn.execute("SELECT SUM(cost_usd) FROM calls").fetchone()[0]
            self.assertLess(rate_card, 1.0)

    def test_spend_today_reads_billed_and_falls_back_for_call_less_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = RunStore(tmp)
            s.index_meta(_meta("failed", total=0.0, status="failed", passes=False))
            _call(s, "failed", "w/m", 0.0, 0.74)
            s.index_meta(_meta("legacy", total=0.30))          # no call rows at all
            self.assertAlmostEqual(s.spend_today(), 1.04)


class TestRepTargetUnchanged(_Corpus):
    """Characterization: replicate planning keeps the rate-card basis (KTD7)."""

    def test_rep_target_on_the_corpus_is_what_it_was_before_u8(self):
        expect = {
            ("corpus-landing-page", "corpus/orch-a", "corpus/worker-cheap"): (100, 0.011056550212765958),
            ("corpus-landing-page", "corpus/orch-b", "corpus/worker-hot"): (100, 0.018083529324324325),
            ("corpus-sql-revenue", "corpus/orch-b", "corpus/worker-hot"): (96, 0.02104723472972973),
            ("corpus-landing-page", "corpus/orch-a", "corpus/worker-fresh"): (5, None),
            ("corpus-failed-task", "corpus/orch-b", "corpus/worker-hot"): (5, None),
        }
        for (task, orch, worker), (reps, est) in expect.items():
            got_reps, got_est = rep_target(self.store, Cell(task, orch, worker), 2.0)
            self.assertEqual(got_reps, reps, (task, orch, worker))
            if est is None:
                self.assertIsNone(got_est)
            else:
                self.assertAlmostEqual(got_est, est, places=12)


class TestPayloadsReadBilledCost(_Corpus):
    def test_runs_payload_row_carries_billed_cost_and_basis(self):
        rows = state.runs_payload(self.store, group="corpus-failed", tasks_dir=FIXTURES / "tasks")
        (row,) = rows
        self.assertAlmostEqual(row["billed_cost_usd"], 0.74)
        self.assertEqual(row["cost_basis"], "billed")

    def test_run_detail_meta_carries_billed_cost(self):
        payload: dict[str, Any] = state.run_detail_payload(
            self.store, self.manifest["failed_run_id"])
        self.assertAlmostEqual(payload["meta"]["billed_cost_usd"], 0.74)
        self.assertEqual(payload["meta"]["cost_basis"], "billed")

    def test_group_cost_in_overview_groups_is_billed(self):
        rows = {g["group"]: g for g in state.groups_payload(self.store, FIXTURES / "none.yaml")}
        self.assertAlmostEqual(rows["corpus-failed"]["cost_usd"], 0.74)


if __name__ == "__main__":
    unittest.main()
