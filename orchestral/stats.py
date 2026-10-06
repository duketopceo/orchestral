"""Aggregate statistics over runs for replicate/variance analysis.

Pure functions over RunMeta lists — the CLI and reports group runs into
cells of (run_group, task, orchestrator, worker) and summarize each cell
so a pairing comparison carries variance instead of single runs.
"""

from __future__ import annotations

import math
import random
import statistics
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, cast

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
    successes_per_dollar_ci: tuple[float, float] | None = None
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
            "successes_per_dollar_ci": list(self.successes_per_dollar_ci)
                if self.successes_per_dollar_ci else None,
            "failures": self.failures,
        }


def run_cost(run: RunMeta, cost_basis: str = "billed") -> float:
    """One run's cost under `cost_basis`: ``billed`` (the provider's bill,
    failed runs included, falling back to the rate card for in-memory metas
    that were never read from a store) or ``rate_card`` (the recorded
    `total_cost_usd`, which `--json` outputs keep for compatibility)."""
    if cost_basis == "rate_card":
        return run.total_cost_usd
    return run.display_cost_usd


def _passes_per_cost(rs: list[RunMeta], cost_basis: str) -> float | None:
    """Successes per dollar of a resampled cell; None on zero cost."""
    passed = sum(1 for r in rs if r.passes)
    cost = math.fsum(run_cost(r, cost_basis) for r in rs)
    return passed / cost if cost > 0 else None


def _scored_mean(rs: list[RunMeta]) -> float | None:
    xs = [r.score for r in rs if r.score is not None]
    return statistics.fmean(xs) if xs else None


def _cost_per_pass(rs: list[RunMeta], cost_basis: str) -> float | None:
    passed = sum(1 for r in rs if r.passes)
    cost = math.fsum(run_cost(r, cost_basis) for r in rs)
    return cost / passed if passed and cost > 0 else None


def _macro_pass(rs: list[RunMeta]) -> float | None:
    """Mean of per-task pass rates — every task weighs the same regardless
    of how much spend it attracted."""
    per_task: dict[str, list[int]] = {}
    for r in rs:
        if r.status != "finished":
            continue
        st = per_task.setdefault(r.task_id, [0, 0])
        st[1] += 1
        st[0] += 1 if r.passes else 0
    if not per_task:
        return None
    return statistics.fmean(p / n for p, n in per_task.values())


