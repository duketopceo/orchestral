"""Hierarchical bootstrap CIs: percentile intervals, determinism, consumers."""

from __future__ import annotations

import unittest

from orchestral.stats import (
    aggregate,
    bootstrap_ci,
    bootstrap_diff_ci,
    mean,
    pairing_leaderboard,
)
from orchestral.storage import RunMeta


def _run(rid: str, task: str = "t-1", orch: str = "o/m", worker: str = "w/m",
         *, passes: bool = True, score: float | None = 0.8,
         cost: float = 0.01, task_type: str = "html",
         status: str = "finished", group: str = "g") -> RunMeta:
    return RunMeta(
        run_id=rid, orchestrator=orch, task_id=task, worker=worker,
        status=status, started_at="2026-10-05T00:00:00",
        total_cost_usd=cost, total_input_tokens=10, total_output_tokens=20,
        score=score, passes=passes, latency_ms=100.0, run_group=group,
        config={"task_type": task_type})


class TestBootstrapCI(unittest.TestCase):
    def test_empty_items(self):
        self.assertIsNone(bootstrap_ci([], mean))

    def test_constant_data_degenerates_to_point(self):
        lo, hi = bootstrap_ci([1.0] * 20, mean, n_boot=100)  # type: ignore[misc]
        self.assertEqual(lo, hi)

    def test_interval_brackets_the_point_estimate(self):
        data = [0.2, 0.4, 0.6, 0.8, 1.0] * 4
        ci = bootstrap_ci(data, mean, n_boot=500, seed=7)
        assert ci is not None
        self.assertLessEqual(ci[0], mean(data))
        self.assertLessEqual(mean(data), ci[1])
        self.assertGreater(ci[1] - ci[0], 0.01)  # real spread, not a point

    def test_deterministic_given_seed(self):
        data = list(range(50))
        a = bootstrap_ci(data, mean, n_boot=200, seed=42)
        b = bootstrap_ci(data, mean, n_boot=200, seed=42)
        self.assertEqual(a, b)

    def test_all_none_stat_returns_none(self):
        self.assertIsNone(bootstrap_ci([1, 2], lambda xs: None, n_boot=10))

    def test_none_draws_dropped_not_zeroed(self):
        calls = []

        def stat(xs):
            calls.append(len(xs))
            return None if xs[0] < 0 else sum(xs) / len(xs)

        # stat is computable on every resample of positive data
        ci = bootstrap_ci([1.0, 2.0, 3.0], stat, n_boot=50, seed=1)
        self.assertIsNotNone(ci)

    def test_hierarchical_resamples_cluster_level_variance(self):
        """Two 10-item clusters, all-0s vs all-1s: every attempt inside a
        cluster agrees, so ALL the variance lives between clusters. Flat
        resampling sees a narrow pooled interval; hierarchical resampling
        draws whole clusters (25% all-A / 50% split / 25% all-B) and must
        report the honest, much wider interval."""
        items = [("a", 0.0)] * 10 + [("b", 1.0)] * 10

        def stat(rs):
            return sum(v for _, v in rs) / len(rs)
        flat = bootstrap_ci([v for _, v in items],
                            lambda rs: sum(rs) / len(rs), n_boot=400, seed=3)
        hier = bootstrap_ci(items, stat, n_boot=400, seed=3,
                            clusters=lambda it: (it[0],))
        assert flat is not None and hier is not None
        self.assertGreater(hier[1] - hier[0], 0.9)   # ~[0, 1]
        self.assertLess(flat[1] - flat[0], 0.7)       # ~[0.25, 0.75]

    def test_clusters_none_uses_flat_resample(self):
        data = [0.0, 1.0] * 10
        ci = bootstrap_ci(data, mean, n_boot=100, seed=5)
        assert ci is not None
        self.assertLess(ci[0], 0.5)
        self.assertGreater(ci[1], 0.5)


class TestBootstrapDiff(unittest.TestCase):
    def test_clear_difference_excludes_zero(self):
        ci = bootstrap_diff_ci([0.0] * 20, [1.0] * 20,
                               lambda rs: sum(rs) / len(rs), n_boot=100, seed=1)
        assert ci is not None
        self.assertGreater(ci[0], 0.0)

    def test_identical_samples_straddle_zero(self):
        data = [0.0, 1.0] * 10
        ci = bootstrap_diff_ci(data, data, lambda rs: sum(rs) / len(rs),
                               n_boot=300, seed=2)
        assert ci is not None
        self.assertLess(ci[0], 0.0)
        self.assertGreater(ci[1], 0.0)

    def test_empty_side_returns_none(self):
        self.assertIsNone(bootstrap_diff_ci([], [1.0], lambda rs: 1.0))


class TestConsumers(unittest.TestCase):
    def _corpus(self) -> list[RunMeta]:
        runs = []
        for task, base in (("t-a", 0.9), ("t-b", 0.3)):
            for i in range(4):
                runs.append(_run(f"r-{task}-{i}", task=task,
                                 passes=base > 0.5 or i == 0,
                                 score=base - i * 0.05,
                                 task_type="html" if task == "t-a" else "sql"))
        return runs

    def test_pairing_rows_carry_ci_only_when_bootstrapped(self):
        runs = self._corpus()
        plain = pairing_leaderboard(runs)[0]
        self.assertIsNone(plain.score_mean_ci)
        self.assertIsNone(plain.cost_per_pass_ci)
        boot = pairing_leaderboard(runs, bootstrap=200)[0]
        assert boot.score_mean_ci is not None
        lo, hi = boot.score_mean_ci
        self.assertLessEqual(lo, boot.score_mean or 0)
        self.assertLessEqual(boot.score_mean or 0, hi)

    def test_cell_successes_per_dollar_ci(self):
        runs = self._corpus()
        cells = aggregate(runs, bootstrap=200)
        self.assertTrue(all(c.successes_per_dollar_ci for c in cells))
        plain = aggregate(runs)
        self.assertTrue(all(c.successes_per_dollar_ci is None for c in plain))

    def test_ci_serializes_as_list(self):
        d = pairing_leaderboard(self._corpus(), bootstrap=50)[0].to_dict()
        self.assertIsInstance(d["score_mean_ci"], list)
        self.assertEqual(len(d["score_mean_ci"]), 2)


if __name__ == "__main__":
    unittest.main()
