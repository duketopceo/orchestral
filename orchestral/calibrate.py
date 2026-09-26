"""Judge calibration — agreement between model-judge and human labels.

A labels file is YAML:

    labels:
      - run_id: a1b2c3d4      # unique prefix of the run id
        score: 0.8            # human score in [0, 1]  (optional)
        passed: true          # human verdict          (optional)

`collect_pairs` joins labels to the judge's stored verdict (report.json →
`judge.score` / `judge.passed`, falling back to run-level score/passes);
`agreement_metrics` reports score agreement (MAE, Pearson, Spearman) and
verdict agreement (accuracy, Cohen's kappa, confusion counts).

Only runs with both a human and a judge value for a field enter that
field's metrics. Runs that are judged-but-unlabeled are not counted here —
only labeled runs are accounted for in `coverage` (label gaps on the
labeled side show up as `matched` vs the pair counts).
"""

from __future__ import annotations

import contextlib
import json
import math
from pathlib import Path
from typing import Any

from orchestral.config import load_yaml
from orchestral.storage import RunStore


def load_labels(path: Path | str) -> list[dict[str, Any]]:
    data = load_yaml(path) or {}
    labels = data.get("labels")
    if not isinstance(labels, list):
        raise ValueError(f"{path} has no `labels` list")
    out = []
    for i, item in enumerate(labels):
        if not isinstance(item, dict) or not item.get("run_id"):
            raise ValueError(f"labels[{i}] needs a run_id")
        out.append(item)
    return out


def _find_run_dir(store: RunStore, run_id: str) -> Path | None:
    matches = [r for r in store.list_runs() if r.run_id.startswith(run_id)]
    if len(matches) == 1:
        return Path(matches[0].run_dir)
    return None


def collect_pairs(store: RunStore, labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Join labels to stored judge verdicts; returns pairs + coverage."""
    pairs: list[dict[str, Any]] = []
    unmatched: list[str] = []
    corrupt = 0
    for label in labels:
        run_dir = _find_run_dir(store, str(label["run_id"]))
        if run_dir is None:
            unmatched.append(str(label["run_id"]))
            continue
        report: dict[str, Any] = {}
        report_path = run_dir / "report.json"
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text())
            except (json.JSONDecodeError, OSError):
                # a corrupt judge report must not silently contaminate the
                # agreement metrics — count it so the gap is visible
                corrupt += 1
        run_json: dict[str, Any] = {}
        run_path = run_dir / "run.json"
        if run_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                run_json = json.loads(run_path.read_text())
        judge = report.get("judge") or {}
        pairs.append(
            {
                "run_id": label["run_id"],
                "task_id": run_json.get("task_id"),
                "human_score": label.get("score"),
                "judge_score": judge.get("score") if judge.get("score") is not None else run_json.get("score"),
                "human_passed": label.get("passed"),
                "judge_passed": judge.get("passed") if judge.get("passed") is not None else run_json.get("passes"),
            }
        )
    coverage = {
        "labeled": len(labels),
        "matched": len(pairs),
        "unmatched": unmatched,
        "corrupt": corrupt,
        "score_pairs": sum(1 for p in pairs if p["human_score"] is not None and p["judge_score"] is not None),
        "verdict_pairs": sum(1 for p in pairs if p["human_passed"] is not None and p["judge_passed"] is not None),
    }
    return {"pairs": pairs, "coverage": coverage}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mean_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return pearson(_ranks(xs), _ranks(ys))


def cohens_kappa(truth: list[bool], pred: list[bool]) -> float | None:
    if not truth:
        return None
    n = len(truth)
    po = sum(t == p for t, p in zip(truth, pred, strict=True)) / n
    t_rate = sum(truth) / n
    p_rate = sum(pred) / n
    pe = t_rate * p_rate + (1 - t_rate) * (1 - p_rate)
    if pe == 1:
        return None
    return (po - pe) / (1 - pe)


def agreement_metrics(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Score + verdict agreement over the pairs that have both sides."""
    score_pairs = [
        p for p in pairs
        if p["human_score"] is not None and p["judge_score"] is not None
    ]
    verdict_pairs = [
        p for p in pairs
        if p["human_passed"] is not None and p["judge_passed"] is not None
    ]

    metrics: dict[str, Any] = {"score": None, "verdict": None}
    if score_pairs:
        human = [float(p["human_score"]) for p in score_pairs]
        judge = [float(p["judge_score"]) for p in score_pairs]
        metrics["score"] = {
            "n": len(score_pairs),
            "mae": _mean([abs(h - j) for h, j in zip(human, judge, strict=True)]),
            "pearson": pearson(human, judge),
            "spearman": spearman(human, judge),
            "human_mean": _mean(human),
            "judge_mean": _mean(judge),
        }
    if verdict_pairs:
        human_v = [bool(p["human_passed"]) for p in verdict_pairs]
        judge_v = [bool(p["judge_passed"]) for p in verdict_pairs]
        tp = sum(h and j for h, j in zip(human_v, judge_v, strict=True))
        tn = sum(not h and not j for h, j in zip(human_v, judge_v, strict=True))
        fp = sum(not h and j for h, j in zip(human_v, judge_v, strict=True))
        fn = sum(h and not j for h, j in zip(human_v, judge_v, strict=True))
        metrics["verdict"] = {
            "n": len(verdict_pairs),
            "accuracy": (tp + tn) / len(verdict_pairs),
            "kappa": cohens_kappa(human_v, judge_v),
            "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        }
    return metrics
