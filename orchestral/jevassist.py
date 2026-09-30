"""Decisions-engine assistance inside the run's decision loop (``--jev-assist``).

When enabled, a decisions-model judge (``~typesafe/jev-latest``) is consulted
at two decision surfaces per run — after planning and after delegation —
before the expensive downstream step commits. Answers are calibrated
probabilities (noul / 0-4 score), not prose: a gate is a probability, not a
vibes check.

Gates:

- ``plan_gate`` — audits the orchestrator's decomposition before delegation.
  An unsound plan (verdict noul < 0.5) gets ONE replan; the engine's verdict
  feeds the replan prompt as bounded context (the orchestrator's own decision
  loop — never worker-visible).
- ``output_gate`` — audits the worker outputs jointly before assembly.
  An inadequate set triggers ONE rework pass on the weakest subtask.

Assist is advisory by construction: a ``decide()`` error, a missing judge, or
a null verdict degrades to ``inconclusive`` and the run proceeds unmodified.
A gate never fails a run — it can only improve inputs or leave them alone.
"""

from __future__ import annotations

import json
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.logger import EventLogger
from orchestral.providers import Provider

_PLAN_QUESTIONS: dict[str, Any] = {
    "sound": {
        "type": "noul",
        "instructions": (
            "Is this decomposition a sound and complete plan for the task? "
            "Judge the plan as written — will following it produce a correct, "
            "complete artifact?"
        ),
        "true": "Following this plan as written will produce a correct, complete artifact.",
        "false": "The plan is wrong, off-task, or missing required pieces.",
    },
    "coverage": {
        "type": "score",
        "instructions": "Rate how completely the plan covers the task requirements.",
        "criteria": ["severely incomplete", "partial", "adequate", "good", "excellent"],
    },
}


def _output_questions(n_subtasks: int) -> dict[str, Any]:
    return {
        "adequate": {
            "type": "noul",
            "instructions": (
                "Taken together, do these worker outputs contain a correct, "
                "complete answer to the task? Judge content against the task, "
                "not surface polish."
            ),
            "true": "The outputs jointly satisfy the task.",
            "false": "The outputs are wrong, incomplete, or off-task.",
        },
        "weakest": {
            "type": "choice",
            "instructions": (
                "Which subtask output is weakest relative to its brief — the "
                "one a rework pass should target first?"
            ),
            "options": [str(i) for i in range(n_subtasks)],
        },
    }


def _cost(phase: str, slug: str, usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": phase,
        "model": slug,
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cost_usd": usage.get("cost") or 0.0,
        "pricing_source": "api",
        "usage": usage,
    }


def _gate(
    *,
    logger: EventLogger,
    step: int,
    event_type: str,
    phase: str,
    state: dict[str, Any],
    questions: dict[str, Any],
    judge: ModelConfig,
    client: Provider,
    input_note: dict[str, Any],
) -> dict[str, Any]:
    """One advisory decision call. Returns the gate record (always truthy);
    raises nothing — a broken advisor degrades to ``inconclusive``."""
    try:
        data = client.decide(model=judge.slug, state=state, questions=questions)
    except Exception as exc:  # advisory must never break the run
        record = {
            "inconclusive": True,
            "error": f"{type(exc).__name__}: {exc}",
            "model": judge.slug,
            "engine": "decisions",
        }
        logger.log(
            phase=phase, step=step, event_type=f"{event_type}_error",
            model=judge.slug, role="judge", input_data=input_note,
            output_data=record,
            reasoning="Decisions call failed; run proceeds unmodified.",
        )
        return {"record": record, "costs": []}
    answers = data.get("answers") or {}
    ok_record: dict[str, Any] = {
        "answers": answers,
        "inconclusive": not answers,
        "model": judge.slug,
        "engine": "decisions",
    }
    usage = data.get("usage") or {}
    logger.log_llm_call(
        phase=phase, step=step, model=judge.slug, role="judge",
        messages=[{"role": "user", "content": json.dumps({"state": state, "questions": questions})}],
        completion={"content": json.dumps(answers), "usage": usage, "id": data.get("id")},
        reasoning=f"Jev {event_type}: {json.dumps(answers)[:300]}",
        input_tokens=usage.get("input_tokens") or 0,
        output_tokens=usage.get("output_tokens") or 0,
        cost_usd=usage.get("cost") or 0.0,
        latency_ms=data.get("latency_ms") or 0.0,
        pricing_source="api",
    )
    return {"record": ok_record, "costs": [_cost(phase, judge.slug, usage)]}


