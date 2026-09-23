"""Planner strategies for the orchestrator: raw vs ce-plan enhanced."""

from __future__ import annotations

import ast
import base64
import contextlib
import html
import json
import random
import re
import subprocess
import threading
import time
from functools import cache
from pathlib import Path
from typing import Any

from orchestral import agentexec
from orchestral.agentexec import AgentAdapter
from orchestral.apistub import parse_plan
from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import compute_cost, compute_image_cost, compute_video_cost, token_usage_from_raw
from orchestral.fileset import (
    FilesetError,
    check_response_size,
    expected_paths,
    member_requirements,
    parse_fileset,
    summarize_fileset,
)
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterClient
from orchestral.sqlexec import extract_sql

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


class PlanError(ValueError):
    """Orchestrator returned a non-object plan — malformed model output."""


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
        "pricing_source": "none",
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
    max_tokens: int | None = None,
    temperature: float = 0.4,
    system_override: str | None = None,
    attempt: int | None = None,
) -> tuple[str, dict[str, Any]]:
    if max_tokens is None:
        # honor the model's configured output ceiling — a hardcoded 4096
        # truncates file-set worker payloads (finish_reason=length mid-JSON)
        max_tokens = int(getattr(model_cfg, "max_tokens", None) or 4096)
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
            pricing_source="none",
            attempt=attempt,
        )
        return content, cost

    if client is None:
        raise ValueError("OpenRouterClient is required for live runs")

    messages = _build_messages(role, input_data, expect_json, system_override)
    completion = client.chat(model=model_cfg.slug, messages=messages, max_tokens=max_tokens, temperature=temperature)

    usage = token_usage_from_raw(completion["usage"])
    cost_usd, _ = compute_cost(usage, model_cfg)
    content = completion["content"]
    api_cost = completion.get("api_cost_usd")

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
            "finish_reason": completion.get("finish_reason"),
        },
        reasoning=reasoning,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost_usd=cost_usd,
        latency_ms=completion["latency_ms"],
        pricing_source="configured",
        api_cost_usd=api_cost if isinstance(api_cost, (int, float)) else None,
        attempt=attempt,
    )

    return content, {
        "phase": phase,
        "model": model_cfg.slug,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd,
        "pricing_source": "configured",
        "api_cost_usd": api_cost if isinstance(api_cost, (int, float)) else None,
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
        raise PlanError(f"No JSON found in model response: {content[:200]}")
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
    if not isinstance(plan, dict):
        # e.g. the model answered the task directly with a JSON list —
        # malformed orchestrator output, not an infra error
        raise PlanError(f"orchestrator plan must be a JSON object, got {type(plan).__name__}")
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
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
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
        attempt=attempt,
    )
    try:
        output = _extract_json(content)
        if not isinstance(output, dict):
            output = {"content": str(output)}
    except Exception:
        output = {"content": content}
    output.setdefault("subtask_id", subtask.get("id"))
    if "content" not in output:
        # workers answer with descriptive keys — {"release_token": ...},
        # {"blurb": ...} — which would otherwise surface as an empty artifact
        payload = {
            k: v for k, v in output.items() if k not in ("subtask_id", "prompt")
        }
        if len(payload) == 1 and isinstance(next(iter(payload.values())), str):
            output["content"] = next(iter(payload.values()))
        elif payload:
            output["content"] = json.dumps(payload, ensure_ascii=False)
    return output, [cost]


