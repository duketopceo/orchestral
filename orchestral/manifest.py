"""Immutable run manifest — the reproducibility anchor for a benchmark run.

Written to ``manifest.json`` at run start (status ``running``) and finalized
at run end (status + ``finished_at``). Captures content hashes rather than
file paths so results stay auditable after prompts or task specs are edited:
a pairing comparison is only meaningful when both runs name the same task
content and prompt content.

See docs/tui-observability-design.md for the field contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.holdout import is_holdout
from orchestral.judge import JUDGE_PROMPT
from orchestral.planners import ORCHESTRATOR_DEFAULT_PROMPT

MANIFEST_SCHEMA_VERSION = "1"

# Base worker system prompt, kept in sync with `_build_messages` in
# planners.py — hash of this exact string is what the manifest records.
_WORKER_SYSTEM_PROMPT = "You are a worker. Execute the subtask and return the requested content."


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def task_hash(task: TaskSpec) -> str:
    """Content hash of the task spec — id/type/prompt/validation, canonical."""
    canonical = json.dumps(asdict(task), sort_keys=True, default=str)
    return _sha(canonical)


def config_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, default=str)
    return _sha(canonical)


def build_manifest(
    *,
    run_id: str,
    task: TaskSpec,
    orchestrator: ModelConfig,
    worker: ModelConfig,
    judge: ModelConfig | None,
    providers: dict[str, tuple[str, str, str]],
    env: dict[str, Any],
    config: dict[str, Any],
    planner: str,
    prompt_variant: str | None,
    dry_run: bool,
    seed: int | None,
    run_group: str | None,
    replicate: int | None,
) -> dict[str, Any]:
    """Assemble the manifest dict. Caller writes it to ``manifest.json``."""
    orch_prompt_hash = _sha(ORCHESTRATOR_DEFAULT_PROMPT)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "task_id": task.id,
        "task_version": str(task.metadata.get("version", "1")),
        "task_hash": task_hash(task),
        "task_type": task.type,
        "orchestrator_model": orchestrator.slug,
        "worker_model": worker.slug,
        "judge_model": judge.slug if judge else None,
        "orchestrator_provider": providers.get("orchestrator", (None,))[0],
        "worker_provider": providers.get("worker", (None,))[0],
        "judge_provider": providers.get("judge", (None,))[0] if judge else None,
        "harness_version": env.get("orchestral_version"),
        "git_commit": env.get("git_sha"),
        "python_version": env.get("python"),
        "platform": env.get("platform"),
        "planner": planner,
        "prompt_variant": prompt_variant,
        "orchestrator_prompt_hash": orch_prompt_hash,
        "worker_prompt_hash": _sha(_WORKER_SYSTEM_PROMPT),
        "judge_prompt_hash": _sha(JUDGE_PROMPT) if judge else None,
        "config_hash": config_hash(config),
        "concurrency": 1,
        "timeout_seconds": 120,
        "retry_policy": f"retry_limit={worker.retry_limit}",
        "seed": seed,
        "holdout": is_holdout(task),
        "run_group": run_group,
        "replicate": replicate,
        "dry_run": dry_run,
    }


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    path = run_dir / "manifest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=str))
    tmp.replace(path)


def finalize_manifest(
    run_dir: Path,
    *,
    status: str,
    passes: bool | None = None,
    score: float | None = None,
    failure_reason: str | None = None,
) -> None:
    """Set terminal fields on an existing manifest. No-op if absent."""
    path = run_dir / "manifest.json"
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    manifest["status"] = status
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    if passes is not None:
        manifest["passes"] = passes
    if score is not None:
        manifest["score"] = score
    if failure_reason is not None:
        manifest["failure_reason"] = failure_reason
    write_manifest(run_dir, manifest)