def _noul(answers: dict[str, Any], key: str) -> float | None:
    v = (answers.get(key) or {}).get("noul")
    return float(v) if isinstance(v, int | float) else None


def _score(answers: dict[str, Any], key: str) -> float | None:
    v = (answers.get(key) or {}).get("score")
    return float(v) if isinstance(v, int | float) else None


def plan_gate(
    *,
    logger: EventLogger,
    step: int,
    task: TaskSpec,
    plan: dict[str, Any],
    judge: ModelConfig,
    client: Provider,
) -> dict[str, Any]:
    """Audit the orchestrator's decomposition before delegation.

    Returns ``{"record", "costs", "sound", "coverage", "replan_note"}`` —
    ``sound`` False means the engine recommends a replan, and
    ``replan_note`` is the bounded context to inject into that replan."""
    state = {
        "task": task.prompt[:4000],
        "plan": {k: v for k, v in plan.items()
                 if k not in ("task_id", "orchestrator", "planner")},
    }
    out = _gate(
        logger=logger, step=step, event_type="jev_plan_gate", phase="plan",
        state=state, questions=_PLAN_QUESTIONS, judge=judge, client=client,
        input_note={"task": task.id, "subtasks": len(plan.get("subtasks") or [])},
    )
    record = out["record"]
    answers = record.get("answers") or {}
    noul = _noul(answers, "sound")
    coverage = _score(answers, "coverage")
    sound = (noul >= 0.5) if noul is not None else None
    note = None
    if sound is False:
        note = (
            "A calibrated plan critic rejected the previous decomposition "
            f"(soundness noul={noul:.2f}, coverage {coverage}/4). "
            "Produce a corrected plan that fixes coverage and correctness."
        )
    return {
        "record": record,
        "costs": out["costs"],
        "sound": sound,
        "coverage": coverage,
        "replan_note": note,
    }


def output_gate(
    *,
    logger: EventLogger,
    step: int,
    task: TaskSpec,
    subtasks: list[Any],
    results: list[dict[str, Any]],
    file_sets: list[tuple[Any, dict[str, str]]] | None = None,
    judge: ModelConfig,
    client: Provider,
) -> dict[str, Any]:
    """Audit worker outputs jointly before assembly.

    Returns ``{"record", "costs", "adequate", "weakest"}`` — ``adequate``
    False plus a valid ``weakest`` index means a rework pass is advised."""
    # multi-file results carry metadata only; the bounded bodies live in
    # file_sets keyed by subtask id (an "orchestrator" entry may lead)
    files_by_id = dict(file_sets or [])
    outputs = []
    for i, (sub, res) in enumerate(zip(subtasks, results, strict=False)):
        entry = {
            "index": i,
            "subtask": (sub.get("description") or sub.get("prompt") or "")[:500]
            if isinstance(sub, dict) else str(sub)[:500],
        }
        content = res.get("content")
        sid = sub.get("id", i) if isinstance(sub, dict) else i
        files = files_by_id.get(sid)
        if isinstance(content, dict):
            # fileset protocol — paths + bounded member bodies
            entry["files"] = {
                p: str(b)[:1500] for p, b in list(content.items())[:20]
            }
        elif files:
            # judge sees the bounded bodies; gate logs stay metadata-only
            entry["files"] = {
                p: str(b)[:1500] for p, b in list(files.items())[:20]
            }
        else:
            entry["content"] = str(content or res)[:2000]
        outputs.append(entry)
    state = {
        "task": task.prompt[:4000],
        "worker_outputs": outputs,
    }
    out = _gate(
        logger=logger, step=step, event_type="jev_output_gate", phase="delegate",
        state=state, questions=_output_questions(len(results)),
        judge=judge, client=client,
        input_note={"task": task.id, "outputs": len(results)},
    )
    record = out["record"]
    answers = record.get("answers") or {}
    noul = _noul(answers, "adequate")
    adequate = (noul >= 0.5) if noul is not None else None
    weakest: int | None = None
    w = (answers.get("weakest") or {})
    raw_choice = w.get("answer") or w.get("choice")
    try:
        cand = int(str(raw_choice))
        if 0 <= cand < len(results):
            weakest = cand
    except (TypeError, ValueError):
        pass
    return {
        "record": record,
        "costs": out["costs"],
        "adequate": adequate,
        "weakest": weakest,
    }
