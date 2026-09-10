"""Planner strategies for the orchestrator: raw vs ce-plan enhanced."""

from __future__ import annotations

import json
import random
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.logger import EventLogger


def _fake_call(
    logger: EventLogger,
    *,
    phase: str,
    step: int,
    model: str,
    role: str,
    input_data: dict[str, Any],
    output_data: dict[str, Any],
    reasoning: str,
) -> dict[str, Any]:
    """Simulate an LLM call with deterministic-ish token counts and cost."""
    input_chars = len(json.dumps(input_data, default=str))
    output_chars = len(json.dumps(output_data, default=str))
    input_tokens = max(100, input_chars // 4 + random.randint(0, 20))
    output_tokens = max(50, output_chars // 4 + random.randint(0, 20))
    price_in, price_out = _prices_from_slug(model)
    cost_usd = (input_tokens * price_in) + (output_tokens * price_out)

    logger.log_llm_call(
        phase=phase,
        step=step,
        model=model,
        role=role,
        messages=[{"role": "user", "content": json.dumps(input_data)}],
        completion=output_data,
        reasoning=reasoning,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=random.uniform(80, 1200),
    )

    return {
        "phase": phase,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }


def _prices_from_slug(slug: str) -> tuple[float, float]:
    if "deepseek" in slug.lower():
        return 0.03 / 1_000_000, 0.10 / 1_000_000
    if "glm" in slug.lower():
        return 0.075 / 1_000_000, 0.25 / 1_000_000
    if "fable" in slug.lower():
        return 10.0 / 1_000_000, 50.0 / 1_000_000
    return 1.0 / 1_000_000, 3.0 / 1_000_000


def plan_raw(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not dry_run:
        raise NotImplementedError("Real OpenRouter calls are not wired yet.")

    plan = {
        "task_id": task.id,
        "orchestrator": orchestrator.slug,
        "subtasks": [
            {"id": 0, "description": "Write the HTML head and page structure"},
            {"id": 1, "description": "Write the hero section"},
            {"id": 2, "description": "Write the signup form"},
        ],
        "reasoning": "Decomposed the landing page into structure, hero, and form subtasks so a cheap worker can write each independently.",
    }
    cost = _fake_call(
        logger,
        phase="plan",
        step=step,
        model=orchestrator.slug,
        role="orchestrator",
        input_data={"prompt": task.prompt},
        output_data=plan,
        reasoning=plan["reasoning"],
    )
    return plan, cost


def assemble_raw(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    results: list[dict[str, Any]],
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    if not dry_run:
        raise NotImplementedError("Real OpenRouter calls are not wired yet.")

    pieces = [r["content"] for r in results]
    artifact = _build_html(task.prompt, pieces)
    cost = _fake_call(
        logger,
        phase="assemble",
        step=step,
        model=orchestrator.slug,
        role="orchestrator",
        input_data={"worker_outputs": results},
        output_data={"artifact_length": len(artifact)},
        reasoning="Merged worker outputs into a single HTML artifact.",
    )
    return artifact, cost


def plan_ce(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """ce-plan style planning: sections, confidence, assumptions, risks."""
    if not dry_run:
        raise NotImplementedError("Real OpenRouter calls are not wired yet.")

    # 1. Structured planning call
    plan = {
        "task_id": task.id,
        "orchestrator": orchestrator.slug,
        "planner": "ce-plan",
        "sections": {
            "goal": f"Produce a responsive landing page for: {task.prompt[:80]}",
            "approach": "Decompose into HTML structure, hero, and signup form; delegate each to a cheap worker; assemble and validate.",
            "subtasks": [
                {
                    "id": 0,
                    "description": "Write the HTML head and page structure",
                    "confidence": 0.95,
                    "risk": "low",
                    "dependencies": [],
                },
                {
                    "id": 1,
                    "description": "Write the hero section",
                    "confidence": 0.85,
                    "risk": "medium",
                    "dependencies": [0],
                },
                {
                    "id": 2,
                    "description": "Write the signup form",
                    "confidence": 0.80,
                    "risk": "medium",
                    "dependencies": [0],
                },
            ],
            "assumptions": [
                "Worker can produce valid HTML snippets from a subtask prompt.",
                "Assembling snippets in order yields a parseable page.",
            ],
            "risks": [
                "Worker may omit required form validation.",
                "Assembled page may lack visual polish unless worker CSS is consistent.",
            ],
            "success_criteria": ["HTML parses", "non-empty", "has title", "has CTA", "has form"],
        },
        "reasoning": "Confidence-weighted plan with explicit dependencies and risks, similar to ce-plan output.",
    }
    cost = _fake_call(
        logger,
        phase="plan",
        step=step,
        model=orchestrator.slug,
        role="orchestrator",
        input_data={"prompt": task.prompt, "planner": "ce-plan"},
        output_data=plan,
        reasoning=plan["reasoning"],
    )

    # 2. Confidence check
    confidence_data = {
        "score": 0.87,
        "assessment": "High confidence in structure and hero; medium risk on form validation.",
        "gating_decision": "proceed",
    }
    _fake_call(
        logger,
        phase="plan",
        step=step + 1,
        model=orchestrator.slug,
        role="orchestrator",
        input_data={"plan": plan},
        output_data=confidence_data,
        reasoning="Self-review the plan against success criteria before delegating.",
    )
    plan["confidence_check"] = confidence_data

    # 3. Headless doc-review (synthetic)
    doc_review = {
        "fixes_applied": 0,
        "proposed_fixes_count": 0,
        "decisions_count": 0,
        "fyi_count": 1,
        "findings": [
            {
                "type": "fyi",
                "message": "Consider adding a meta viewport tag during assembly.",
            }
        ],
    }
    _fake_call(
        logger,
        phase="plan",
        step=step + 2,
        model=orchestrator.slug,
        role="ce-doc-review",
        input_data={"plan": plan},
        output_data=doc_review,
        reasoning="Headless doc-review pass: catch scope/grounding issues before work begins.",
    )
    plan["doc_review"] = doc_review

    total_cost = cost["cost_usd"]
    for c in [plan.get("confidence_check", {}), plan.get("doc_review", {})]:
        # token costs already logged in fake calls above; for the returned cost we keep the first one
        pass

    return plan, cost


def assemble_ce(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    results: list[dict[str, Any]],
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    """ce-plan style assembly with a post-merge review pass."""
    if not dry_run:
        raise NotImplementedError("Real OpenRouter calls are not wired yet.")

    pieces = [r["content"] for r in results]
    artifact = _build_html(task.prompt, pieces)

    # Assembly with explicit review of each worker output
    cost = _fake_call(
        logger,
        phase="assemble",
        step=step,
        model=orchestrator.slug,
        role="orchestrator",
        input_data={"worker_outputs": results, "success_criteria": ["parses", "non_empty", "has_title", "has_cta", "has_form"]},
        output_data={"artifact_length": len(artifact), "passed": True},
        reasoning="Merge worker outputs and check each success criterion before returning the artifact.",
    )

    # Final check pass
    final_check = {
        "checks": {
            "doctype_present": True,
            "meta_viewport": True,
            "title_present": True,
            "cta_present": True,
            "form_present": True,
        },
        "passed": True,
    }
    _fake_call(
        logger,
        phase="assemble",
        step=step + 1,
        model=orchestrator.slug,
        role="ce-work",
        input_data={"artifact": artifact},
        output_data=final_check,
        reasoning="Final verification pass before marking assembly complete.",
    )

    return artifact, cost


def _build_html(prompt: str, pieces: list[str]) -> str:
    import html

    body = "\n".join(f"<section>{p}</section>" for p in pieces)
    return (
        "<!doctype html>\n"
        "<html lang='en'>\n"
        "<head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>orchestral dry-run</title></head>\n"
        f"<body>\n<h1>{html.escape(prompt[:80])}</h1>\n{body}\n</body>\n"
        "</html>"
    )
