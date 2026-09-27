"""Export run data for external analysis: CSV tables and Markdown audits.

CSV is for leaderboard/pandas-style analysis; Markdown is a human-readable
per-run audit (manifest + evaluation evidence + failure context). JSONL
export is just the events file — consumers copy it directly.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from orchestral.stats import PairingAggregate
from orchestral.storage import RunMeta

_RUN_FIELDS = (
    "run_id", "started_at", "finished_at", "status", "task_id",
    "orchestrator", "worker", "score", "judge_score", "judge_passed",
    "passes", "failure_reason",
    "total_cost_usd", "total_input_tokens", "total_output_tokens",
    "latency_ms", "run_group", "replicate", "run_dir",
)

_PAIRING_FIELDS = (
    "orchestrator", "worker", "runs", "finished", "passed", "tasks_covered",
    "pass_rate", "score_median", "score_mean", "judge_score_median",
    "cost_median", "cost_total",
    "duration_median_ms", "failure_rate", "cost_per_pass", "low_sample",
)


def runs_csv(runs: list[RunMeta]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(_RUN_FIELDS))
    w.writeheader()
    for r in runs:
        w.writerow({f: getattr(r, f, None) for f in _RUN_FIELDS})
    return buf.getvalue()


def leaderboard_csv(rows: list[PairingAggregate]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(_PAIRING_FIELDS))
    w.writeheader()
    for p in rows:
        w.writerow({f: p.to_dict().get(f) for f in _PAIRING_FIELDS})
    return buf.getvalue()


def run_audit_markdown(run_dir: Path | str) -> str:
    """Render a human audit of one run dir: manifest, report, metrics, calls."""
    run_dir = Path(run_dir)
    parts = [f"# Run audit: {run_dir.name}", ""]

    manifest = _read_json(run_dir / "manifest.json")
    if manifest:
        parts += [
            "## Manifest", "",
            f"- task: `{manifest.get('task_id')}` (hash `{str(manifest.get('task_hash'))[:12]}`)",
            f"- orchestrator: `{manifest.get('orchestrator_model')}` via {manifest.get('orchestrator_provider')}",
            f"- worker: `{manifest.get('worker_model')}` via {manifest.get('worker_provider')}",
            f"- judge: `{manifest.get('judge_model') or 'none'}`",
            f"- status: {manifest.get('status')} · started {manifest.get('started_at')} · finished {manifest.get('finished_at')}",
            f"- git: `{manifest.get('git_commit')}` · harness {manifest.get('harness_version')} · python {manifest.get('python_version')}",
            f"- seed: {manifest.get('seed')} · group: {manifest.get('run_group')} · replicate: {manifest.get('replicate')}",
            "",
        ]

    report = _read_json(run_dir / "report.json")
    if report:
        parts += ["## Evaluation", ""]
        checks = report.get("checks") or {}
        for name, ok in checks.items():
            parts.append(f"- {name}: {'pass' if ok else 'FAIL'}")
        if report.get("score") is not None:
            parts.append(f"- score: {report['score']}")
        judge = report.get("judge") or {}
        if judge.get("reasoning"):
            parts.append(f"- judge: {judge['reasoning']}")
        for err in report.get("errors") or []:
            parts.append(f"- error: {err}")
        parts.append("")

    metrics = _read_json(run_dir / "metrics.json")
    if metrics:
        parts += ["## Metrics", "", "```json", json.dumps(metrics, indent=2), "```", ""]

    cost = _read_json(run_dir / "cost.json")
    if cost:
        parts += ["## Cost breakdown", "", "```json", json.dumps(cost, indent=2), "```", ""]

    artifacts = sorted(
        p.name for p in run_dir.iterdir()
        if p.is_file() and (p.name.startswith("artifact.") or p.name.startswith("worker-"))
    )
    if artifacts:
        parts += ["## Artifacts", "", *(f"- `{a}`" for a in artifacts), ""]

    return "\n".join(parts)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None
