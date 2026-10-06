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


# --- Inspect AI eval-log interop (schema version 2) -------------------------
#
# `harness.py export --format inspect` writes Inspect AI-compatible eval-log
# JSON so runs can be browsed in `inspect view`. One log per
# (task, orchestrator, worker, arm) group keeps `results` aggregates arm-pure;
# a run's messages are a synthesized transcript (plan -> delegate -> assemble),
# flagged via sample.metadata.transcript_source.

_INSPECT_LOG_VERSION = 2


def inspect_group_key(meta: RunMeta) -> tuple[str, str, str, str]:
    """Grouping for bulk export: one eval log per distinct eval config."""
    return (meta.task_id, meta.orchestrator, meta.worker, _arm_of(meta))


def inspect_log_name(key: tuple[str, str, str, str]) -> str:
    slug = "__".join(part.replace("/", "-") for part in key)
    return f"{slug}.json"


def inspect_eval_log(metas: list[RunMeta], *, tasks_dir: Path | str = "tasks") -> dict[str, Any]:
    """Build an Inspect AI eval-log dict from runs sharing one group key."""
    first = metas[0]
    arm = _arm_of(first)
    spec = _find_spec(first.task_id, tasks_dir)
    samples = [_inspect_sample(m, spec) for m in metas]
    group_usage: dict[str, Any] = {}
    for s in samples:
        for model, u in s["model_usage"].items():
            entry = group_usage.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
            entry["input_tokens"] += u["input_tokens"]
            entry["output_tokens"] += u["output_tokens"]
            entry["total_tokens"] += u["total_tokens"]
    passed = sum(1 for s in samples if s["scores"]["orchestral_checks"]["value"] == "C")
    scored = [s["scores"]["judge"]["value"] for s in samples if s["scores"].get("judge")]
    metrics: dict[str, Any] = {
        "accuracy": {"name": "accuracy", "value": passed / len(samples) if samples else 0.0},
    }
    if scored:
        metrics["judge_mean"] = {"name": "judge_mean", "value": sum(scored) / len(scored)}
    return {
        "version": _INSPECT_LOG_VERSION,
        "status": "success",
        "eval": {
            "created": max((m.finished_at or m.started_at or "") for m in metas),
            "task": first.task_id,
            "task_id": f"orchestral/{first.task_id}",
            "task_version": 0,
            "model": f"orchestral:{first.orchestrator}+{first.worker}",
            "dataset": {
                "name": "orchestral",
                "location": str(tasks_dir),
                "samples": len(samples),
                "sample_ids": [s["id"] for s in samples],
            },
            "tags": [f"arm:{arm}"] if arm else [],
            "config": {},
            "model_args": {},
        },
        "plan": {
            "name": "orchestral",
            "steps": [
                {"solver": "plan", "params": {"model": first.orchestrator}},
                {"solver": "delegate", "params": {"model": first.worker}},
                {"solver": "assemble", "params": {"model": first.orchestrator}},
            ],
            "config": {},
        },
        "results": {
            "total_samples": len(samples),
            "completed_samples": len(samples),
            "scores": [{
                "name": "orchestral_checks",
                "scorer": "orchestral_checks",
                "params": {},
                "metrics": metrics,
            }],
        },
        "stats": {
            "started_at": min((m.started_at or "") for m in metas),
            "completed_at": max((m.finished_at or m.started_at or "") for m in metas),
            "model_usage": group_usage,
        },
        "samples": samples,
    }


def _arm_of(meta: RunMeta) -> str:
    group = getattr(meta, "run_group", None) or ""
    tail = group.rsplit(":", 1)[-1]
    return tail if tail in ("baseline", "jev") else ""


def _find_spec(task_id: str, tasks_dir: Path | str) -> Any | None:
    try:
        from orchestral.audit import load_specs
        for _path, spec in load_specs(tasks_dir):
            if getattr(spec, "id", None) == task_id:
                return spec
    except Exception:
        pass
    return None


