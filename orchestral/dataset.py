"""RL-ready dataset export — one record per LLM call, joined to run outcome.

The `calls` index holds every llm_call/worker_error event a run logged:
prompt messages, raw completion, token usage, cost, latency, finish_reason.
This module joins those steps to the run's outcome block (mechanical score,
every judge's verdict, delegated flag, failure category) and writes JSONL —
the shape RLHF/GRPO-style trainers consume.

Reward semantics: `reward` is the field a trainer should read. `--reward`
selects its source; `rewards` always carries every available signal so the
choice can be re-shaped downstream without re-exporting.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orchestral.storage import RunMeta, RunStore

SCHEMA = "orchestral.rl_step/v1"
SCHEMA_EPISODE = "orchestral.rl_episode/v1"


def _report(run_dir: str) -> dict[str, Any]:
    p = Path(run_dir) / "report.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _outcome(meta: RunMeta, report: dict[str, Any]) -> dict[str, Any]:
    judge = report.get("judge") or {}
    judges = report.get("judges") or {}
    judge_scores = {
        slug: v.get("score")
        for slug, v in judges.items()
        if isinstance(v, dict) and v.get("score") is not None
    }
    if judge.get("score") is not None and judge.get("model"):
        judge_scores.setdefault(judge["model"], judge["score"])
    return {
        "status": meta.status,
        "score": meta.score,
        "passes": meta.passes,
        # index column only exists for post-v3 runs — report.json is the
        # retroactive source for older entries
        "delegated": meta.delegated if meta.delegated is not None else report.get("delegated"),
        "subtasks": report.get("subtasks"),
        "failure_reason": meta.failure_reason,
        "duration_ms": meta.latency_ms,
        "total_cost_usd": meta.total_cost_usd,
        # every judge verdict we hold — jev primary, others secondary;
        # agreement between them is itself a reward-quality signal
        "judge": {
            "model": judge.get("model"),
            "engine": judge.get("engine"),
            "score": judge.get("score"),
            "passed": judge.get("passed"),
            "inconclusive": judge.get("inconclusive"),
            "noul": judge.get("noul"),
            "confidence": judge.get("confidence"),
        } if judge else None,
        "judge_scores": judge_scores,
    }


def _reward(outcome: dict[str, Any], mode: str) -> tuple[float | None, str]:
    judge = outcome.get("judge") or {}
    judge_score = judge.get("score")
    mech = outcome.get("score")
    if mode == "judge":
        return judge_score, "judge"
    if mode == "mechanical":
        return mech, "mechanical"
    # best: prefer judge; mechanical when the run was never judged
    if judge_score is not None:
        return judge_score, "judge"
    if mech is not None:
        return mech, "mechanical"
    return None, "none"


def iter_runs(
    store: RunStore,
    *,
    group: str | None = None,
    task: str | None = None,
    orchestrator: str | None = None,
    worker: str | None = None,
    include_dry: bool = False,
) -> Iterable[tuple[RunMeta, dict[str, Any], dict[str, Any]]]:
    """Yield (meta, outcome, report) for matching runs."""
    for meta in store.list_runs(
        run_group=group, task_id=task, orchestrator=orchestrator,
        worker=worker, limit=None,
    ):
        if meta.dry_run and not include_dry:
            continue  # synthetic artifacts must never enter training data
        report = _report(meta.run_dir)
        yield meta, _outcome(meta, report), report


def iter_steps(
    store: RunStore,
    *,
    reward: str = "judge",
    include_payloads: bool = True,
    **filters: Any,
) -> Iterable[dict[str, Any]]:
    """One record per indexed LLM call, carrying the run's outcome+reward."""
    for meta, outcome, _report in iter_runs(store, **filters):
        outcome = dict(outcome)
        outcome["reward"], outcome["reward_source"] = _reward(outcome, reward)
        base: dict[str, Any] = {
            "schema": SCHEMA,
            "run_id": meta.run_id,
            "group": meta.run_group,
            "replicate": meta.replicate,
            "task_id": meta.task_id,
            "orchestrator": meta.orchestrator,
            "worker": meta.worker,
            "outcome": outcome,
        }
        for call in store.calls_for_run(meta.run_id):
            rec: dict[str, Any] = dict(base)
            rec["step"] = {
                "seq": call.get("sequence"),
                "phase": call.get("phase"),
                "step": call.get("step"),
                "role": call.get("role"),
                "worker_id": call.get("worker_id"),
                "model": call.get("model"),
                "input_tokens": call.get("input_tokens"),
                "output_tokens": call.get("output_tokens"),
                "cost_usd": call.get("cost_usd"),
                "api_cost_usd": call.get("api_cost_usd"),
                "latency_ms": call.get("latency_ms"),
                "attempt": call.get("attempt"),
                "finish_reason": call.get("finish_reason"),
                "error": call.get("error"),
                "error_category": call.get("error_category"),
            }
            if include_payloads:
                try:
                    rec["step"]["prompt"] = (json.loads(call["input_json"]) or {}).get("messages")
                except (json.JSONDecodeError, TypeError):
                    rec["step"]["prompt"] = None
                try:
                    rec["step"]["completion"] = json.loads(call["output_json"])
                except (json.JSONDecodeError, TypeError):
                    rec["step"]["completion"] = None
            yield rec


def iter_episodes(
    store: RunStore,
    *,
    reward: str = "judge",
    **filters: Any,
) -> Iterable[dict[str, Any]]:
    """One record per run — the bandit/episode view of the same data."""
    for meta, outcome, _report in iter_runs(store, **filters):
        outcome = dict(outcome)
        outcome["reward"], outcome["reward_source"] = _reward(outcome, reward)
        calls = store.calls_for_run(meta.run_id)
        yield {
            "schema": SCHEMA_EPISODE,
            "run_id": meta.run_id,
            "group": meta.run_group,
            "replicate": meta.replicate,
            "task_id": meta.task_id,
            "orchestrator": meta.orchestrator,
            "worker": meta.worker,
            "started_at": meta.started_at,
            "calls": len(calls),
            "total_cost_usd": meta.total_cost_usd,
            "outcome": outcome,
            # compact per-step summary — full payloads live in the steps file
            "steps": [
                {
                    "phase": c.get("phase"), "role": c.get("role"),
                    "model": c.get("model"), "worker_id": c.get("worker_id"),
                    "output_tokens": c.get("output_tokens"),
                    "cost_usd": c.get("cost_usd"), "finish_reason": c.get("finish_reason"),
                    "error_category": c.get("error_category"),
                }
                for c in calls
            ],
        }


def write_dataset(
    store: RunStore,
    out_path: Path,
    *,
    reward: str = "judge",
    include_payloads: bool = True,
    episodes_path: Path | None = None,
    **filters: Any,
) -> dict[str, int]:
    counts = {"steps": 0, "episodes": 0}
    with out_path.open("w") as fh:
        for rec in iter_steps(store, reward=reward, include_payloads=include_payloads, **filters):
            fh.write(json.dumps(rec, default=str) + "\n")
            counts["steps"] += 1
    if episodes_path is not None:
        with episodes_path.open("w") as fh:
            for rec in iter_episodes(store, reward=reward, **filters):
                fh.write(json.dumps(rec, default=str) + "\n")
                counts["episodes"] += 1
    return counts
