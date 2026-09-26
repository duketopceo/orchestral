"""Billed-cost budget guard for the paid eval workflow.

Why this exists
---------------
`runs.total_cost_usd` and `calls.cost_usd` hold the harness's *configured
rate-card estimate*. The provider bills something else, and the two are not
close. On the 2026-09-26 eval day the estimate understated real spend by 5.3x
against OpenRouter's own key usage total. A cap written against the estimate
is decorative: it passed 83 runs that were charged $3.12.

So this guard reads `calls.api_cost_usd` -- the figure the provider puts in its
own response -- and compares *that* against the cap. Two properties matter
more than the arithmetic:

1. It sums **every run in the index**, not the newest row. `runs.total_cost_usd`
   is left at 0.0 for failed runs, so `ORDER BY started_at DESC LIMIT 1` reads a
   zero and passes while the job spends real money. In CI the index is created
   fresh by the job (`runs/` is gitignored), so the whole index is exactly the
   spend of the run being checked.

2. It **fails closed**. A run that made real provider calls but reported no
   priced call has no billed figure, and an unmeasurable run cannot be shown to
   be within budget. So does a missing index. Only a genuinely dry-run eval
   (no non-dry-run calls at all) passes without a billed figure.

Calls the provider did not price are counted at their estimate. That is a
lower bound: if the rate card is stale, the true figure is higher. The output
prints the estimate/billed ratio so the remaining understatement stays visible
instead of being assumed away.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

# Exit codes. 0 pass, 1 over budget, 2 the run cannot be audited.
EXIT_OK = 0
EXIT_OVER_BUDGET = 1
EXIT_UNAUDITABLE = 2


@dataclass(frozen=True)
class BilledCost:
    """Billed and estimated spend recorded in the run index."""

    runs: int
    calls: int
    priced_calls: int
    billed_usd: float
    unpriced_estimate_usd: float
    estimate_usd: float
    first_started_at: str | None
    last_started_at: str | None

    @property
    def total_usd(self) -> float:
        """Billed cost where known, estimate for the calls nothing priced."""
        return self.billed_usd + self.unpriced_estimate_usd

    @property
    def unpriced_calls(self) -> int:
        return self.calls - self.priced_calls

    @property
    def estimate_ratio(self) -> float | None:
        """Billed / estimated, or None when the estimate is 0 and useless."""
        if self.estimate_usd <= 0:
            return None
        return self.total_usd / self.estimate_usd


@dataclass(frozen=True)
class Verdict:
    ok: bool
    exit_code: int
    summary: str
    cost: BilledCost
    max_cost_usd: float


def read_billed_cost(db_path: str | Path) -> BilledCost:
    """Total recorded spend, preferring the provider-reported figure.

    Reads the index read-only and only from the `runs` and `calls` tables, so a
    missing or pre-`api_cost_usd` database degrades to the estimate instead of
    raising.
    """
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)) as conn:
        try:
            runs, first_at, last_at = conn.execute(
                "SELECT COUNT(*), MIN(started_at), MAX(started_at) FROM runs"
            ).fetchone()
            calls, priced, billed, unpriced_estimate, estimate = conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(api_cost_usd IS NOT NULL), 0),
                       COALESCE(SUM(api_cost_usd), 0.0),
                       COALESCE(SUM(CASE WHEN api_cost_usd IS NULL THEN cost_usd END), 0.0),
                       COALESCE(SUM(cost_usd), 0.0)
                FROM calls
                WHERE dry_run = 0
                """
            ).fetchone()
        except sqlite3.Error:
            return BilledCost(0, 0, 0, 0.0, 0.0, 0.0, None, None)
    return BilledCost(
        runs=int(runs or 0),
        calls=int(calls or 0),
        priced_calls=int(priced or 0),
        billed_usd=float(billed or 0.0),
        unpriced_estimate_usd=float(unpriced_estimate or 0.0),
        estimate_usd=float(estimate or 0.0),
        first_started_at=first_at,
        last_started_at=last_at,
    )


def check_budget(db_path: str | Path, max_cost_usd: float) -> Verdict:
    """Decide whether the indexed spend is within `max_cost_usd`."""
    cost = read_billed_cost(db_path)

    if cost.runs == 0:
        return Verdict(
            ok=False,
            exit_code=EXIT_UNAUDITABLE,
            summary=(
                f"no runs indexed in {db_path} — cannot verify spend against a "
                f"${max_cost_usd:.6g} cap"
            ),
            cost=cost,
            max_cost_usd=max_cost_usd,
        )

    if cost.calls == 0:
        return Verdict(
            ok=True,
            exit_code=EXIT_OK,
            summary=f"dry run — {cost.runs} run(s), no billed calls, nothing to charge",
            cost=cost,
            max_cost_usd=max_cost_usd,
        )

    if cost.priced_calls == 0:
        return Verdict(
            ok=False,
            exit_code=EXIT_UNAUDITABLE,
            summary=(
                f"{cost.calls} provider call(s) reported no billed cost, so the "
                f"${max_cost_usd:.6g} cap cannot be verified; estimates are not a "
                f"substitute for a provider invoice"
            ),
            cost=cost,
            max_cost_usd=max_cost_usd,
        )

    over = cost.total_usd > max_cost_usd
    return Verdict(
        ok=not over,
        exit_code=EXIT_OVER_BUDGET if over else EXIT_OK,
        summary=(
            f"billed ${cost.total_usd:.6f} against a ${max_cost_usd:.6g} cap "
            f"({cost.runs} run(s), {cost.priced_calls}/{cost.calls} call(s) priced by the provider)"
        ),
        cost=cost,
        max_cost_usd=max_cost_usd,
    )


def format_report(verdict: Verdict) -> str:
    cost = verdict.cost
    lines = [
        f"orchestral eval {verdict.summary}",
        f"  runs indexed      : {cost.runs} ({cost.first_started_at or '-'} .. {cost.last_started_at or '-'})",
        f"  provider billed   : ${cost.billed_usd:.6f} over {cost.priced_calls} call(s)",
        f"  estimated (rest)  : ${cost.unpriced_estimate_usd:.6f} over {cost.unpriced_calls} unpriced call(s)",
        f"  charged for guard : ${cost.total_usd:.6f}",
        f"  rate-card estimate: ${cost.estimate_usd:.6f} (what the old cap read)",
    ]
    ratio = cost.estimate_ratio
    if ratio is not None and abs(ratio - 1.0) > 0.01:
        lines.append(f"  estimate understated billed spend by {ratio:.2f}x")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m orchestral.budget",
        description="Fail when the paid eval's provider-billed cost exceeds a cap.",
    )
    parser.add_argument(
        "--runs-dir",
        default="runs",
        help="run index root containing index.db (default: runs)",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        required=True,
        help="cap in USD, compared against provider-billed cost",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.runs_dir)
    if db_path.is_dir():
        db_path = db_path / "index.db"

    if not db_path.exists():
        print(f"orchestral eval FAILED: no run index at {db_path} — cannot verify spend")
        return EXIT_UNAUDITABLE

    verdict = check_budget(db_path, args.max_cost_usd)
    print(format_report(verdict))
    if verdict.exit_code == EXIT_OVER_BUDGET:
        print(f"orchestral eval cost budget exceeded: {verdict.summary}")
    elif not verdict.ok:
        print(f"orchestral eval cost budget is UNAUDITED, so it cannot pass: {verdict.summary}")
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