def delegate_image(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
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
            "pricing_source": "none",
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
            pricing_source="none",
            attempt=attempt,
        )
        return {"subtask_id": subtask.get("id"), "prompt": prompt}, image_bytes, [cost]

    if client is None:
        raise ValueError("OpenRouterClient is required for live runs")

    completion = client.images(model=worker.slug, prompt=prompt)
    image_bytes = completion["image_bytes"]
    usage = token_usage_from_raw(completion["usage"])
    cost_usd = compute_image_cost(worker, usage, 1)
    api_cost = completion.get("api_cost_usd")

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
        pricing_source="configured",
        api_cost_usd=api_cost if isinstance(api_cost, (int, float)) else None,
        attempt=attempt,
    )
    return {"subtask_id": subtask.get("id"), "prompt": prompt}, image_bytes, [{
        "phase": "delegate",
        "model": worker.slug,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd,
        "pricing_source": "configured",
        "api_cost_usd": api_cost if isinstance(api_cost, (int, float)) else None,
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
    attempt: int | None = None,
    seed: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
    """Generate one video for a subtask brief via the async Videos API.

    Generation params (duration, resolution, aspect_ratio, generate_audio,
    seed) come from `task.metadata`. Logged completion data is field-limited:
    job id, usage, and byte counts only — never job/polling/content URLs.
    """
    prompt = subtask.get("prompt") or subtask.get("description") or str(subtask)
    options = {k: task.metadata[k] for k in _VIDEO_OPTION_KEYS if k in task.metadata}
    if "seed" not in options and seed is not None:
        options["seed"] = seed
    duration_s = options.get("duration")
    resolution = options.get("resolution")
    generate_audio = options.get("generate_audio")
    if dry_run:
        video_bytes = TINY_MP4
        cost: dict[str, Any] = {
            "phase": "delegate",
            "model": worker.slug,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": compute_video_cost(
                worker, duration_s=duration_s, resolution=resolution, generate_audio=generate_audio
            ),
            "pricing_source": "none",
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
            pricing_source="none",
            attempt=attempt,
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
    api_cost = usage.get("cost")
    cost_usd = compute_video_cost(
        worker,
        api_cost=api_cost,
        duration_s=duration_s,
        resolution=resolution,
        generate_audio=generate_audio,
    )
    # the async API reports an authoritative usage.cost on the completed job;
    # anything else is a configured rate-card estimate
    pricing_source = "api_reported" if isinstance(api_cost, (int, float)) else "configured_estimate"

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
        pricing_source=pricing_source,
        api_cost_usd=api_cost if isinstance(api_cost, (int, float)) else None,
        attempt=attempt,
    )
    return {"subtask_id": subtask.get("id"), "prompt": prompt}, video_bytes, [{
        "phase": "delegate",
        "model": worker.slug,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": cost_usd,
        "pricing_source": pricing_source,
        "api_cost_usd": api_cost if isinstance(api_cost, (int, float)) else None,
        "usage": usage,
    }]


def delegate_multi(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """Produce one file set for a subtask brief.

    Bypasses `_llm_call` on purpose: its dry-run fake output and its completion
    logging both assume a single text artifact, and file *contents* must never
    reach the event log. Only paths, sizes, and hashes are traced.
    """
    prompt = subtask.get("prompt") or subtask.get("description") or str(subtask)
    declared = expected_paths(task.metadata)
    if not declared:
        # code/bugfix tasks default to the declared module; multi-file to a site
        declared = [str(task.metadata.get("module") or "solution.py")] if task.type in ("code", "bugfix") else ["index.html", "style.css"]
    if dry_run:
        files = {path: _fake_file_body(path) for path in declared}
        for member, tokens in member_requirements(task.metadata).items():
            if member in files:
                files[member] += "\n".join(tokens) + "\n"
        cost = _fake_cost(worker, {"subtask": subtask}, {"paths": sorted(files)})
        cost["phase"] = "delegate"
        summary = summarize_fileset(files)
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": prompt}],
            completion={
                "file_count": len(files),
                "total_bytes": sum(summary["sizes"].values()),
                "paths": summary["paths"],
            },
            reasoning=f"Produce a {len(files)}-file set for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "notes": "Dry-run file set.",
            **summary,
        }, files, [cost]

    if client is None:
        raise ValueError("OpenRouterClient is required for live runs")

    # bugfix subtasks carry the broken repo in metadata.files so the worker
    # repairs instead of generating from scratch
    if task.type == "bugfix":
        subtask = {**subtask, "broken_files": task.metadata.get("files") or {}}
    messages = _build_messages(
        "worker",
        {
            "subtask": subtask,
            "task_type": task.type,
            "instructions": (
                "Repair the files in broken_files. Return a JSON object "
                "{\"files\": [{\"path\": ..., \"content\": ...}]} with the complete corrected file set."
                if task.type == "bugfix" else
                "Return a JSON object {\"files\": [{\"path\": ..., \"content\": ...}]} "
                "containing every file this subtask must produce."
            ),
        },
        expect_json=True,
    )
    completion = client.chat(model=worker.slug, messages=messages)
    content = completion["content"]
    check_response_size(content)
    try:
        data = _extract_json(content)
    except ValueError as exc:
        raise FilesetError(f"Worker returned no parseable file set: {exc}") from exc
    files = parse_fileset(data)
    if not files:
        raise FilesetError("Worker returned no usable files")

    usage = token_usage_from_raw(completion["usage"])
    cost_usd, _ = compute_cost(usage, worker)
    api_cost = completion.get("api_cost_usd")
    summary = summarize_fileset(files)
    logger.log_llm_call(
        phase="delegate",
        step=step,
        model=worker.slug,
        role="worker",
        messages=messages,
        completion={
            "file_count": len(files),
            "total_bytes": sum(summary["sizes"].values()),
            "paths": summary["paths"],
            "usage": usage.to_dict(),
            "id": completion.get("id"),
            "finish_reason": completion.get("finish_reason"),
        },
        reasoning=f"Produce a {len(files)}-file set for subtask {subtask.get('id')} with {worker.slug}.",
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost_usd=cost_usd,
        latency_ms=completion["latency_ms"],
        pricing_source="configured",
        api_cost_usd=api_cost if isinstance(api_cost, (int, float)) else None,
        attempt=attempt,
    )
    notes = data.get("notes") if isinstance(data, dict) else None
    return {
        "subtask_id": subtask.get("id"),
        "notes": notes if isinstance(notes, str) else None,
        **summary,
    }, files, [{
        "phase": "delegate",
        "model": worker.slug,
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd,
        "pricing_source": "configured",
        "api_cost_usd": api_cost if isinstance(api_cost, (int, float)) else None,
        "usage": usage.to_dict(),
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
                {
                    "subtask_id": r.get("subtask_id"),
                    "prompt": r.get("prompt"),
                    "query": r.get("query"),
                    # bounded preview — without it the orchestrator picks blind
                    "content": str(r.get("content") or "")[:500],
                }
                for r in results
            ],
            "task_type": task.type,
        },
        reasoning=f"Select the subtask index whose generated {task.type} best satisfies the task.",
        client=client,
        dry_run=dry_run,
        expect_json=True,
    )
    position, resolved = _resolve_pick(content, results)
    if not resolved:
        # the pick is part of what is being measured — log it loudly rather
        # than silently degrading to candidate 0
        logger.log(
            phase="assemble",
            step=step,
            event_type="pick_unresolved",
            model=orchestrator.slug,
            role="orchestrator",
            input_data={"task": task.id},
            output_data={"raw": content[:400]},
            reasoning="Orchestrator selection carried no resolvable subtask_id/index; falling back to the first candidate.",
        )
    return position, [cost]


