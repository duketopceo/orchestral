"""Judge calibration — agreement between model-judge and human labels.

A labels file is YAML:

    labels:
      - run_id: a1b2c3d4      # unique prefix of the run id
        score: 0.8            # human score in [0, 1]  (optional)
        passed: true          # human verdict          (optional)

`collect_pairs` joins labels to the judge's stored verdict — only
`report.json`'s `judge` block, never the mechanical run verdict (a run
that was never judged counts toward coverage but enters no agreement
metric, because falling back to `run.json` would make any run "agree"
with itself). `agreement_metrics` reports score agreement (MAE,
Pearson, Spearman) and verdict agreement (accuracy, Cohen's kappa,
confusion counts), sliced overall, per judge model, and per task.

Only pairs where both sides carry a value for a field enter that
field's metrics — a run judged-but-unlabeled (or labeled-but-unjudged,
or judged-inconclusively) still counts in `coverage` so gaps are
visible.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from orchestral.config import load_yaml
from orchestral.storage import RunStore

# a judge only earns the "calibrated" label when it has both enough
# verdict pairs and real agreement — κ ≥ 0.7 is the "substantial
# agreement" convention from inter-rater literature
MIN_CALIBRATION_PAIRS = 30
MIN_CALIBRATION_KAPPA = 0.7


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


_VERDICT_TRUE = frozenset({"true", "yes", "y", "1", "pass", "passed"})
_VERDICT_FALSE = frozenset({"false", "no", "n", "0", "fail", "failed"})


def _coerce_verdict(v: Any) -> bool | None:
    """Labels may carry YAML bools, strings, or numbers — coerce through an
    explicit table so a literal ``"false"`` never reads truthy."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _VERDICT_TRUE:
            return True
        if s in _VERDICT_FALSE:
            return False
    return None


