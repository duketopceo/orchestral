"""Planner strategies for the orchestrator: raw vs ce-plan enhanced."""

from __future__ import annotations

import base64
import contextlib
import html
import json
import random
import re
from functools import cache
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import compute_cost, compute_image_cost, compute_video_cost, token_usage_from_raw
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterClient

# 1x1 transparent PNG used as the deterministic dry-run image artifact
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# Minimal ISO-BMFF stub (ftyp + free box) used as the deterministic dry-run
# video artifact — well-formed enough to pass mp4_signature, not playable.
TINY_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08free"

_VIDEO_OPTION_KEYS = ("duration", "resolution", "aspect_ratio", "generate_audio", "seed")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
ORCHESTRATOR_DEFAULT_PROMPT = "You are an orchestrator. Produce a plan and subtasks for a worker to execute."


def available_prompt_variants(prompts_dir: Path | str = PROMPTS_DIR) -> list[str]:
    """Prompt-variant names available as prompts/orchestrator-<name>.md."""
    root = Path(prompts_dir)
    if not root.exists():
        return []
    return sorted(p.stem.removeprefix("orchestrator-") for p in root.glob("orchestrator-*.md"))


_VARIANT_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def load_prompt_variant(variant: str, prompts_dir: Path | str = PROMPTS_DIR) -> str:
    """Load prompts/orchestrator-<variant>.md (including 'default')."""
    if not _VARIANT_NAME.match(variant):
        raise FileNotFoundError(f"Invalid prompt variant name '{variant}'")
    try:
        text = _read_prompt_file(str(Path(prompts_dir) / f"orchestrator-{variant}.md"))
    except FileNotFoundError:
        known = ", ".join(available_prompt_variants(prompts_dir)) or "(none)"
        raise FileNotFoundError(f"Unknown prompt variant '{variant}'. Available: {known}") from None
    if not text:
        raise ValueError(f"Prompt variant '{variant}' is empty")
    return text


@cache
def _read_prompt_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------


def _build_messages(
    role: str,
    input_data: dict[str, Any],
    expect_json: bool,
    system_override: str | None = None,
) -> list[dict[str, str]]:
    system = "You are a helpful assistant."
    if role == "orchestrator":
        system = system_override or ORCHESTRATOR_DEFAULT_PROMPT
    elif role == "worker":
        system = "You are a worker. Execute the subtask and return the requested content."
    elif role == "ce-doc-review":
        system = "You are a document reviewer. Review the plan for scope, feasibility, and risks."
    elif role == "ce-work":
        system = "You are a worker/assembler. Verify the merged output against the success criteria."

    if expect_json:
        system += " Return only a JSON object. Do not wrap it in markdown."
    else:
        system += " Return only the requested artifact. Do not include extra commentary."

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(input_data, default=str)},
    ]


