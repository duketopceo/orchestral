"""Aggregate statistics over runs for replicate/variance analysis.

Pure functions over RunMeta lists — the CLI and reports group runs into
cells of (run_group, task, orchestrator, worker) and summarize each cell
so a pairing comparison carries variance instead of single runs.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from orchestral.storage import RunMeta


def mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return statistics.fmean(xs) if xs else 0.0


def stdev(xs: Iterable[float]) -> float:
    """Sample standard deviation; 0.0 when fewer than two values."""
    xs = list(xs)
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def percentile(xs: Iterable[float], p: float) -> float:
    """Linear-interpolated percentile; p in [0, 100]."""
    s = sorted(xs)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


@dataclass
class CellAggregate:
    """Summary statistics for one (group, task, orchestrator, worker) cell."""

    run_group: str
    task_id: str
    orchestrator: str
    worker: str
    runs: int = 0
    finished: int = 0
    passed: int = 0
    pass_rate: float | None = None
    score_mean: float | None = None
    score_sd: float = 0.0
    cost_mean: float = 0.0
    cost_sd: float = 0.0
    cost_total: float = 0.0
    latency_p50: float = 0.0
    latency_p95: float = 0.0
    tokens_mean: float = 0.0
    successes_per_dollar: float | None = None
    failures: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_group": self.run_group,
            "task_id": self.task_id,
            "orchestrator": self.orchestrator,
            "worker": self.worker,
            "runs": self.runs,
            "finished": self.finished,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "score_mean": self.score_mean,
            "score_sd": self.score_sd,
            "cost_mean": self.cost_mean,
            "cost_sd": self.cost_sd,
            "cost_total": self.cost_total,
            "latency_p50": self.latency_p50,
            "latency_p95": self.latency_p95,
            "tokens_mean": self.tokens_mean,
            "successes_per_dollar": self.successes_per_dollar,
            "failures": self.failures,
        }


def aggregate(runs: Iterable[RunMeta], *, by_group: bool = True) -> list[CellAggregate]:
    """Group runs into cells and summarize each.

    `by_group=True` keys cells on (run_group, task, orchestrator, worker)
    so separate experiments stay separate; `False` drops the group key for
    a pairing rollup across all groups. Unlabeled runs land in group "".
    """
    cells: dict[tuple[str, str, str, str], list[RunMeta]] = {}
    for r in runs:
        key = (
            (r.run_group or "") if by_group else "",
            r.task_id,
            r.orchestrator,
            r.worker,
        )
        cells.setdefault(key, []).append(r)

    out: list[CellAggregate] = []
    for (group, task_id, orch, worker), cell in cells.items():
        n = len(cell)
        finished = [r for r in cell if r.status == "finished"]
        passed = sum(1 for r in cell if r.passes)
        scored = [r.score for r in finished if r.score is not None]
        costs = [r.total_cost_usd for r in cell]
        latencies = [r.latency_ms for r in cell if r.latency_ms]
        tokens = [r.total_input_tokens + r.total_output_tokens for r in cell]
        cost_total = sum(costs)
        failures: dict[str, int] = {}
        for r in cell:
            if r.failure_reason:
                failures[r.failure_reason] = failures.get(r.failure_reason, 0) + 1
        out.append(CellAggregate(
            run_group=group,
            task_id=task_id,
            orchestrator=orch,
            worker=worker,
            runs=n,
            finished=len(finished),
            passed=passed,
            pass_rate=passed / n if n else None,
            score_mean=mean(scored) if scored else None,
            score_sd=stdev(scored),
            cost_mean=mean(costs),
            cost_sd=stdev(costs),
            cost_total=cost_total,
            latency_p50=percentile(latencies, 50),
            latency_p95=percentile(latencies, 95),
            tokens_mean=mean(tokens),
            successes_per_dollar=passed / cost_total if cost_total > 0 else None,
            failures=failures,
        ))
    out.sort(key=lambda c: (c.run_group, c.task_id, c.orchestrator, c.worker))
    return out


# Below this a pairing needs repeated evidence before a ranking means
# anything — one lucky run is anecdote, not a benchmark result.
MIN_LEADERBOARD_SAMPLES = 10


@dataclass
class PairingAggregate:
    """Leaderboard row: one (orchestrator, worker) pairing across all runs."""

    orchestrator: str
    worker: str
    runs: int = 0
    finished: int = 0
    passed: int = 0
    tasks_covered: int = 0
    pass_rate: float | None = None
    score_median: float | None = None
    score_mean: float | None = None
    cost_median: float = 0.0
    cost_total: float = 0.0
    duration_median_ms: float = 0.0
    failure_rate: float | None = None
    cost_per_pass: float | None = None
    failures: dict[str, int] = field(default_factory=dict)
    low_sample: bool = True
    holdout_runs: int = 0
    holdout_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "orchestrator": self.orchestrator,
            "worker": self.worker,
            "runs": self.runs,
            "finished": self.finished,
            "passed": self.passed,
            "tasks_covered": self.tasks_covered,
            "pass_rate": self.pass_rate,
            "score_median": self.score_median,
            "score_mean": self.score_mean,
            "cost_median": self.cost_median,
            "cost_total": self.cost_total,
            "duration_median_ms": self.duration_median_ms,
            "failure_rate": self.failure_rate,
            "cost_per_pass": self.cost_per_pass,
            "failures": self.failures,
            "low_sample": self.low_sample,
            "holdout_runs": self.holdout_runs,
            "holdout_only": self.holdout_only,
        }


def is_holdout_run(run: RunMeta) -> bool:
    """Was this run produced from the unpublished holdout arm?

    Read from the run config the runner wrote, so a run's arm membership travels
    with the run instead of being re-derived from a tasks/ directory that no
    longer holds the spec that produced it.
    """
    return bool((run.config or {}).get("holdout"))


def pairing_leaderboard(
    runs: Iterable[RunMeta], *, min_samples: int = MIN_LEADERBOARD_SAMPLES
) -> list[PairingAggregate]:
    """Aggregate runs into leaderboard rows keyed on (orchestrator, worker).

    Medians over finished runs only — unfinished runs distort cost/latency
    downward. `low_sample` marks pairings under `min_samples` so a caller can
    refuse to crown a "best" on anecdotal evidence.

    Holdout runs are counted in `holdout_runs` and left out of every rate, median,
    and cost figure. A published leaderboard that quietly drops the harder arm
    would rank pairings on the easier half of the evidence, so the exclusion is
    reported as a number the caller is expected to print. A pairing that has
    *only* holdout runs is returned as `holdout_only` rather than dropped, so a
    pairing that was measured and then filtered away cannot look like a pairing
    that was never measured.
    """
    published: dict[tuple[str, str], list[RunMeta]] = {}
    holdout: dict[tuple[str, str], int] = {}
    for r in runs:
        key = (r.orchestrator, r.worker)
        if is_holdout_run(r):
            holdout[key] = holdout.get(key, 0) + 1
            continue
        published.setdefault(key, []).append(r)

    holdout_only = [
        PairingAggregate(
            orchestrator=orch,
            worker=worker,
            runs=0,
            tasks_covered=0,
            holdout_runs=count,
            holdout_only=True,
            low_sample=True,
        )
        for (orch, worker), count in sorted(holdout.items())
        if (orch, worker) not in published
    ]

    out: list[PairingAggregate] = []
    for (orch, worker), cell in published.items():
        n = len(cell)
        finished = [r for r in cell if r.status == "finished"]
        passed = sum(1 for r in cell if r.passes)
        scored = [r.score for r in finished if r.score is not None]
        costs = [r.total_cost_usd for r in finished]
        latencies = [r.latency_ms for r in finished if r.latency_ms]
        cost_total = sum(r.total_cost_usd for r in cell)
        failures: dict[str, int] = {}
        for r in cell:
            if r.failure_reason:
                failures[r.failure_reason] = failures.get(r.failure_reason, 0) + 1
        failed_n = sum(1 for r in cell if not r.passes)
        out.append(PairingAggregate(
            orchestrator=orch,
            worker=worker,
            runs=n,
            finished=len(finished),
            passed=passed,
            tasks_covered=len({r.task_id for r in cell}),
            pass_rate=passed / n if n else None,
            score_median=statistics.median(scored) if scored else None,
            score_mean=mean(scored) if scored else None,
            cost_median=statistics.median(costs) if costs else 0.0,
            cost_total=cost_total,
            duration_median_ms=statistics.median(latencies) if latencies else 0.0,
            failure_rate=failed_n / n if n else None,
            cost_per_pass=cost_total / passed if passed else None,
            failures=failures,
            low_sample=n < min_samples,
            holdout_runs=holdout.get((orch, worker), 0),
        ))
    out.sort(key=lambda p: (
        p.cost_per_pass is None, p.cost_per_pass or 0.0,
        -(p.pass_rate or 0.0), p.orchestrator, p.worker,
    ))
    return out + holdout_only


def run_task_type(run: RunMeta) -> str:
    """The task type a run graded, or "" when the run predates the field."""
    return str((run.config or {}).get("task_type") or "")


def run_score(run: RunMeta) -> float | None:
    """A comparable 0-1 score for one run, or None when it has neither.

    Prefers the graded score and falls back to the pass/fail verdict, because a
    type that only reports pass/fail still carries evidence and dropping it would
    quietly shrink an arm. Within one task type every run takes the same branch,
    so the means being subtracted are on the same scale.
    """
    if run.status != "finished":
        return None
    if run.score is not None:
        return float(run.score)
    if run.passes is True:
        return 1.0
    if run.passes is False:
        return 0.0
    return None


@dataclass
class ArmComparison:
    """Published-vs-holdout means for one task type, and the gap between them.

    The gap is only interpretable within a type. A type present in one arm and
    absent from the other has no gap: the difference would be a difference of
    subject, not of contamination, so `comparable` is False and `gap` is None.
    """

    task_type: str
    published_n: int = 0
    published_mean: float | None = None
    holdout_n: int = 0
    holdout_mean: float | None = None
    gap: float | None = None
    comparable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "published_n": self.published_n,
            "published_mean": self.published_mean,
            "holdout_n": self.holdout_n,
            "holdout_mean": self.holdout_mean,
            "gap": self.gap,
            "comparable": self.comparable,
        }


def contamination_gap(runs: Iterable[RunMeta]) -> list[ArmComparison]:
    """Compare mean score on published specs against mean score on holdout specs.

    A positive gap means the published arm scored higher than the holdout arm of
    the same type, which is the shape a contaminated benchmark takes: the
    published problems were easier to produce a high score on, whether by
    memorisation or by being easier to begin with. The output cannot tell those
    two causes apart, and does not try to — it reports the number and the sample
    size behind it so a reader can judge how much of it is noise.
    """
    published: dict[str, list[float]] = {}
    holdout: dict[str, list[float]] = {}
    for run in runs:
        score = run_score(run)
        if score is None:
            continue
        bucket = holdout if is_holdout_run(run) else published
        bucket.setdefault(run_task_type(run), []).append(score)

    out: list[ArmComparison] = []
    for task_type in sorted(set(published) | set(holdout)):
        pub = published.get(task_type, [])
        hold = holdout.get(task_type, [])
        row = ArmComparison(
            task_type=task_type or "(unrecorded)",
            published_n=len(pub),
            published_mean=mean(pub) if pub else None,
            holdout_n=len(hold),
            holdout_mean=mean(hold) if hold else None,
        )
        row.comparable = bool(pub) and bool(hold)
        if row.comparable:
            assert row.published_mean is not None and row.holdout_mean is not None
            row.gap = row.published_mean - row.holdout_mean
        out.append(row)
    return out