def _coerce_score(v: Any) -> float | None:
    """Numbers and numeric strings coerce; anything else is no score."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _resolve_run(metas: list[Any], prefix: str) -> Any | None:
    """Unique-prefix resolution against the index — an ambiguous prefix
    resolves to nothing rather than silently joining the wrong run."""
    matches = [m for m in metas if m.run_id.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def collect_pairs(store: RunStore, labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Join labels to stored judge verdicts; returns pairs + coverage.

    Judge-only: a label matching a run with no ``report.judge`` block is
    recorded in ``coverage["unjudged"]`` and produces no pair. Inconclusive
    judge results keep their pair slot — ``judge_score``/``judge_passed``
    may be ``None`` — so score data still contributes while verdict
    metrics stay clean. Labels dedupe on the resolved run id (first wins);
    pairs record the resolved id so coverage is auditable.
    """
    metas = store.list_runs()
    pairs: list[dict[str, Any]] = []
    unmatched: list[str] = []
    unjudged: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for label in labels:
        meta = _resolve_run(metas, str(label["run_id"]))
        if meta is None:
            unmatched.append(str(label["run_id"]))
            continue
        if meta.run_id in seen:
            duplicates.append(meta.run_id)
            continue
        seen.add(meta.run_id)
        run_dir = Path(meta.run_dir)
        report: dict[str, Any] = {}
        report_path = run_dir / "report.json"
        if report_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                report = json.loads(report_path.read_text())
        judge = report.get("judge")
        if not isinstance(judge, dict) or not judge:
            unjudged.append(str(label["run_id"]))
            continue
        run_json: dict[str, Any] = {}
        run_path = run_dir / "run.json"
        if run_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                run_json = json.loads(run_path.read_text())
        pairs.append(
            {
                "run_id": meta.run_id,
                "task_id": run_json.get("task_id") or report.get("task", {}).get("id"),
                "human_score": _coerce_score(label.get("score")),
                "judge_score": _coerce_score(judge.get("score")),
                "human_passed": _coerce_verdict(label.get("passed")),
                "judge_passed": _coerce_verdict(judge.get("passed")),
                "judge_model": judge.get("model"),
            }
        )
    coverage = {
        "labeled": len(labels),
        "matched": len(pairs) + len(unjudged),
        "unmatched": unmatched,
        "unjudged": unjudged,
        "duplicates": duplicates,
        "score_pairs": sum(1 for p in pairs if p.get("human_score") is not None and p.get("judge_score") is not None),
        "verdict_pairs": sum(1 for p in pairs if p.get("human_passed") is not None and p.get("judge_passed") is not None),
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


def _metrics_block(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Score + verdict agreement over the pairs that have both sides."""
    score_pairs = [
        p for p in pairs
        if p.get("human_score") is not None and p.get("judge_score") is not None
    ]
    verdict_pairs = [
        p for p in pairs
        if p.get("human_passed") is not None and p.get("judge_passed") is not None
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


def agreement_metrics(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Overall agreement plus per-judge and per-task slices."""
    metrics = _metrics_block(pairs)
    for axis, key in (("by_judge", "judge_model"), ("by_task", "task_id")):
        slices: dict[str, Any] = {}
        for value in {str(p.get(key)) for p in pairs if p.get(key)}:
            slices[value] = _metrics_block(
                [p for p in pairs if p.get(key) == value]
            )
        metrics[axis] = slices
    return metrics


def emit_label_skeleton(
    store: RunStore,
    run_group: str | None = None,
) -> str:
    """YAML label skeleton for judged finished runs — one blank entry each.

    Only runs with a ``report.judge`` block are emitted: labeling an
    unjudged run can never produce a calibration pair, so it would be
    wasted human effort. Humans fill in ``score``/``passed`` while
    looking at the artifact; the run_id/task_id/artifact pointers come
    prefilled so labeling is review, not transcription.
    """
    metas = [
        m for m in store.list_runs(run_group=run_group)
        if m.status == "finished"
    ]
    entries = []
    for m in metas:
        run_dir = Path(m.run_dir)
        report: dict[str, Any] = {}
        report_path = run_dir / "report.json"
        if report_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                report = json.loads(report_path.read_text())
        if not isinstance(report.get("judge"), dict) or not report["judge"]:
            continue
        artifact = next(
            (p for p in sorted(run_dir.glob("artifact.*")) if p.is_file()),
            None,
        )
        entries.append(
            {
                "run_id": m.run_id,
                "task_id": m.task_id,
                "artifact": str(artifact) if artifact else None,
                "judge_model": report["judge"].get("model"),
                "score": None,
                "passed": None,
            }
        )
    return yaml.safe_dump({"labels": entries}, sort_keys=False)


def persist_calibration(
    reports_dir: Path | str,
    *,
    labels_path: Path | str | None,
    pairs: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> Path:
    """Write ``reports/calibration-<ts>.json`` and return its path."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC)
    labels_sha256 = None
    if labels_path is not None:
        labels_sha256 = hashlib.sha256(Path(labels_path).read_bytes()).hexdigest()
    payload = {
        "created_at": created_at.isoformat(timespec="seconds"),
        "labels_sha256": labels_sha256,
        "judge_models": sorted({str(p.get("judge_model")) for p in pairs if p.get("judge_model")}),
        "pairs": len(pairs),
        "score_pairs": sum(
            1 for p in pairs
            if p.get("human_score") is not None and p.get("judge_score") is not None
        ),
        "verdict_pairs": sum(
            1 for p in pairs
            if p.get("human_passed") is not None and p.get("judge_passed") is not None
        ),
        "metrics": metrics,
    }
    path = reports_dir / f"calibration-{created_at.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def calibration_status(
    reports_dir: Path | str,
    judge_slug: str | None = None,
) -> dict[str, Any]:
    """Latest persisted calibration state for a judge model.

    Calibrated means: a persisted report where this judge's verdict
    slice has at least ``MIN_CALIBRATION_PAIRS`` pairs and kappa at
    least ``MIN_CALIBRATION_KAPPA``. ``judge_slug=None`` evaluates the
    report's overall verdict block.
    """
    reports_dir = Path(reports_dir)
    paths = sorted(reports_dir.glob("calibration-*.json"))
    status: dict[str, Any] = {
        "calibrated": False,
        "kappa": None,
        "verdict_pairs": 0,
        "source": None,
        "created_at": None,
    }
    if not paths:
        return status
    # newest report that actually contains this judge's block — a report
    # covering a different judge must not reset this one's provenance
    for latest in reversed(paths):
        found = False
        with contextlib.suppress(json.JSONDecodeError, OSError):
            data = json.loads(latest.read_text())
            metrics = data.get("metrics") or {}
            block = metrics.get("verdict")
            if judge_slug is not None:
                block = (metrics.get("by_judge") or {}).get(judge_slug, {}).get("verdict")
            if block:
                status["kappa"] = block.get("kappa")
                status["verdict_pairs"] = block.get("n", 0)
                found = True
            elif judge_slug is not None:
                continue  # this report doesn't cover the requested judge
        if found or judge_slug is None:
            status["source"] = str(latest)
            status["created_at"] = data.get("created_at")
            status["calibrated"] = (
                status["verdict_pairs"] >= MIN_CALIBRATION_PAIRS
                and status["kappa"] is not None
                and status["kappa"] >= MIN_CALIBRATION_KAPPA
            )
            return status
    return status