def _iter_json_objects(text: str):
    """Yield every top-level JSON object/array decodable from `text`, in order."""
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        m = re.search(r"[\[{]", text[pos:])
        if not m:
            return
        start = pos + m.start()
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        yield obj
        pos = start + end


def _resolve_pick(content: str, results: list[dict[str, Any]]) -> tuple[int, bool]:
    """Map an orchestrator selection response to a results position.

    Models commonly wrap the answer — `{"plan": "..."}` reasoning first, then
    `{"subtask_id": 3}`. Taking only the first JSON object resolves nothing and
    silently picks candidate 0, which is how correct worker outputs got dropped
    in favor of an earlier partial. `subtask_id` is resolved as an id lookup;
    `index` is treated as a 0-based position.
    """
    for obj in _iter_json_objects(content):
        if not isinstance(obj, dict):
            continue
        for key in ("subtask_id", "index"):
            value = obj.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if key == "subtask_id":
                pos = next(
                    (i for i, r in enumerate(results) if r.get("subtask_id") == value),
                    None,
                )
                if pos is None:
                    # the model may have meant "position" under the wrong key —
                    # accept it if it lands in range rather than failing
                    pos = value if 0 <= value < len(results) else None
            else:
                pos = value if 0 <= value < len(results) else None
            if pos is not None:
                return pos, True
    return 0, False


