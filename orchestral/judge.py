"""LLM-as-judge for orchestral, ported from perplexityai/search_evals.

The judge scores the final artifact against the task and returns a score,
pass/fail, and reasoning. It uses the same OpenRouter client as the rest of
harness so it stays provider-agnostic.
"""

from __future__ import annotations

import base64
import json
import random
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import compute_cost, token_usage_from_raw
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterClient
from orchestral.planners import _extract_json

JUDGE_PROMPT = """You are an expert judge evaluating the output of an AI system.

Task: {prompt}

Artifact:
{artifact_section}

Score the artifact from 0.0 to 1.0 based on how well it satisfies the task.
Return only a JSON object with this exact shape:

{{
  "score": <float between 0.0 and 1.0>,
  "passed": <boolean>,
  "reasoning": "<concise explanation>"
}}
"""

# base64 inflates ~33%, so this keeps judge payloads under ~10MB
MAX_JUDGE_IMAGE_BYTES = 7_500_000


def judge_artifact(
    *,
    logger: EventLogger,
    step: int,
    task: TaskSpec,
    artifact: str,
    judge: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    image_bytes: bytes | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return judge result and list of call costs."""
    if image_bytes is not None and len(image_bytes) > MAX_JUDGE_IMAGE_BYTES:
        result = {"score": None, "passed": None, "reasoning": f"image too large to judge ({len(image_bytes)} bytes)"}
        logger.log(
            phase="judge",
            step=step,
            event_type="judge_skipped",
            model=judge.slug,
            role="judge",
            input_data={"task": task.id},
            output_data=result,
            reasoning="Image exceeds judge payload cap; skipped without an API call.",
        )
        return result, []
    artifact_section = (
        "The artifact is the attached image."
        if image_bytes is not None
        else f"```html\n{artifact[:2000]}\n```"
    )
    prompt_text = JUDGE_PROMPT.format(prompt=task.prompt, artifact_section=artifact_section)

    if dry_run or client is None:
        result = _fake_judge_result()
        cost = {
            "phase": "judge",
            "model": judge.slug,
            "input_tokens": 300,
            "output_tokens": 80,
            "cost_usd": 0.00005,
            "usage": None,
        }
        logger.log_llm_call(
            phase="judge",
            step=step,
            model=judge.slug,
            role="judge",
            messages=[{"role": "user", "content": prompt_text}],
            completion=result,
            reasoning="Dry-run judge evaluation.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
        )
        return result, [cost]

    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode()
        user_content: Any = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    else:
        user_content = prompt_text
    messages = [
        {"role": "system", "content": "You are an expert judge. Return only a JSON object."},
        {"role": "user", "content": user_content},
    ]
    completion = client.chat(model=judge.slug, messages=messages, max_tokens=4096, temperature=0.2)
    content = completion["content"]
    usage = token_usage_from_raw(completion["usage"])
    cost_usd, _ = compute_cost(usage, judge)

    try:
        result = _extract_json(content)
        if not isinstance(result, dict):
            raise ValueError("Judge did not return a JSON object")
        if "score" not in result or "passed" not in result:
            raise ValueError("Judge JSON missing score or passed")
    except Exception:
        result = {
            "score": 0.0,
            "passed": False,
            "reasoning": f"Could not parse judge response: {content[:200]}",
            "parse_failed": True,
        }

    result["score"] = float(result.get("score", 0.0))
    result["passed"] = bool(result.get("passed", False))
    if "reasoning" not in result:
        result["reasoning"] = ""

    logger.log_llm_call(
        phase="judge",
        step=step,
        model=judge.slug,
        role="judge",
        messages=messages,
        completion={
            "content": content,
            "usage": usage.to_dict(),
            "id": completion.get("id"),
        },
        reasoning=result.get("reasoning", ""),
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost_usd=cost_usd,
        latency_ms=completion["latency_ms"],
    )

    return result, [{
        "phase": "judge",
        "model": judge.slug,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd,
        "usage": usage.to_dict(),
    }]


def _fake_judge_result() -> dict[str, Any]:
    return {"score": None, "passed": None, "reasoning": "Dry-run; no judge model was called."}