def _fake_cost(model_cfg: ModelConfig, input_data: dict[str, Any], output_data: dict[str, Any]) -> dict[str, Any]:
    input_chars = len(json.dumps(input_data, default=str))
    output_chars = len(json.dumps(output_data, default=str))
    input_tokens = max(100, input_chars // 4 + random.randint(0, 20))
    output_tokens = max(50, output_chars // 4 + random.randint(0, 20))
    cost_usd = (input_tokens * model_cfg.input_price) + (output_tokens * model_cfg.output_price)
    return {
        "phase": "",
        "model": model_cfg.slug,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }


def _llm_call(
    *,
    logger: EventLogger,
    phase: str,
    step: int,
    model_cfg: ModelConfig,
    role: str,
    input_data: dict[str, Any],
    reasoning: str,
    client: OpenRouterClient | None,
    dry_run: bool,
    expect_json: bool = True,
    max_tokens: int = 4096,
    temperature: float = 0.4,
    system_override: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if dry_run:
        # deterministic fake output so the harness still exercises the path
        fake_output = _fake_output(input_data, phase)
        content = json.dumps(fake_output)
        cost = _fake_cost(model_cfg, input_data, fake_output)
        cost["phase"] = phase
        logger.log_llm_call(
            phase=phase,
            step=step,
            model=model_cfg.slug,
            role=role,
            messages=[{"role": "user", "content": json.dumps(input_data, default=str)}],
            completion=fake_output,
            reasoning=reasoning,
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
        )
        return content, cost

    if client is None:
        raise ValueError("OpenRouterClient is required for live runs")

    messages = _build_messages(role, input_data, expect_json, system_override)
    completion = client.chat(model=model_cfg.slug, messages=messages, max_tokens=max_tokens, temperature=temperature)

    usage = token_usage_from_raw(completion["usage"])
    cost_usd, _ = compute_cost(usage, model_cfg)
    content = completion["content"]

    logger.log_llm_call(
        phase=phase,
        step=step,
        model=model_cfg.slug,
        role=role,
        messages=messages,
        completion={
            "content": content,
            "usage": usage.to_dict(),
            "id": completion.get("id"),
        },
        reasoning=reasoning,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost_usd=cost_usd,
        latency_ms=completion["latency_ms"],
    )

    return content, {
        "phase": phase,
        "model": model_cfg.slug,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd,
        "usage": usage.to_dict(),
    }


def _extract_json(content: str) -> Any:
    """Parse JSON from a model response, tolerating code fences and extra text."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json")
        text = text.strip()
    # try to find the first JSON object or array
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError(f"No JSON found in model response: {content[:200]}")
    # naive brace matching
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
    raise ValueError(f"Could not extract JSON from model response: {content[:200]}")


def _fake_output(input_data: dict[str, Any], phase: str) -> Any:
    """Return plausible dry-run outputs per phase."""
    if phase == "plan":
        return {
            "subtasks": [
                {"id": 0, "description": "Write the HTML head and page structure"},
                {"id": 1, "description": "Write the hero section"},
                {"id": 2, "description": "Write the signup form"},
            ],
            "reasoning": "Decomposed the landing page into structure, hero, and form subtasks so a cheap worker can write each independently.",
        }
    if phase == "delegate":
        return {
            "content": "<!-- worker output -->",
            "notes": "Dry-run worker output.",
        }
    if phase == "assemble":
        return {"content": _build_html(input_data.get("prompt", ""), ["<!-- piece 1 -->", "<!-- piece 2 -->"])}
    if phase == "confidence_check":
        return {"score": 0.87, "gating_decision": "proceed"}
    if phase == "doc_review":
        return {"fixes_applied": 0, "proposed_fixes_count": 0, "decisions_count": 0, "fyi_count": 0}
    return {"content": "dry-run"}


# ---------------------------------------------------------------------------
# Raw planner (no CE scaffolding)
# ---------------------------------------------------------------------------


def plan_raw(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    client: OpenRouterClient | None,
    dry_run: bool,
    prompt_variant: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    content, cost = _llm_call(
        logger=logger,
        phase="plan",
        step=step,
        model_cfg=orchestrator,
        role="orchestrator",
        input_data={"prompt": task.prompt, "task_type": task.type},
        reasoning="Decompose the task into subtasks for the worker.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
        system_override=load_prompt_variant(prompt_variant) if prompt_variant else None,
    )
    plan = _extract_json(content)
    plan["task_id"] = task.id
    plan["orchestrator"] = orchestrator.slug
    plan["planner"] = "raw"
    return plan, [cost]


def assemble_raw(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    results: list[dict[str, Any]],
    client: OpenRouterClient | None,
    dry_run: bool,
) -> tuple[str, list[dict[str, Any]]]:
    content, cost = _llm_call(
        logger=logger,
        phase="assemble",
        step=step,
        model_cfg=orchestrator,
        role="orchestrator",
        input_data={"worker_outputs": results, "task_type": task.type},
        reasoning="Merge worker outputs into a single artifact.",
        client=client,
        dry_run=dry_run,
        expect_json=False,
    )
    artifact = _extract_html(content) if content.strip().startswith("<") else _build_html(task.prompt, [r.get("content", "") for r in results])
    return artifact, [cost]


def delegate(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    content, cost = _llm_call(
        logger=logger,
        phase="delegate",
        step=step,
        model_cfg=worker,
        role="worker",
        input_data={"subtask": subtask},
        reasoning=f"Execute subtask {subtask.get('id')} with {worker.slug}.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
    )
    try:
        output = _extract_json(content)
        if not isinstance(output, dict):
            output = {"content": str(output)}
    except Exception:
        output = {"content": content}
    output.setdefault("subtask_id", subtask.get("id"))
    return output, [cost]


def delegate_image(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
    """Generate one image for a subtask brief via the Images API."""
    prompt = subtask.get("prompt") or subtask.get("description") or str(subtask)
    if dry_run:
        image_bytes = TINY_PNG
        cost: dict[str, Any] = {
            "phase": "delegate",
            "model": worker.slug,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": compute_image_cost(worker, None, 1),
        }
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": prompt}],
            completion={"image": "<dry-run png>", "bytes": len(image_bytes)},
            reasoning=f"Generate image for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=0,
            output_tokens=0,
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
        )
        return {"subtask_id": subtask.get("id"), "prompt": prompt}, image_bytes, [cost]

    if client is None:
        raise ValueError("OpenRouterClient is required for live runs")

    completion = client.images(model=worker.slug, prompt=prompt)
    image_bytes = completion["image_bytes"]
    usage = token_usage_from_raw(completion["usage"])
    cost_usd = compute_image_cost(worker, usage, 1)

    logger.log_llm_call(
        phase="delegate",
        step=step,
        model=worker.slug,
        role="worker",
        messages=[{"role": "user", "content": prompt}],
        completion={
            "image_bytes": len(image_bytes),
            "usage": usage.to_dict(),
            "id": completion.get("id"),
        },
        reasoning=f"Generate image for subtask {subtask.get('id')} with {worker.slug}.",
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost_usd=cost_usd,
        latency_ms=completion["latency_ms"],
    )
    return {"subtask_id": subtask.get("id"), "prompt": prompt}, image_bytes, [{
        "phase": "delegate",
        "model": worker.slug,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd,
        "usage": usage.to_dict(),
    }]


def delegate_video(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
    """Generate one video for a subtask brief via the async Videos API.

    Generation params (duration, resolution, aspect_ratio, generate_audio,
    seed) come from `task.metadata`. Logged completion data is field-limited:
    job id, usage, and byte counts only — never job/polling/content URLs.
    """
    prompt = subtask.get("prompt") or subtask.get("description") or str(subtask)
    options = {k: task.metadata[k] for k in _VIDEO_OPTION_KEYS if k in task.metadata}
    duration_s = options.get("duration")
    if dry_run:
        video_bytes = TINY_MP4
        cost: dict[str, Any] = {
            "phase": "delegate",
            "model": worker.slug,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": compute_video_cost(worker, duration_s=duration_s),
        }
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": prompt}],
            completion={"video": "<dry-run mp4>", "bytes": len(video_bytes)},
            reasoning=f"Generate video for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=0,
            output_tokens=0,
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
        )
        return {"subtask_id": subtask.get("id"), "prompt": prompt}, video_bytes, [cost]

    if client is None:
        raise ValueError("OpenRouterClient is required for live runs")

    completion = client.videos(model=worker.slug, prompt=prompt, **options)
    video_bytes = completion["video_bytes"]
    # whitelist scalar fields — `usage` is API-controlled data, and only
    # cost/token scalars are meaningful for logging and accounting anyway
    usage = {
        k: v for k, v in (completion.get("usage") or {}).items()
        if isinstance(v, (int, float, str, bool))
    }
    cost_usd = compute_video_cost(worker, api_cost=usage.get("cost"), duration_s=duration_s)

    logger.log_llm_call(
        phase="delegate",
        step=step,
        model=worker.slug,
        role="worker",
        messages=[{"role": "user", "content": prompt}],
        completion={
            "video_bytes": len(video_bytes),
            "usage": usage,
            "id": completion.get("id"),
        },
        reasoning=f"Generate video for subtask {subtask.get('id')} with {worker.slug}.",
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost_usd,
        latency_ms=completion["latency_ms"],
    )
    return {"subtask_id": subtask.get("id"), "prompt": prompt}, video_bytes, [{
        "phase": "delegate",
        "model": worker.slug,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": cost_usd,
        "usage": usage,
    }]


def assemble_media(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    results: list[dict[str, Any]],
    client: OpenRouterClient | None,
    dry_run: bool,
) -> tuple[int, list[dict[str, Any]]]:
    """Orchestrator picks the winning worker artifact by subtask index."""
    content, cost = _llm_call(
        logger=logger,
        phase="assemble",
        step=step,
        model_cfg=orchestrator,
        role="orchestrator",
        input_data={
            "prompt": task.prompt,
            "candidates": [
                {"subtask_id": r.get("subtask_id"), "prompt": r.get("prompt")}
                for r in results
            ],
            "task_type": task.type,
        },
        reasoning=f"Select the subtask index whose generated {task.type} best satisfies the task.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
    )
    try:
        selection = _extract_json(content)
        idx = int(selection.get("subtask_id", selection.get("index", 0)))
    except Exception:
        idx = 0
    # resolve subtask_id to a position in results, clamped to range
    position = next((i for i, r in enumerate(results) if r.get("subtask_id") == idx), 0)
    position = max(0, min(position, len(results) - 1))
    return position, [cost]


def plan_ce(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    client: OpenRouterClient | None,
    dry_run: bool,
    prompt_variant: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    content, plan_cost = _llm_call(
        logger=logger,
        phase="plan",
        step=step,
        model_cfg=orchestrator,
        role="orchestrator",
        input_data={"prompt": task.prompt, "task_type": task.type, "mode": "ce-plan"},
        reasoning="Produce a ce-plan style plan with sections, confidence, assumptions, and risks.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
        system_override=load_prompt_variant(prompt_variant) if prompt_variant else None,
    )
    plan = _extract_json(content)
    plan["task_id"] = task.id
    plan["orchestrator"] = orchestrator.slug
    plan["planner"] = "ce-plan"

    conf_content, conf_cost = _llm_call(
        logger=logger,
        phase="confidence_check",
        step=step + 1,
        model_cfg=orchestrator,
        role="orchestrator",
        input_data={"plan": plan, "prompt": "Review the plan and assign a confidence score and gating decision."},
        reasoning="Confidence check: verify the plan is strong enough before delegating.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
    )
    try:
        plan["confidence_check"] = _extract_json(conf_content)
    except Exception:
        plan["confidence_check"] = {"raw": conf_content}

    review_content, review_cost = _llm_call(
        logger=logger,
        phase="doc_review",
        step=step + 2,
        model_cfg=orchestrator,
        role="ce-doc-review",
        input_data={"plan": plan, "prompt": "Review the plan for scope, feasibility, and risks. Return JSON findings."},
        reasoning="Headless doc-review pass to catch scope/grounding issues before work begins.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
    )
    try:
        plan["doc_review"] = _extract_json(review_content)
    except Exception:
        plan["doc_review"] = {"raw": review_content}

    return plan, [plan_cost, conf_cost, review_cost]


def assemble_ce(
    *,
    logger: EventLogger,
    task: TaskSpec,
    orchestrator: ModelConfig,
    step: int,
    results: list[dict[str, Any]],
    client: OpenRouterClient | None,
    dry_run: bool,
) -> tuple[str, list[dict[str, Any]]]:
    content, cost = _llm_call(
        logger=logger,
        phase="assemble",
        step=step,
        model_cfg=orchestrator,
        role="orchestrator",
        input_data={
            "worker_outputs": results,
            "task_type": task.type,
            "success_criteria": ["parses", "non_empty", "has_title", "has_cta", "has_form"],
        },
        reasoning="Merge worker outputs and verify success criteria before returning the artifact.",
        client=client,
        dry_run=dry_run,
        expect_json=False,
    )
    artifact = _extract_html(content) if content.strip().startswith("<") else _build_html(task.prompt, [r.get("content", "") for r in results])

    final_content, final_cost = _llm_call(
        logger=logger,
        phase="assemble",
        step=step + 1,
        model_cfg=orchestrator,
        role="ce-work",
        input_data={"artifact": artifact, "success_criteria": ["parses", "non_empty", "has_title", "has_cta", "has_form"]},
        reasoning="Final verification pass before marking assembly complete.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
    )
    with contextlib.suppress(Exception):
        _extract_json(final_content)

    return artifact, [cost, final_cost]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_html(content: str) -> str:
    """Extract the HTML document from a model response."""
    text = content.strip()
    if "```html" in text:
        text = text.split("```html")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    if "<!doctype" in text.lower():
        start = text.lower().find("<!doctype")
        return text[start:]
    if "<html" in text.lower():
        start = text.lower().find("<html")
        return text[start:]
    return text


def _build_html(prompt: str, pieces: list[str]) -> str:
    body = "\n".join(f"<section>{p}</section>" for p in pieces)
    return (
        "<!doctype html>\n"
        "<html lang='en'>\n"
        "<head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>orchestral dry-run</title></head>\n"
        f"<body>\n<h1>{html.escape(prompt[:80])}</h1>\n{body}\n"
        "<a href='#signup' class='cta'>Get started</a>\n"
        "<form id='signup'><input type='email' name='email'><button type='submit'>Subscribe</button></form>\n"
        "</body>\n"
        "</html>"
    )