def delegate_constraint(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce one candidate constrained text for a subtask.

    Dry runs return `metadata.reference_text` when present — a compliant
    example that proves the task's constraints are self-consistent — and
    generic fake content otherwise.
    """
    if dry_run and task.metadata.get("reference_text"):
        reference = str(task.metadata["reference_text"])
        cost = _fake_cost(worker, {"subtask": subtask}, {"chars": len(reference)})
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": str(subtask.get("description", ""))}],
            completion={"chars": len(reference)},
            reasoning=f"Write constrained text for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
            "content": reference,
        }, [cost]
    return delegate(
        logger=logger,
        step=step,
        subtask=subtask,
        worker=worker,
        client=client,
        dry_run=dry_run,
        attempt=attempt,
    )


def delegate_needle(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce one candidate answer for a needle-in-haystack subtask.

    `metadata.document` (the haystack) rides inside the subtask dict so the
    worker sees it in the normal `{"subtask": ...}` message. Dry runs return
    `metadata.expected_answer` so validation exercises the real token checks.
    """
    if dry_run:
        reference = str(task.metadata.get("expected_answer") or "")
        cost = _fake_cost(worker, {"subtask": subtask}, {"chars": len(reference)})
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": str(subtask.get("description", ""))}],
            completion={"chars": len(reference)},
            reasoning=f"Find the needle for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
            "content": reference,
        }, [cost]

    with_document = dict(subtask)
    if task.metadata.get("document"):
        with_document["document"] = task.metadata["document"]
    return delegate(
        logger=logger,
        step=step,
        subtask=with_document,
        worker=worker,
        client=client,
        dry_run=dry_run,
        attempt=attempt,
    )



def delegate_sql(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce one candidate SQL query for a subtask.

    The worker output keeps the normal `content` contract; `query` carries
    the extracted SQL so the orchestrator's pick maps to a real artifact.
    Dry runs return the task's reference SQL — validation then executes real
    sqlite against the fixture, so a dry run also proves the task spec works.
    """
    if dry_run:
        reference = str(task.metadata.get("reference_sql") or "SELECT 1")
        cost = _fake_cost(worker, {"subtask": subtask}, {"query": "<reference>"})
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": str(subtask.get("description", ""))}],
            completion={"query_chars": len(reference)},
            reasoning=f"Produce a SQL query for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
            "content": reference,
            "query": reference,
        }, [cost]

    out, costs = delegate(
        logger=logger,
        step=step,
        subtask=subtask,
        worker=worker,
        client=client,
        dry_run=dry_run,
        attempt=attempt,
    )
    out["query"] = extract_sql(out)
    return out, costs



def delegate_extract(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce one candidate extraction for a subtask.

    `content` is the extracted JSON (text or fenced). Dry runs return
    `metadata.expected` so validation exercises the real grading path and
    proves the task spec is self-consistent.
    """
    if dry_run:
        reference = json.dumps(task.metadata.get("expected") or {})
        cost = _fake_cost(worker, {"subtask": subtask}, {"json_chars": len(reference)})
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": str(subtask.get("description", ""))}],
            completion={"json_chars": len(reference)},
            reasoning=f"Extract structured data for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
            "content": reference,
        }, [cost]

    out, costs = delegate(
        logger=logger,
        step=step,
        subtask=subtask,
        worker=worker,
        client=client,
        dry_run=dry_run,
        attempt=attempt,
    )
    if "content" not in out:
        # delegate() JSON-parses worker output — a worker that returns the
        # extraction object directly lands in `out` itself, not under content
        out["content"] = json.dumps(
            {k: v for k, v in out.items() if k not in ("subtask_id", "prompt")}
        )
    return out, costs



