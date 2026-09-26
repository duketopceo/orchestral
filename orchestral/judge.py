"""LLM-as-judge for orchestral, ported from perplexityai/search_evals.

The judge scores the final artifact against the task and returns a score,
pass/fail, and reasoning. It uses the same OpenRouter client as the rest of
harness so it stays provider-agnostic.
"""

from __future__ import annotations

import base64
import random
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import compute_cost, token_usage_from_raw
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterClient
from orchestral.planners import _extract_json, _response_fingerprint

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
    language: str = "html",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return judge result and list of call costs.

    `language` labels the fenced artifact block — multi-file tasks pass a
    neutral tag because their artifact section is a file listing, not HTML.
    """
    if image_bytes is not None and len(image_bytes) > MAX_JUDGE_IMAGE_BYTES:
        result: dict[str, Any] = {"score": None, "passed": None, "reasoning": f"image too large to judge ({len(image_bytes)} bytes)"}
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
        else f"```{language}\n{artifact[:2000]}\n```\n[artifact truncated to first 2000 chars for review]"
    )
    prompt_text = JUDGE_PROMPT.format(prompt=task.prompt, artifact_section=artifact_section)

    if dry_run or client is None:
        result = _fake_judge_result()
        cost: dict[str, Any] = {
            "phase": "judge",
            "model": judge.slug,
            "input_tokens": 300,
            "output_tokens": 80,
            "cost_usd": 0.00005,
            "pricing_source": "none",
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
            pricing_source="none",
        )
        return result, [cost]

    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode()
        user_content: Any = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        # The raw payload is sent to the judge but must not land in
        # events.jsonl — it would duplicate the full artifact as base64.
        log_content: Any = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,[{len(image_bytes)} image bytes redacted]"}},
        ]
    else:
        user_content = prompt_text
        log_content = prompt_text
    messages = [
        {"role": "system", "content": "You are an expert judge. Return only a JSON object."},
        {"role": "user", "content": user_content},
    ]
    log_messages = [
        {"role": "system", "content": "You are an expert judge. Return only a JSON object."},
        {"role": "user", "content": log_content},
    ]
    completion = client.chat(model=judge.slug, messages=messages, max_tokens=4096, temperature=0.2)
    content = completion["content"]
    usage = token_usage_from_raw(completion["usage"])
    cost_usd, _ = compute_cost(usage, judge)
    api_cost = completion.get("api_cost_usd")

    try:
        result = _extract_json(content)
        if not isinstance(result, dict):
            raise ValueError(f"Judge did not return a JSON object: {_response_fingerprint(content)}")
        if "score" not in result or "passed" not in result:
            raise ValueError(f"Judge JSON missing score or passed: {_response_fingerprint(content)}")
        result["score"] = _judge_score(result.get("score", 0.0), content)
        passed = result.get("passed", False)
        # bool("false") is True — a judge returning the string "false" must
        # not be scored as a pass; only bools and true/false strings count
        if isinstance(passed, bool):
            result["passed"] = passed
        else:
            result["passed"] = str(passed).strip().lower() == "true"
    except (TypeError, ValueError) as exc:
        # `reasoning` is a designed, quoted field, not a log: `reporter.py`
        # renders it into the HTML report, the TUI shows it, and
        # `export --format md` writes it into the audit `scrub` publishes. So
        # this line identifies the response and never republishes it —
        # `content[:200]` pasted 200 chars of judge output into all three.
        #
        # Every message that can reach here is model-free by construction:
        # `_extract_json` reports one of three named faults and ends in a
        # fingerprint (DUK-159), the two raises above are static names, and
        # `_judge_score` re-raises rather than forwarding `float()`'s message,
        # which quotes the value it could not convert.
        # `tests/test_judge_parse_failure.py` plants a canary in the response on
        # every path and holds that line.
        result = {
            "score": 0.0,
            "passed": False,
            "reasoning": f"Could not parse judge response: {exc}",
            "parse_failed": True,
        }
    if "reasoning" not in result:
        result["reasoning"] = ""

    logger.log_llm_call(
        phase="judge",
        step=step,
        model=judge.slug,
        role="judge",
        messages=log_messages,
        completion={
            "content": content,
            "usage": usage.to_dict(),
            "id": completion.get("id"),
        },
        reasoning=str(result.get("reasoning") or ""),
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost_usd=cost_usd,
        latency_ms=completion["latency_ms"],
        pricing_source="configured",
        api_cost_usd=api_cost if isinstance(api_cost, (int, float)) else None,
    )

    return result, [{
        "phase": "judge",
        "model": judge.slug,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd,
        "pricing_source": "configured",
        "api_cost_usd": api_cost if isinstance(api_cost, (int, float)) else None,
        "usage": usage.to_dict(),
    }]


def _judge_score(value: Any, response: str) -> float:
    """`float(value)` with a model-free failure message.

    `float("high")` raises `could not convert string to float: 'high'`, which
    quotes the judge's own text — and `judge_artifact` turns that message into
    the parse-failure reason that reaches the report. The values accepted and
    the type raised are unchanged; only the message differs. The exception type
    is kept in the label because it separates a wrong JSON type from a
    non-numeric string, which are different judge faults.
    """
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Judge score was not a number ({type(exc).__name__}): {_response_fingerprint(response)}"
        ) from None


def _fake_judge_result() -> dict[str, Any]:
    return {"score": None, "passed": None, "reasoning": "Dry-run; no judge model was called."}