def aggregate(runs: Iterable[RunMeta], *, by_group: bool = True,
              cost_basis: str = "billed", bootstrap: int = 0) -> list[CellAggregate]:
    """Group runs into cells and summarize each.

    Costs read billed spend by default; `cost_basis="rate_card"` keeps the
    recorded rate-card totals.

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
        costs = [run_cost(r, cost_basis) for r in cell]
        latencies = [r.latency_ms for r in cell if r.latency_ms]
        tokens = [r.total_input_tokens + r.total_output_tokens for r in cell]
        cost_total = math.fsum(costs)
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
            successes_per_dollar_ci=(
                bootstrap_ci(
                    cell, lambda rs: _passes_per_cost(rs, cost_basis),
                    n_boot=bootstrap,
                    seed=_seed_for("spd", group, task_id, orch, worker))
                if bootstrap else None
            ),
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
    macro_pass_rate: float | None = None
    macro_pass_rate_ci: tuple[float, float] | None = None
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
    score_mean_ci: tuple[float, float] | None = None
    cost_per_pass_ci: tuple[float, float] | None = None
    failures: dict[str, int] = field(default_factory=dict)
    low_sample: bool = True
    holdout_runs: int = 0
    holdout_only: bool = False
    on_frontier: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "orchestrator": self.orchestrator,
            "worker": self.worker,
            "runs": self.runs,
            "finished": self.finished,
            "passed": self.passed,
            "tasks_covered": self.tasks_covered,
            "pass_rate": self.pass_rate,
            "macro_pass_rate": self.macro_pass_rate,
            "macro_pass_rate_ci": list(self.macro_pass_rate_ci)
                if self.macro_pass_rate_ci else None,
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
            "score_mean_ci": list(self.score_mean_ci) if self.score_mean_ci else None,
            "cost_per_pass_ci": list(self.cost_per_pass_ci) if self.cost_per_pass_ci else None,
            "failures": self.failures,
            "low_sample": self.low_sample,
            "holdout_runs": self.holdout_runs,
            "holdout_only": self.holdout_only,
            "on_frontier": self.on_frontier,
        }


def is_holdout_run(run: RunMeta) -> bool:
    """Was this run produced from the unpublished holdout arm?

    Read from the run config the runner wrote, so a run's arm membership travels
    with the run instead of being re-derived from a tasks/ directory that no
    longer holds the spec that produced it.
    """
    return bool((run.config or {}).get("holdout"))


def pairing_leaderboard(
    runs: Iterable[RunMeta],
    *,
    min_samples: int = MIN_LEADERBOARD_SAMPLES,
    unmetered_workers: Iterable[str] | None = None,
    cost_basis: str = "billed",
    bootstrap: int = 0,
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

    Holdout runs are counted in `holdout_runs` and left out of every rate, median,
    and cost figure. A published leaderboard that quietly drops the harder arm
    would rank pairings on the easier half of the evidence, so the exclusion is
    reported as a number the caller is expected to print. A pairing that has
    *only* holdout runs is returned as `holdout_only` rather than dropped, so a
    pairing that was measured and then filtered away cannot look like a pairing
    that was never measured.
    """
    unmetered = set(unmetered_workers or ())
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
        judge_scores: list[float] = []
        for r in finished:
            if r.judge_score is not None:
                judge_scores.append(r.judge_score)
        judged = [r for r in finished if r.judge_score is not None or r.judge_passed is not None]
        judge_approved = sum(1 for r in judged if r.judge_passed is True)
        costs = [run_cost(r, cost_basis) for r in finished]
        latencies = [r.latency_ms for r in finished if r.latency_ms]
        cost_total = math.fsum(run_cost(r, cost_basis) for r in cell)
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
            # pooled pass rate silently weighs tasks by where spend went;
            # macro gives every task equal say — the publishable headline
            macro_pass_rate=_macro_pass(finished),
            macro_pass_rate_ci=(
                bootstrap_ci(
                    finished, _macro_pass,
                    clusters=lambda r: (run_task_type(r), r.task_id),
                    n_boot=bootstrap,
                    seed=_seed_for("macro", orch, worker))
                if bootstrap and finished else None
            ),
            score_median=statistics.median(scored) if scored else None,
            score_mean=mean(scored) if scored else None,
            judge_score_median=statistics.median(judge_scores) if judge_scores else None,
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
            score_mean_ci=(
                bootstrap_ci(
                    finished, _scored_mean,
                    clusters=lambda r: (run_task_type(r), r.task_id),
                    n_boot=bootstrap,
                    seed=_seed_for("score", orch, worker))
                if bootstrap and finished else None
            ),
            cost_per_pass_ci=(
                bootstrap_ci(
                    cell, lambda rs: _cost_per_pass(rs, cost_basis),
                    clusters=lambda r: (run_task_type(r), r.task_id),
                    n_boot=bootstrap,
                    seed=_seed_for("cpp", orch, worker))
                if bootstrap else None
            ),
            failures=failures,
            low_sample=len(finished) < min_samples,
            holdout_runs=holdout.get((orch, worker), 0),
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
    mark_pareto_front(out)
    return out + holdout_only


