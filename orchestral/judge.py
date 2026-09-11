"""LLM-as-judge for orchestral, ported from perplexityai/search_evals.

The judge scores the final artifact against the task and returns a score,
pass/fail, and reasoning. It uses the same OpenRouter client as the rest of
harness so it stays provider-agnostic.
"""

from __future__ import annotations

import json
import random
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import compute_cost, token_usage_from_raw
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterClient

JUDGE_PROMPT = """You are an expert judge evaluating the output of an AI system.

Task: {prompt}

Artifact:
```html
{artifact}
```

Score the artifact from 0.0 to 1.0 based on how well it satisfies the task.
Return only a JSON object with this exact shape:

{{
  "score": <float between 0.0 and 1.0>,
  "passed": <boolean>,
  "reasoning": "<concise explanation>"
}}
"""


def judge_artifact(
    *,
    logger: EventLogger,
    step: int,
    task: TaskSpec,
    artifact: str,
    judge: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return judge result and list of call costs."""
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
            messages=[{"role": "user", "content": JUDGE_PROMPT.format(prompt=task.prompt, artifact=artifact[:2000])}],
            completion=result,
            reasoning="Dry-run judge evaluation.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
        )
        return result, [cost]

    messages = [
        {"role": "system", "content": "You are an expert judge. Return only a JSON object."},
        {"role": "user", "content": JUDGE_PROMPT.format(prompt=task.prompt, artifact=artifact[:2000])},
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


def _extract_json(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError(f"No JSON found in judge response: {content[:200]}")
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and in_string:
            in_string = False
            continue
        if ch == '"' and not in_string:
            in_string = True
            continue
        if in_string:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"Could not extract JSON from judge response: {content[:200]}")