def delegate_api(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce one candidate request plan for a subtask.

    `content` is a JSON list of {method, path, json?, params?} calls. Dry
    runs return `metadata.calls` so validation replays the expected plan
    against the real stub and proves the spec is self-consistent.
    """
    if dry_run:
        reference = json.dumps(task.metadata.get("calls") or [])
        cost = _fake_cost(worker, {"subtask": subtask}, {"json_chars": len(reference)})
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": str(subtask.get("description", ""))}],
            completion={"json_chars": len(reference)},
            reasoning=f"Write an API request plan for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
            "content": reference,
        }, [cost]

    out, costs = delegate(
        logger=logger,
        step=step,
        subtask=subtask,
        worker=worker,
        client=client,
        dry_run=dry_run,
        attempt=attempt,
    )
    if "content" not in out:
        # a worker returning a bare JSON object lands in `out` itself
        plan = {k: v for k, v in out.items() if k not in ("subtask_id", "prompt")}
        out["content"] = json.dumps(plan.get("calls", list(plan.values()) if len(plan) == 1 else plan))
    elif parse_plan(str(out["content"])) is None:
        # delegate() str()-ifies parsed JSON lists into Python reprs —
        # literal_eval recovers the original so the plan stays valid JSON
        with contextlib.suppress(ValueError, SyntaxError):
            out["content"] = json.dumps(ast.literal_eval(str(out["content"])))
    return out, costs


def delegate_terminal(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce one candidate shell-command plan for a subtask.

    `content` is a JSON list of {"run": "..."} commands replayed in the
    virtual shell at validation. Dry runs return `metadata.commands` so the
    task spec's reference plan proves itself against `metadata.expect`.
    """
    if dry_run:
        reference = json.dumps(task.metadata.get("commands") or [])
        cost = _fake_cost(worker, {"subtask": subtask}, {"json_chars": len(reference)})
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": str(subtask.get("description", ""))}],
            completion={"json_chars": len(reference)},
            reasoning=f"Write a shell command plan for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
            "content": reference,
        }, [cost]

    out, costs = delegate(
        logger=logger,
        step=step,
        subtask=subtask,
        worker=worker,
        client=client,
        dry_run=dry_run,
        attempt=attempt,
    )
    if "content" not in out:
        plan = {k: v for k, v in out.items() if k not in ("subtask_id", "prompt")}
        out["content"] = json.dumps(plan.get("commands", list(plan.values()) if len(plan) == 1 else plan))
    elif parse_plan(str(out["content"])) is None:
        with contextlib.suppress(ValueError, SyntaxError):
            out["content"] = json.dumps(ast.literal_eval(str(out["content"])))
    return out, costs


