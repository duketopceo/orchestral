"""LLM-as-judge for orchestral, ported from perplexityai/search_evals.

The judge scores the final artifact against the task and returns a score,
pass/fail, and reasoning. It uses the same OpenRouter client as the rest of
harness so it stays provider-agnostic.

`backfill_judgments` applies the judge retroactively to finished runs whose
artifacts are already on disk — the score axis without re-running the eval.
Backfilled runs keep their recorded mechanical verdict; only `score` and the
cost ledger grow, and `report.json` marks `judge_backfill: true` so the
provenance is never ambiguous.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec, find_task, load_task
from orchestral.costs import compute_cost, token_usage_from_raw
from orchestral.logger import EventLogger
from orchestral.planners import _extract_json
from orchestral.providers import Provider

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

# Decisions-engine question set — the same semantic questions the chat rubric
# asks, expressed as typed noul/score questions so answers are calibrated
# probabilities rather than free text we have to parse.
_DECISIONS_QUESTIONS: dict[str, Any] = {
    "verdict": {
        "type": "noul",
        "instructions": (
            "Does the artifact correctly and completely satisfy the task "
            "requirements? Judge correctness against the task, not surface polish."
        ),
        "true": "The artifact meets the task requirements.",
        "false": "The artifact is wrong, incomplete, or off-task.",
    },
    "quality": {
        "type": "score",
        "instructions": "Rate the artifact's overall quality for this task.",
        "criteria": ["poor", "weak", "adequate", "good", "excellent"],
    },
}


def is_decisions_model(model: ModelConfig) -> bool:
    """True for typed-decision engines (``~typesafe/jev-latest`` et al.) —
    identified by ``metadata.engine: decisions`` or the ``~``/``typesafe/``
    slug prefix, so an ad-hoc ``--judge`` slug works without a models/*.yaml."""
    meta = model.metadata or {}
    return meta.get("engine") == "decisions" or model.slug.startswith(("~", "typesafe/"))


def _judge_via_decisions(
    *,
    logger: EventLogger,
    step: int,
    task: TaskSpec,
    artifact: str,
    judge: ModelConfig,
    client: Provider,
    language: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Judge through /api/alpha/decisions — calibrated noul + rubric score.

    ``verdict`` noul >= 0.5 maps to ``passed``; the 0-4 quality rubric's
    expected value maps to ``score`` (0-1). Both probabilities and the
    engine's own confidence are preserved in the result for provenance."""
    state = {
        "task": task.prompt[:4000],
        "artifact_language": language,
        "artifact": artifact[:8000],
    }
    data = client.decide(model=judge.slug, state=state, questions=_DECISIONS_QUESTIONS)  # type: ignore[attr-defined]
    answers = data.get("answers") or {}
    verdict = answers.get("verdict") or {}
    quality = answers.get("quality") or {}
    noul = verdict.get("noul")
    raw = quality.get("score")
    score = (float(raw) / 4.0) if isinstance(raw, int | float) else None
    passed = (float(noul) >= 0.5) if isinstance(noul, int | float) else None
    usage = data.get("usage") or {}
    result: dict[str, Any] = {
        "score": score,
        "passed": passed,
        "reasoning": (
            f"jev noul={noul} rubric={raw}/4 "
            f"confidence={quality.get('confidence')} probs={quality.get('probabilities')}"
        ),
        "noul": noul,
        "confidence": quality.get("confidence"),
        "engine": "decisions",
        "model": judge.slug,
    }
    cost: dict[str, Any] = {
        "phase": "judge",
        "model": judge.slug,
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cost_usd": usage.get("cost") or 0.0,
        "pricing_source": "api",
        "usage": usage,
    }
    logger.log_llm_call(
        phase="judge",
        step=step,
        model=judge.slug,
        role="judge",
        messages=[{"role": "user", "content": json.dumps({"state": state, "questions": _DECISIONS_QUESTIONS})}],
        completion={"content": json.dumps(answers), "usage": usage, "id": data.get("id")},
        reasoning=str(result["reasoning"]),
        input_tokens=cost["input_tokens"],
        output_tokens=cost["output_tokens"],
        cost_usd=cost["cost_usd"],
        latency_ms=data.get("latency_ms") or 0.0,
        pricing_source="api",
    )
    return result, [cost]


def judge_artifact(
    *,
    logger: EventLogger,
    step: int,
    task: TaskSpec,
    artifact: str,
    judge: ModelConfig,
    client: Provider | None,
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
        else f"```{language}\n{artifact[:2000]}\n```"
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

    if is_decisions_model(judge):
        if image_bytes is not None:
            # decisions engines are text-only — no image path exists
            result = {"score": None, "passed": None,
                      "reasoning": "decisions engine cannot judge image artifacts"}
            logger.log(phase="judge", step=step, event_type="judge_skipped",
                       model=judge.slug, role="judge",
                       input_data={"task": task.id}, output_data=result,
                       reasoning="Decisions engine is text-only; image skipped.")
            return result, []
        return _judge_via_decisions(
            logger=logger, step=step, task=task, artifact=artifact,
            judge=judge, client=client, language=language,
        )

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
    result["model"] = judge.slug
    result["engine"] = "chat"

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


def _fake_judge_result() -> dict[str, Any]:
    return {"score": None, "passed": None, "reasoning": "Dry-run; no judge model was called."}


_TEXT_ARTIFACT_EXTS = {"html", "txt", "sql", "diff", "json", "md", "css"}
_IMAGE_ARTIFACT_EXTS = {"png", "jpg", "jpeg", "webp"}


class _NoJudgeableArtifact(Exception):
    """Run dir has no artifact the judge path can consume."""


def _judge_input(run_dir: Path) -> tuple[bytes | None, str | None, str]:
    """Rebuild the judge's view of a stored artifact — same shapes the live
    path produces: image bytes for image tasks, a content-free member listing
    for zips, raw text for everything else.

    Raises _NoJudgeableArtifact for missing artifacts and video (the live
    path defers video judging the same way).
    """
    artifacts = sorted(run_dir.glob("artifact.*"))
    if not artifacts:
        raise _NoJudgeableArtifact(f"no artifact.* in {run_dir}")
    path = artifacts[0]
    ext = path.suffix.lstrip(".").lower()
    if ext in _IMAGE_ARTIFACT_EXTS:
        return path.read_bytes(), None, "html"
    if ext == "mp4":
        raise _NoJudgeableArtifact("video judging deferred — no video-input judge path")
    if ext == "zip":
        with zipfile.ZipFile(path) as zf:
            listing = "\n".join(f"{i.filename} ({i.file_size} bytes)" for i in zf.infolist())
        return None, listing, "text"
    if ext in _TEXT_ARTIFACT_EXTS:
        return None, path.read_text(encoding="utf-8", errors="replace"), "html"
    raise _NoJudgeableArtifact(f"unjudgeable artifact type: {path.name}")


_SPEC_AUDIT_QUESTIONS: dict[str, Any] = {
    "lowballs": {
        "type": "noul",
        "instructions": (
            "Would this task UNDER-TEST the capability it claims to measure — "
            "i.e. could a weak model pass it with shallow or lucky output? "
            "Consider fixture adversariality, ambiguity, and whether the "
            "ground truth can be gamed."
        ),
        "true": "Yes — the task is too weak to discriminate real capability.",
        "false": "No — the task meaningfully discriminates capability.",
    },
    "sound": {
        "type": "noul",
        "instructions": "Is the task spec unambiguous — one clear correct behavior a competent model can identify?",
        "true": "The spec is clear and unambiguous.",
        "false": "The spec is ambiguous or self-contradictory.",
    },
    "difficulty": {
        "type": "score",
        "instructions": "How difficult is this task for a mid-tier code-capable LLM?",
        "criteria": ["trivial", "easy", "moderate", "hard", "expert"],
    },
    "adversarial": {
        "type": "score",
        "instructions": (
            "How adversarial is the fixture — does it contain traps that make "
            "plausible-looking wrong answers diverge from the reference?"
        ),
        "criteria": ["none", "light", "moderate", "strong", "brutal"],
    },
}


def audit_specs(
    *,
    specs: list[TaskSpec],
    judge: ModelConfig,
    client: Provider | None,
    dry_run: bool,
    workers: int = 8,
) -> list[dict[str, Any]]:
    """Ask a decisions-engine judge to audit our own task specs.

    The meta-question every benchmark owes an answer to: are these tasks
    actually discriminating, or do they low-ball the capability they claim
    to measure? Requires a decisions model — chat judges answer this kind
    of structured audit unreliably. Returns one row per spec."""
    if not is_decisions_model(judge):
        raise ValueError("spec audit requires a decisions-engine judge (e.g. '~typesafe/jev-latest')")
    if dry_run:
        return [
            {"task_id": t.id, "type": t.type, "skipped": "dry-run"}
            for t in specs
        ]
    if client is None:
        raise ValueError("spec audit requires a provider client (unless --dry-run)")

    def audit_one(t: TaskSpec) -> dict[str, Any]:
        state = {
            "task_id": t.id,
            "type": t.type,
            "prompt": t.prompt[:4000],
            "validation": t.validation,
            "metadata_keys": sorted((t.metadata or {}).keys()),
            "seed_rows": len((t.metadata or {}).get("seed") or []),
            "has_reference": bool((t.metadata or {}).get("reference_sql") or (t.metadata or {}).get("tests") or (t.metadata or {}).get("expected_answer")),
        }
        try:
            data = client.decide(model=judge.slug, state=state, questions=_SPEC_AUDIT_QUESTIONS)  # type: ignore[attr-defined]
        except Exception as exc:
            return {"task_id": t.id, "type": t.type, "error": str(exc)[:160]}
        a = data.get("answers") or {}
        return {
            "task_id": t.id,
            "type": t.type,
            "lowballs": (a.get("lowballs") or {}).get("noul"),
            "sound": (a.get("sound") or {}).get("noul"),
            "difficulty": (a.get("difficulty") or {}).get("score"),
            "adversarial": (a.get("adversarial") or {}).get("score"),
            "confidence": (a.get("difficulty") or {}).get("confidence"),
            "cost_usd": (data.get("usage") or {}).get("cost"),
        }

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(audit_one, specs))


_CLAIM_AUDIT_QUESTIONS: dict[str, Any] = {
    "supported": {
        "type": "noul",
        "instructions": (
            "Given ONLY the stated evidence, is the claim supported as written? "
            "Do not credit vibes, intent, or adjacent truths — rule on the "
            "claim as literally stated against the evidence shown."
        ),
        "true": "The evidence supports the claim as stated.",
        "false": "The evidence does not support the claim, or the claim overreaches.",
    },
    "fatal_flaw": {
        "type": "noul",
        "instructions": (
            "Does the methodology or evidence contain a flaw that would "
            "invalidate this claim even if the numbers are accurate — e.g. "
            "confounded variables, conflated metrics, tiny n, survivorship, "
            "contamination, or the evidence measuring something else?"
        ),
        "true": "Yes — a methodological flaw undermines the claim.",
        "false": "No fatal flaw found in the stated methodology.",
    },
    "severity": {
        "type": "score",
        "instructions": (
            "If a flaw exists, how severe is it for THIS claim specifically — "
            "does it change the conclusion, or just qualify it?"
        ),
        "criteria": ["cosmetic", "minor", "material", "severe", "fatal"],
    },
    "strength": {
        "type": "score",
        "instructions": "How strong is the evidence for this claim, ignoring whether you agree with it?",
        "criteria": ["anecdotal", "weak", "suggestive", "strong", "conclusive"],
    },
}


def audit_claims(
    *,
    claims: list[dict[str, Any]],
    judge: ModelConfig,
    client: Provider | None,
    dry_run: bool,
    workers: int = 4,
) -> list[dict[str, Any]]:
    """Point a decisions engine at our own claims, evidence attached.

    The 'scorch our ideas' battery: each claim is a dict with an id, a
    statement, and an evidence object (real stats — never vibes). jev rules
    on support, fatal flaws, and evidentiary strength. Cheap enough to run
    on every published result."""
    if not is_decisions_model(judge):
        raise ValueError("claims audit requires a decisions-engine judge (e.g. '~typesafe/jev-latest')")
    if dry_run:
        return [{"claim_id": c.get("id", "?"), "skipped": "dry-run"} for c in claims]
    if client is None:
        raise ValueError("claims audit requires a provider client (unless --dry-run)")

    def audit_one(c: dict[str, Any]) -> dict[str, Any]:
        state = {
            "claim_id": c.get("id"),
            "claim": c.get("statement"),
            "context": c.get("context", ""),
            "evidence": c.get("evidence", {}),
            "caveats_already_known": c.get("caveats", []),
        }
        try:
            data = client.decide(model=judge.slug, state=state, questions=_CLAIM_AUDIT_QUESTIONS)  # type: ignore[attr-defined]
        except Exception as exc:
            return {"claim_id": c.get("id"), "error": str(exc)[:160]}
        a = data.get("answers") or {}
        return {
            "claim_id": c.get("id"),
            "statement": (c.get("statement") or "")[:120],
            "supported": (a.get("supported") or {}).get("noul"),
            "fatal_flaw": (a.get("fatal_flaw") or {}).get("noul"),
            "severity": (a.get("severity") or {}).get("score"),
            "strength": (a.get("strength") or {}).get("score"),
            "confidence": (a.get("supported") or {}).get("confidence"),
            "cost_usd": (data.get("usage") or {}).get("cost"),
        }

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(audit_one, claims))


def backfill_judgments(
    store: Any,
    judge: ModelConfig,
    client: Provider | None,
    *,
    run_group: str | None = None,
    task_id: str | None = None,
    orchestrator: str | None = None,
    worker: str | None = None,
    limit: int | None = None,
    jobs: int = 4,
    dry_run: bool = False,
    force: bool = False,
    tasks_dir: Path | str = "tasks",
) -> dict[str, Any]:
    """Judge the artifacts of finished runs retroactively.

    Writes `judge` + `judge_backfill` into report.json, sets the index score,
    and adds judge cost to the run's recorded total — the same fields a
    natively judged run carries, minus the `passed` gate (the mechanical
    verdict recorded at run time stands).
    """
    metas = [
        m for m in store.list_runs(
            orchestrator=orchestrator, worker=worker,
            task_id=task_id, run_group=run_group,
        )
        if m.status == "finished"
    ]
    spec_cache: dict[str, TaskSpec] = {}

    def _spec(tid: str) -> TaskSpec:
        if tid not in spec_cache:
            path = find_task(tid, tasks_dir)
            if path is None:
                raise FileNotFoundError(f"task spec not found: {tid}")
            spec_cache[tid] = load_task(path)
        return spec_cache[tid]

    def _already_judged(run_dir: Path) -> bool:
        report_path = run_dir / "report.json"
        if not report_path.exists():
            return False
        try:
            return json.loads(report_path.read_text()).get("judge") is not None
        except Exception:
            return False

    def _one(meta: Any) -> dict[str, Any]:
        run_dir = Path(meta.run_dir)
        if not force and _already_judged(run_dir):
            return {"run_id": meta.run_id, "skipped": "already judged"}
        try:
            image_bytes, text, language = _judge_input(run_dir)
        except _NoJudgeableArtifact as exc:
            return {"run_id": meta.run_id, "skipped": str(exc)}
        try:
            task = _spec(meta.task_id)
        except Exception as exc:
            return {"run_id": meta.run_id, "skipped": f"spec: {exc}"}
        logger = EventLogger(run_dir, store=store, run_id=meta.run_id, dry_run=dry_run)
        try:
            payload = image_bytes if image_bytes is not None else (text or "").encode()
            sha = hashlib.sha256(task.prompt.encode() + b"\0" + payload).hexdigest()
            with store.judge_lock((task.id, judge.slug, sha)):
                cached = None if dry_run else store.get_judge_result(task.id, judge.slug, sha)
                result: dict[str, Any]
                costs: list[dict[str, Any]]
                if cached is not None:
                    result, costs = cached, []
                else:
                    result, costs = judge_artifact(
                        logger=logger, step=0, task=task,
                        artifact=text or "", judge=judge, client=client,
                        dry_run=dry_run, image_bytes=image_bytes, language=language,
                    )
                    if not dry_run and not result.get("parse_failed"):
                        store.put_judge_result(task.id, judge.slug, sha, result)
            if dry_run:
                return {"run_id": meta.run_id, "judged": "dry-run", "score": result.get("score")}
            report_path = run_dir / "report.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            report["judge"] = result
            report["judge_backfill"] = True
            report_path.write_text(json.dumps(report, indent=2, default=str))
            if result.get("score") is not None:
                meta.score = float(result["score"])
            meta.total_cost_usd += sum(c.get("cost_usd") or 0.0 for c in costs)
            store.update_meta(meta)
            return {"run_id": meta.run_id, "judged": judge.slug, "score": result.get("score"),
                    "passed": result.get("passed")}
        finally:
            logger.close()

    results: list[dict[str, Any]] = []
    targets = metas[:limit] if limit else metas
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for r in pool.map(_one, targets):
            results.append(r)

    judged = [r for r in results if "judged" in r and r["judged"] != "dry-run"]
    return {
        "runs_seen": len(targets),
        "judged": len(judged),
        "skipped": len(results) - len(judged) - sum(1 for r in results if r.get("judged") == "dry-run"),
        "dry_run_judged": sum(1 for r in results if r.get("judged") == "dry-run"),
        "judge": judge.slug,
        "results": results,
    }