def mark_pareto_front(rows: list[PairingAggregate]) -> None:
    """Flag rows on the quality/cost Pareto frontier (``on_frontier``).

    A row is frontier when no other eligible row is at least as good on
    macro pass rate AND at most as expensive per pass, strictly better on
    one. Eligibility requires a measured quality number and a metered
    ``cost_per_pass`` from a credible sample: unmetered and low_sample
    rows cannot nominate a frontier point because their evidence is a
    subsidy or a guess, not a trade-off. Identical twins co-exist —
    neither strictly dominates the other.
    """
    eligible = [
        r for r in rows
        if not r.low_sample and not r.holdout_only
        and r.cost_per_pass is not None
        and _quality(r) is not None
    ]
    pts = [(cast(float, _quality(r)), cast(float, r.cost_per_pass), r)
           for r in eligible]
    for r in rows:
        r.on_frontier = False
    for q, c, r in pts:
        r.on_frontier = not any(
            o is not r and oq >= q and oc <= c and (oq > q or oc < c)
            for oq, oc, o in pts)


def _quality(r: PairingAggregate) -> float | None:
    """The frontier's quality axis: macro pass rate, pooled as fallback."""
    return r.macro_pass_rate if r.macro_pass_rate is not None else r.pass_rate


def horizon_fit(points: list[tuple[float, bool]]) -> dict[str, Any] | None:
    """METR-style time-horizon fit: logistic p(pass) over log task minutes.

    ``points`` are (human_minutes, passed) per finished run. Returns t50 —
    the task length where the pairing passes half the time — plus slope
    and counts, or None when the fit is undefined: fewer than two
    distinct durations, a single outcome class, too few points, or a
    non-negative slope (success that does not decay with length has no
    horizon to name). Newton-Raphson on two parameters; no new deps.
    """
    pts = [(math.log(m), 1.0 if ok else 0.0) for m, ok in points if m > 0]
    if len(pts) < 6 or len({x for x, _ in pts}) < 2 or len({y for _, y in pts}) < 2:
        return None
    # exact MLE first; ridge-regularized retry only when the unpenalized
    # fit diverges (quasi-separated outcomes collapse the Hessian)
    fit = _logit2(pts, 0.0)
    if fit is None:
        fit = _logit2(pts, 1.0)
    if fit is None or fit[1] >= -1e-9:
        return None
    b0, b1 = fit
    return {
        "t50_minutes": math.exp(-b0 / b1),
        "slope": b1,
        "n": len(pts),
        "tasks": len({x for x, _ in pts}),
    }


def _logit2(pts: list[tuple[float, float]], lam: float) -> tuple[float, float] | None:
    """Newton-Raphson for intercept/slope; None when it cannot converge."""
    b0 = b1 = 0.0
    for _ in range(50):
        g0 = g1 = h01 = 0.0
        h00 = h11 = lam
        for x, y in pts:
            z = b0 + b1 * x
            # stable sigmoid — separable data drives |z| to overflow
            p = 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))
            w = p * (1 - p)
            g0 += y - p
            g1 += (y - p) * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        g0 -= lam * b0
        g1 -= lam * b1
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            return None
        d0 = (g0 * h11 - g1 * h01) / det
        d1 = (g1 * h00 - g0 * h01) / det
        b0 += d0
        b1 += d1
        if not (math.isfinite(b0) and math.isfinite(b1)):
            return None
        if abs(d0) < 1e-9 and abs(d1) < 1e-9:
            return b0, b1
    return None


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


_Z_95 = 1.96


