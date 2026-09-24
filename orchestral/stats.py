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
    judge_score_mean: float | None = None
    judge_score_sd: float = 0.0
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
            "judge_score_mean": self.judge_score_mean,
            "judge_score_sd": self.judge_score_sd,
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
        judged = [r.judge_score for r in finished if r.judge_score is not None]
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
            # capability axis: crashed/infra-failed runs are noise, not
            # evidence against the pairing — pass rate is over finished runs
            pass_rate=passed / len(finished) if finished else None,
            score_mean=mean(scored) if scored else None,
            score_sd=stdev(scored),
            judge_score_mean=mean(judged) if judged else None,
            judge_score_sd=stdev(judged),
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
MIN_LEADERBOARD_SAMPLES = 3


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
    judge_score_median: float | None = None
    judged: int = 0
    judge_approved: int = 0
    judge_pass_rate: float | None = None
    cost_median: float = 0.0
    cost_total: float = 0.0
    duration_median_ms: float = 0.0
    duration_p90_ms: float = 0.0
    failure_rate: float | None = None
    cost_per_pass: float | None = None
    failures: dict[str, int] = field(default_factory=dict)
    low_sample: bool = True

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
            "judge_score_median": self.judge_score_median,
            "judged": self.judged,
            "judge_approved": self.judge_approved,
            "judge_pass_rate": self.judge_pass_rate,
            "cost_median": self.cost_median,
            "cost_total": self.cost_total,
            "duration_median_ms": self.duration_median_ms,
            "duration_p90_ms": self.duration_p90_ms,
            "failure_rate": self.failure_rate,
            "cost_per_pass": self.cost_per_pass,
            "failures": self.failures,
            "low_sample": self.low_sample,
        }


def pairing_leaderboard(
    runs: Iterable[RunMeta],
    *,
    min_samples: int = MIN_LEADERBOARD_SAMPLES,
    unmetered_workers: Iterable[str] | None = None,
) -> list[PairingAggregate]:
    """Aggregate runs into leaderboard rows keyed on (orchestrator, worker).

    Medians, pass_rate, and `low_sample` are over finished runs only —
    a crashed run is infra noise, not evidence about the pairing, so three
    crashes and zero finishes cannot rank. `failure_rate` stays over all
    runs: infra fragility is real signal, just a different axis.

    `unmetered_workers` carries worker slugs whose calls are declared
    unmetered (free/local agent CLIs). A $0 total must not read as a free
    `cost_per_pass` — measured-zero and unmetered rows get None and sort
    last, since "the meter read nothing" is not "this costs nothing".
    """
    unmetered = set(unmetered_workers or ())
    cells: dict[tuple[str, str], list[RunMeta]] = {}
    for r in runs:
        cells.setdefault((r.orchestrator, r.worker), []).append(r)

    out: list[PairingAggregate] = []
    for (orch, worker), cell in cells.items():
        n = len(cell)
        finished = [r for r in cell if r.status == "finished"]
        passed = sum(1 for r in cell if r.passes)
        scored = [r.score for r in finished if r.score is not None]
        judge_scored = [r for r in finished if r.judge_score is not None]
        judged = [r for r in finished if r.judge_score is not None or r.judge_passed is not None]
        judge_approved = sum(1 for r in judged if r.judge_passed is True)
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
            pass_rate=passed / len(finished) if finished else None,
            score_median=statistics.median(scored) if scored else None,
            score_mean=mean(scored) if scored else None,
            judge_score_median=statistics.median([r.judge_score for r in judge_scored]) if judge_scored else None,
            judged=len(judged),
            judge_approved=judge_approved,
            judge_pass_rate=(judge_approved / len(judged)) if judged else None,
            cost_median=statistics.median(costs) if costs else 0.0,
            cost_total=cost_total,
            duration_median_ms=statistics.median(latencies) if latencies else 0.0,
            duration_p90_ms=percentile(latencies, 90) if latencies else 0.0,
            failure_rate=failed_n / n if n else None,
            cost_per_pass=(
                None
                if worker in unmetered or not passed or cost_total <= 0
                else cost_total / passed
            ),
            failures=failures,
            low_sample=len(finished) < min_samples,
        ))
    # thin samples never rank: low_sample rows always tail, whatever the
    # metric — a 1-run 100% pairing is anecdote, not a placement.
    # The headline order answers "which pairing performs best": pass rate
    # first, cost per pass breaks ties.
    out.sort(key=lambda p: (
        p.low_sample,
        -(p.pass_rate or 0.0),
        p.cost_per_pass is None, p.cost_per_pass or 0.0,
        p.orchestrator, p.worker,
    ))
    return out