def _inspect_sample(meta: RunMeta, spec: Any | None) -> dict[str, Any]:
    run_dir = Path(meta.run_dir)
    report = _read_json(run_dir / "report.json") or {}
    manifest = _read_json(run_dir / "manifest.json") or {}
    checks = report.get("checks") or {}
    failed = [name for name, ok in checks.items() if not ok]
    judge = report.get("judge") or {}
    scores: dict[str, Any] = {
        "orchestral_checks": {
            "value": "C" if getattr(meta, "passes", None) else "I",
            "explanation": ("all checks passed" if not failed
                            else "failed: " + ", ".join(failed)),
        },
    }
    if judge.get("score") is not None:
        scores["judge"] = {
            "value": judge["score"],
            "explanation": judge.get("reasoning") or "",
            "metadata": {"model": judge.get("model"), "engine": judge.get("engine")},
        }
    metadata: dict[str, Any] = {
        "run_id": meta.run_id,
        "transcript_source": "synthesized from plan/delegate/assemble artifacts",
    }
    for key, val in (
        ("arm", _arm_of(meta)),
        ("run_group", getattr(meta, "run_group", None)),
        ("cost_usd", getattr(meta, "total_cost_usd", None)),
        ("task_hash", manifest.get("task_hash")),
        ("canary", getattr(spec, "metadata", {}).get("canary") if spec else None),
    ):
        if val is not None:
            metadata[key] = val
    sample: dict[str, Any] = {
        "id": meta.run_id,
        "epoch": int(getattr(meta, "replicate", None) or 1),
        "input": getattr(spec, "prompt", None) or meta.task_id,
        "target": list(getattr(spec, "validation", None) or []),
        "messages": _inspect_messages(run_dir, spec),
        "output": _inspect_output(run_dir, meta),
        "scores": scores,
        "metadata": metadata,
        "model_usage": _inspect_usage(run_dir),
        "started_at": meta.started_at,
        "completed_at": meta.finished_at,
    }
    if meta.status != "finished":
        sample["error"] = {
            "message": meta.failure_reason or meta.status,
            "traceback": "",
            "traceback_ansi": "",
        }
    return sample


def _inspect_messages(run_dir: Path, spec: Any | None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if spec is not None and getattr(spec, "prompt", None):
        messages.append({"role": "user", "content": spec.prompt})
    plan = _read_json(run_dir / "plan.json") or {}
    if plan.get("plan"):
        messages.append({"role": "assistant", "content": f"[orchestrator plan] {plan['plan']}"})
    outputs: dict[str, str] = {}
    for worker_path in sorted(run_dir.glob("worker-*.json")):
        worker = _read_json(worker_path) or {}
        if worker.get("output"):
            outputs[str(worker.get("subtask_id") or worker.get("id"))] = _as_text(worker["output"])
    for i, sub in enumerate(plan.get("subtasks") or [], 1):
        if isinstance(sub, dict):
            sid = str(sub.get("id"))
            desc = sub.get("description") or sub.get("title") or sid
        else:
            sid, desc = str(i), str(sub)
        messages.append({"role": "user", "content": f"[delegate {sid}] {desc}"})
        if sid in outputs:
            messages.append({"role": "assistant", "content": outputs[sid]})
    return messages


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return value["content"]
    return json.dumps(value)


def _inspect_output(run_dir: Path, meta: RunMeta) -> dict[str, Any]:
    content = ""
    for artifact in sorted(run_dir.glob("artifact.*")):
        try:
            content = artifact.read_text(encoding="utf-8", errors="replace")
            break
        except OSError:
            continue
    return {
        "model": meta.worker,
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "stop_reason": "stop",
        }],
    }


def _inspect_usage(run_dir: Path) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    debug = run_dir / "debug.jsonl"
    if not debug.exists():
        return usage
    for line in debug.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        fields = event.get("fields") or {}
        model = fields.get("model")
        if event.get("message") != "chat" or not model:
            continue
        entry = usage.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        in_tok = int(fields.get("prompt_tokens") or 0)
        out_tok = int(fields.get("completion_tokens") or 0)
        entry["input_tokens"] += in_tok
        entry["output_tokens"] += out_tok
        entry["total_tokens"] += in_tok + out_tok
    return usage
