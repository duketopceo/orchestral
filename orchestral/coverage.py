"""Experiment coverage ledger — matrix cells vs stored runs.

Answers "which cells are done, which are pending, which findings are
published" without touching the run pipeline: joins the matrix spec to
the index, splits runs by arm (``config.jev_assist``), and derives a
per-cell state — ``pending | partial | done | aborted``. ``aborted`` is
the only persisted state (a ``cell-state`` annotation written by the
driver), so a killed process reads differently from a stopped cell.

Publication marks are ``post`` annotations keyed on the cell key
(``task:orchestrator:worker``) — a published claim is about the pairing,
not the matrix it ran under.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestral.experiment import (
    Matrix,
    arm_stats,
    cell_runs,
    cell_state,
    dual_pass_rates,
    rep_target,
    status_counts,
)
from orchestral.stats import diff_ci, diff_verdict
from orchestral.storage import RunStore


@dataclass
class CoverageRow:
    cell_key: str
    task_id: str
    orchestrator: str
    worker: str
    state: str
    target: int
    baseline_passes: int = 0
    baseline_n: int = 0
    jev_passes: int = 0
    jev_n: int = 0
    non_evidence: int = 0
    baseline_status: dict[str, int] = field(default_factory=dict)
    jev_status: dict[str, int] = field(default_factory=dict)
    baseline_rates: dict[str, float | None] = field(default_factory=dict)
    jev_rates: dict[str, float | None] = field(default_factory=dict)
    diff: list[float] | None = None
    verdict: str = "pending"
    cost: float = 0.0
    posted: bool = False
    posted_note: str = ""
    note: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell": self.cell_key,
            "task": self.task_id,
            "orchestrator": self.orchestrator,
            "worker": self.worker,
            "state": self.state,
            "target": self.target,
            "baseline": {
                "passes": self.baseline_passes,
                "n": self.baseline_n,
                "status": self.baseline_status,
                **self.baseline_rates,
            },
            "jev": {
                "passes": self.jev_passes,
                "n": self.jev_n,
                "status": self.jev_status,
                **self.jev_rates,
            },
            "non_evidence": self.non_evidence,
            "diff_ci": self.diff,
            "verdict": self.verdict,
            "cost": round(self.cost, 6),
            "posted": self.posted,
            "posted_note": self.posted_note,
            "note": self.note,
            "warnings": self.warnings,
        }


def coverage_rows(
    store: RunStore,
    matrix: Matrix,
    *,
    budget: float = 0.0,
    diff_eps: float = 0.15,
) -> list[CoverageRow]:
    """One row per matrix cell — the done/pending/published ledger."""
    cells = matrix.cells
    cell_budget = budget / len(cells) if budget > 0 and cells else 0.0

    posts = {
        a["target"]: a["note"]
        for a in store.annotations()
        if a["kind"] == "post" and a["flag"] == "posted"
    }
    aborts = {
        a["target"]: a["note"]
        for a in store.annotations()
        if a["kind"] == "cell-state" and a["flag"] == "aborted"
    }

    rows: list[CoverageRow] = []
    for cell in cells:
        target, est = rep_target(store, cell, cell_budget)
        state = cell_state(store, matrix.name, cell, target, diff_eps)
        arms = cell_runs(store, matrix.name, cell)
        a_pass, a_n, a_non = arm_stats(arms["baseline"])
        b_pass, b_n, b_non = arm_stats(arms["jev"])
        ci = diff_ci(a_pass, a_n, b_pass, b_n)
        row = CoverageRow(
            cell_key=cell.key,
            task_id=cell.task_id,
            orchestrator=cell.orchestrator,
            worker=cell.worker,
            state=state,
            target=target,
            baseline_passes=a_pass,
            baseline_n=a_n,
            jev_passes=b_pass,
            jev_n=b_n,
            non_evidence=a_non + b_non,
            baseline_status=status_counts(arms["baseline"]),
            jev_status=status_counts(arms["jev"]),
            baseline_rates=dual_pass_rates(arms["baseline"]),
            jev_rates=dual_pass_rates(arms["jev"]),
            diff=[round(ci[0], 3), round(ci[1], 3)] if ci else None,
            verdict=diff_verdict(a_pass, a_n, b_pass, b_n, diff_eps),
            cost=sum(
                r.total_cost_usd for r in arms["baseline"] + arms["jev"]
            ),
            posted=cell.key in posts,
            posted_note=posts.get(cell.key, ""),
            note=aborts.get(f"{matrix.name}:{cell.key}", ""),
        )
        if est is None:
            row.warnings.append("no billing history — target is provisional")
        rows.append(row)
    return rows


def coverage_summary(rows: list[CoverageRow]) -> dict[str, Any]:
    """Roll-up counts for the ledger header."""
    states: dict[str, int] = {}
    for r in rows:
        states[r.state] = states.get(r.state, 0) + 1
    return {
        "cells": len(rows),
        "states": states,
        "posted": sum(1 for r in rows if r.posted),
        "spend": round(sum(r.cost for r in rows), 4),
        "verdicts": {
            v: sum(1 for r in rows if r.verdict == v)
            for v in ("lift", "harm", "resolved", "inconclusive", "insufficient", "pending")
        },
        "pass_rates": _mean_dual_rates(rows),
    }


def _mean_dual_rates(rows: list[CoverageRow]) -> dict[str, float | None]:
    """Cell-mean pass rates under both failure-accounting conventions —
    ``failed_excluded`` for the honest finished-only read,
    ``failed_as_zero`` for the no-free-crashes read."""
    out: dict[str, float | None] = {}
    for conv in ("failed_excluded", "failed_as_zero"):
        vals: list[float] = [
            v
            for r in rows
            for v in (
                r.baseline_rates.get(conv),
                r.jev_rates.get(conv),
            )
            if v is not None
        ]
        out[conv] = round(sum(vals) / len(vals), 4) if vals else None
    return out