def delegate_patch(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    client: OpenRouterClient | None,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce one candidate unified diff for a subtask.

    The worker sees `repo_files` (metadata.files) and must return a patch —
    {"patch": "<unified diff>"} or raw diff text. Dry runs return
    `metadata.patch` so the spec's reference diff proves itself against the
    hidden tests.
    """
    if dry_run:
        reference = str(task.metadata.get("patch") or "")
        cost = _fake_cost(worker, {"subtask": subtask}, {"patch_chars": len(reference)})
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": str(subtask.get("description", ""))}],
            completion={"patch_chars": len(reference)},
            reasoning=f"Write a unified diff for subtask {subtask.get('id')} with {worker.slug}.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        return {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
            "content": reference,
        }, [cost]

    enriched = {**subtask, "repo_files": task.metadata.get("files") or {},
                "output_contract": "Return a JSON object {\"patch\": \"<unified diff>\"} — "
                                   "the diff must apply cleanly to repo_files."}
    out, costs = delegate(
        logger=logger,
        step=step,
        subtask=enriched,
        worker=worker,
        client=client,
        dry_run=dry_run,
        attempt=attempt,
    )
    if "content" not in out:
        # {"patch": "..."} JSON output, or a bare dict holding the diff
        out["content"] = str(out.get("patch") or json.dumps(
            {k: v for k, v in out.items() if k not in ("subtask_id", "prompt")}
        ))
    return out, costs


# conservative per-attempt guess when a CLI reports no usage — the ledger
# must cover executor runs so `spend_today` can't be bypassed by silence
EXECUTOR_FLAT_ESTIMATE_USD = 0.25

# added diff-lines shaped like instructions to the judge — the judge reads
# agent-controlled text, so these are flagged as evidence, not hidden
_INJECTION_HINT = re.compile(
    r"^\+\s*[^\n]*(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions"
    r"|verdict\s*[:=]\s*(?:pass|true)|score\s*[:=]\s*(?:1\.0|10)"
    r"|note\s+to\s+(?:the\s+)?judge|dear\s+judge)",
    re.IGNORECASE | re.MULTILINE,
)


def _repo_status(root: Path) -> set[str]:
    """`git status --porcelain` baseline for the repo-mutation tripwire.

    Empty on any failure — a non-git cwd simply disables the check rather
    than blocking the attempt.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        return set(out.stdout.splitlines())
    except Exception:
        return set()


def _oracle_probe_needles(
    transcript_path: Path | None,
    repo_root: Path | None,
    task: TaskSpec,
) -> list[str]:
    """Scan the captured transcript for oracle-adjacent references.

    The filesystem is open to the agent — containment is environmental, so
    reads of task oracles are *detected*, not prevented. A mention is not
    proof of a read, which is why this is a warning milestone.
    """
    if transcript_path is None or not Path(transcript_path).exists():
        return []
    text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    needles: list[str] = []
    if repo_root is not None and str(repo_root) in text:
        needles.append("repo_root")
    if re.search(r"tasks/[^\s'\"]+\.ya?ml", text):
        needles.append("task_spec")
    if "metadata.tests" in text or "hidden test" in text.lower():
        needles.append("oracle")
    return needles


def delegate_agentic(
    *,
    logger: EventLogger,
    step: int,
    subtask: dict[str, Any],
    task: TaskSpec,
    worker: ModelConfig,
    adapter: AgentAdapter,
    dry_run: bool,
    attempt: int | None = None,
    cancel_event: threading.Event | None = None,
    evidence_dir: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, str], str, list[dict[str, Any]]]:
    """Run one agent-CLI attempt for a subtask.

    Returns (out, files, diff, costs): `files` is the harvested workspace
    fileset for multi-type tasks, `diff` is the unified diff that becomes
    the artifact for patch-shaped tasks (and the judge's input). `out` is
    content-free for fileset tasks — worker-*.json carries paths/sizes/
    hashes, never agent file bodies; the diff only lands in `content` where
    the artifact contract already publishes it (swe-patch), mirroring
    delegate_patch.

    The `llm_call` event's completion carries usage and milestones —
    transcript text and file bodies never enter it. Raw evidence lives
    under `evidence_dir` (the run's raw/ dir), omitted from scrubbed output.
    """
    prompt = str(subtask.get("task_prompt") or task.prompt)
    desc = str(subtask.get("description") or "").strip()
    if desc and desc != task.prompt:
        prompt = f"{prompt}\n\n## Subtask {subtask.get('id')}\n{desc}"
    is_fileset = task.type in ("multi-file", "code", "bugfix")

    if dry_run:
        # never spawn — return the spec's declared reference oracle so the
        # pipeline still proves itself end-to-end
        if is_fileset:
            files = {
                str(k): str(v)
                for k, v in (
                    (task.metadata.get("reference_files")
                     or task.metadata.get("files"))
                    or {}
                ).items()
            }
            reference_diff = ""
        else:
            files = {}
            reference_diff = str(task.metadata.get("patch") or "")
        completion = {
            "executor": adapter.name,
            "dry_run": True,
            "reference": "metadata.files" if is_fileset else "metadata.patch",
        }
        cost = _fake_cost(worker, {"subtask": subtask.get("id")}, completion)
        cost["phase"] = "delegate"
        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": prompt}],
            completion=completion,
            reasoning=f"Agent-executor dry run for subtask {subtask.get('id')} ({adapter.name}); returning the reference oracle.",
            input_tokens=cost["input_tokens"],
            output_tokens=cost["output_tokens"],
            cost_usd=cost["cost_usd"],
            latency_ms=random.uniform(80, 1200),
            pricing_source="none",
            attempt=attempt,
        )
        out: dict[str, Any] = {
            "subtask_id": subtask.get("id"),
            "prompt": subtask.get("prompt") or subtask.get("description"),
        }
        if is_fileset:
            out.update(summarize_fileset(files))
        else:
            out["content"] = reference_diff
        return out, files, reference_diff, [cost]

    baseline = _repo_status(repo_root) if repo_root is not None else None
    t0 = time.perf_counter()
    result = agentexec.run_attempt(
        adapter,
        prompt=prompt,
        files={str(k): str(v) for k, v in (task.metadata.get("files") or {}).items()},
        timeout=float(task.metadata.get("timeout_seconds") or agentexec.DEFAULT_TIMEOUT_SECONDS),
        cancel_event=cancel_event,
        allow_hidden=bool(task.metadata.get("allow_hidden")),
        evidence_dir=evidence_dir,
    )
    latency_ms = (time.perf_counter() - t0) * 1000

    # usage -> pricing source: CLI-reported usage, a flat estimate when the
    # CLI is silent, or a declared-unmetered worker (never a $0 free pass)
    usage = result.usage or {}
    unmetered = adapter.unmetered or bool((worker.metadata or {}).get("executor_unmetered"))
    if unmetered:
        pricing_source, cost_usd, in_tok, out_tok = "unmetered", 0.0, 0, 0
    elif result.usage:
        pricing_source = "cli_reported"
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or usage.get("tokens") or 0)
        cost_usd = float(
            usage.get("usd")
            or (in_tok * worker.input_price + out_tok * worker.output_price)
        )
    else:
        pricing_source, cost_usd, in_tok, out_tok = (
            "flat_estimate", EXECUTOR_FLAT_ESTIMATE_USD, 0, 0,
        )

    # tripwire 1: the repo tree changed during the attempt — the agent wrote
    # outside its workspace (or a stray process did)
    if baseline is not None:
        touched = sorted(_repo_status(repo_root) - baseline)
        if touched:
            logger.log(
                phase="delegate",
                step=step,
                event_type="executor.repo_mutation",
                model=worker.slug,
                role="harness",
                input_data={"subtask": subtask.get("id")},
                output_data={"paths": touched[:20]},
                reasoning="Repo tree changed during the agent attempt — the agent (or a stray process) wrote outside its workspace.",
            )

    # tripwire 2: transcript references the repo or task oracles — reads are
    # detected, not prevented (open filesystem is the documented boundary)
    needles = _oracle_probe_needles(result.transcript_path, repo_root, task)
    if needles:
        logger.log(
            phase="delegate",
            step=step,
            event_type="executor.oracle_probe",
            model=worker.slug,
            role="harness",
            input_data={"subtask": subtask.get("id")},
            output_data={"needles": needles},
            reasoning="Agent transcript references repo paths or task oracles — possible oracle probing.",
        )

    # the judge will read this diff — added lines shaped like instructions
    # to it are recorded as a milestone, not removed
    injection_hits = len(_INJECTION_HINT.findall(result.diff))
    if injection_hits:
        logger.log(
            phase="delegate",
            step=step,
            event_type="executor.judge_injection",
            model=worker.slug,
            role="harness",
            input_data={"subtask": subtask.get("id")},
            output_data={"hits": injection_hits},
            reasoning="Harvested diff contains lines shaped like instructions to the judge — flagged, not stripped.",
        )

    logger.log_llm_call(
        phase="delegate",
        step=step,
        model=worker.slug,
        role="worker",
        messages=[{"role": "user", "content": prompt}],
        completion={
            "executor": adapter.name,
            "changed_paths": result.changed_paths,
            "deleted_paths": result.deleted_paths,
            "hidden_paths": result.hidden_paths,
            "file_count": len(result.files),
            "exit_code": result.exit_code,
            "usage": usage or None,
            "transcript_sha256": result.transcript_sha256,
            "transcript_bytes": result.transcript_bytes,
            "redactions": result.redactions,
            "group_survivors": result.group_survivors,
            "diff_bytes": len(result.diff.encode("utf-8")),
        },
        reasoning=f"Agent CLI {adapter.name} executed subtask {subtask.get('id')} in a contained workspace.",
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        pricing_source=pricing_source,
        attempt=attempt,
    )
    cost = {
        "phase": "delegate",
        "model": worker.slug,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": cost_usd,
        "pricing_source": pricing_source,
        "usage": usage or None,
    }
    out = {
        "subtask_id": subtask.get("id"),
        "prompt": subtask.get("prompt") or subtask.get("description"),
    }
    if is_fileset:
        out.update(summarize_fileset(result.files))
    else:
        out["content"] = result.diff
    return out, result.files, result.diff, [cost]


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
    if not isinstance(plan, dict):
        raise PlanError(f"orchestrator plan must be a JSON object, got {type(plan).__name__}")
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


def _fake_file_body(path: str) -> str:
    """Deterministic dry-run file body — shaped by extension, never real code."""
    if path.endswith(".py"):
        return '"""orchestral dry-run stub."""\n'
    if path.endswith(".css"):
        return "body { margin: 0; font-family: system-ui; }\n"
    if path.endswith(".js"):
        return "console.log('dry-run');\n"
    if path.endswith(".json"):
        return '{"dry_run": true}\n'
    return f"<!doctype html>\n<title>orchestral dry-run</title>\n<!-- {path} -->\n"


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