def wilson_interval(passes: int, n: int) -> tuple[float, float] | None:
    """Wilson 95% score interval on a binomial pass rate.

    Returns ``(lower, upper)`` unrounded — callers round at presentation
    so the interval composes inside ``diff_ci`` without double-rounding.
    ``None`` when n <= 0.
    """
    if n <= 0:
        return None
    p = passes / n
    denom = 1 + _Z_95 * _Z_95 / n
    center = (p + _Z_95 * _Z_95 / (2 * n)) / denom
    margin = (
        _Z_95
        * ((p * (1 - p) / n + _Z_95 * _Z_95 / (4 * n * n)) ** 0.5)
        / denom
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def diff_ci(
    a_passes: int, a_n: int, b_passes: int, b_n: int
) -> tuple[float, float] | None:
    """Newcombe score interval (method 10) for ``rate_b - rate_a``.

    Positive values mean arm B beats arm A. Returns ``None`` when either
    arm has no observations. Unpaired — the correct shape here since no
    seed reaches the chat providers.
    """
    a = wilson_interval(a_passes, a_n)
    b = wilson_interval(b_passes, b_n)
    if a is None or b is None:
        return None
    (a_lo, a_hi), (b_lo, b_hi) = a, b
    p_a, p_b = a_passes / a_n, b_passes / b_n
    d = p_b - p_a
    lo = d - ((p_b - b_lo) ** 2 + (a_hi - p_a) ** 2) ** 0.5
    hi = d + ((b_hi - p_b) ** 2 + (p_a - a_lo) ** 2) ** 0.5
    return max(-1.0, lo), min(1.0, hi)


def diff_verdict(
    a_passes: int, a_n: int, b_passes: int, b_n: int, eps: float
) -> str:
    """Cell verdict from the difference interval half-width.

    ``lift``/``harm`` when the interval clears zero on one side,
    ``resolved`` when it is tighter than ``eps`` around a non-significant
    difference, ``inconclusive`` otherwise. ``pending``/``insufficient``
    cover the no-data and one-arm-empty cases so coverage never prints a
    bare dash.
    """
    if a_n <= 0 and b_n <= 0:
        return "pending"
    if a_n <= 0 or b_n <= 0:
        return "insufficient"
    ci = diff_ci(a_passes, a_n, b_passes, b_n)
    assert ci is not None  # both arms non-empty
    lo, hi = ci
    if lo > 0:
        return "lift"
    if hi < 0:
        return "harm"
    if (hi - lo) / 2 <= eps:
        return "resolved"
    return "inconclusive"


def bradley_terry(
    battles: Iterable[tuple[str, str, str]],
) -> dict[str, dict[str, float]]:
    """Fit Bradley-Terry ratings over pairwise battle outcomes.

    ``battles`` is an iterable of ``(player_a, player_b, outcome)`` where
    outcome is ``"a"``, ``"b"``, or ``"tie"``. Ties contribute a half-win
    to each side — the standard Rao-Kupper-free approximation, adequate at
    the battle counts a judge budget produces. Same-player battles are
    dropped.

    Ratings are exponentiated log-strengths normalized to geometric mean
    1 (MM iteration). ``theta_se`` is the observed-Fisher-information
    standard error on the log scale — it understates the true interval
    when a player wins or loses every battle (perfect separation), so
    treat the interval as a floor, not a ceiling.
    """
    players: list[str] = []
    seen: set[str] = set()
    wins: dict[str, float] = {}
    played: dict[tuple[str, str], int] = {}
    for a, b, outcome in battles:
        if a == b or outcome not in ("a", "b", "tie"):
            continue
        for p in (a, b):
            if p not in seen:
                seen.add(p)
                players.append(p)
                wins[p] = 0.0
        pair = (a, b) if a < b else (b, a)
        played[pair] = played.get(pair, 0) + 1
        if outcome == "tie":
            wins[a] += 0.5
            wins[b] += 0.5
        else:
            wins[a if outcome == "a" else b] += 1.0
    if not players:
        return {}

    # A weak anchor keeps perfect separation finite: every player carries
    # _PRIOR pseudo-battles tied against a fixed strength-1 opponent, so an
    # undefeated player tops out instead of diverging (and the winless
    # never hit 0).
    _PRIOR = 1.0
    wins_obs = dict(wins)
    for p in players:
        wins[p] += _PRIOR / 2

    strength = dict.fromkeys(players, 1.0)
    for _ in range(200):
        prev = dict(strength)
        for p in players:
            denom = _PRIOR / (strength[p] + 1.0)
            for (a, b), n in played.items():
                if p in (a, b):
                    other = b if p == a else a
                    denom += n / (strength[p] + strength[other])
            strength[p] = wins[p] / denom if denom > 0 else prev[p]
        geo = math.exp(sum(math.log(max(v, 1e-300)) for v in strength.values()) / len(players))
        strength = {p: v / geo for p, v in strength.items()}
        if max(abs(strength[p] - prev[p]) for p in players) < 1e-9:
            break

    out: dict[str, dict[str, float]] = {}
    for p in players:
        info = _PRIOR * strength[p] / (strength[p] + 1.0) ** 2
        for (a, b), n in played.items():
            if p in (a, b):
                other = b if p == a else a
                info += n * strength[p] * strength[other] / (strength[p] + strength[other]) ** 2
        theta = math.log(max(strength[p], 1e-300))
        out[p] = {
            "rating": strength[p],
            "theta": theta,
            "theta_se": (1.0 / info) ** 0.5 if info > 0 else float("inf"),
            "wins": wins_obs[p],
            "battles": sum(n for pair, n in played.items() if p in pair),
        }
    return out


def _seed_for(*parts: str) -> int:
    """Deterministic per-entity seed — Python's hash() is salted per process,
    so a stable CRC keeps bootstrap draws reproducible across invocations."""
    return zlib.crc32("|".join(parts).encode())


def bootstrap_ci(
    items: Iterable[Any],
    stat: Callable[[list[Any]], float | None],
    *,
    clusters: Callable[[Any], tuple[str, ...]] | None = None,
    n_boot: int = 999,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval for ``stat(items)``.

    With ``clusters`` — a callable returning each item's hierarchy path,
    outermost first (e.g. ``(task_type, task_id)``) — this is a hierarchical
    bootstrap: every level resamples with replacement inside its parent, so
    the interval reflects between-cluster variance, not just within-cell
    noise. Without it, items resample flat. Draws where ``stat`` returns
    ``None`` drop out; an all-None distribution returns ``None`` rather than
    inventing a zero-width interval on no evidence.
    """
    items = list(items)
    if not items:
        return None
    rng = random.Random(seed)

    if clusters is None:
        def draw() -> list[Any]:
            return [rng.choice(items) for _ in items]
    else:
        leaf_key = "\x00items"
        root: dict[str, Any] = {}
        for it in items:
            node = root
            for key in clusters(it):
                node = node.setdefault(key, {})
            node.setdefault(leaf_key, []).append(it)

        def draw() -> list[Any]:
            out: list[Any] = []

            def walk(node: dict[str, Any]) -> None:
                keys = [k for k in node if k != leaf_key]
                for _ in keys:
                    walk(node[rng.choice(keys)])
                leaf = node.get(leaf_key)
                if leaf:
                    out.extend(rng.choice(leaf) for _ in leaf)

            walk(root)
            return out

    draws: list[float] = []
    for _ in range(n_boot):
        v = stat(draw())
        if v is not None:
            draws.append(v)
    if not draws:
        return None
    return (percentile(draws, alpha / 2 * 100),
            percentile(draws, (1 - alpha / 2) * 100))


def bootstrap_diff_ci(
    items_a: Iterable[Any],
    items_b: Iterable[Any],
    stat: Callable[[list[Any]], float | None],
    *,
    n_boot: int = 999,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval for ``stat(b) - stat(a)``.

    Each draw resamples each side independently — the right frame when the
    two samples are unpaired (baseline vs candidate arm, same task cell)."""
    a, b = list(items_a), list(items_b)
    if not a or not b:
        return None
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_boot):
        sa = stat([rng.choice(a) for _ in a])
        sb = stat([rng.choice(b) for _ in b])
        if sa is not None and sb is not None:
            draws.append(sb - sa)
    if not draws:
        return None
    return (percentile(draws, alpha / 2 * 100),
            percentile(draws, (1 - alpha / 2) * 100))
