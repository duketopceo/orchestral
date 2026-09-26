"""Orchestrator -> worker -> assemble -> validate loop with full logging."""

from __future__ import annotations

import contextlib
import hashlib
import html.parser
import io
import json
import platform
import re
import subprocess
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.apistub import check_api
from orchestral.audit import VALIDATION_CHECKS
from orchestral.codeexec import (
    DEFAULT_TIMEOUT_SECONDS,
    check_code_quality,
    materialize,
    run_unittest_suite,
    score_from_report,
)
from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import CostLedger
from orchestral.extract import check_extraction
from orchestral.fileset import (
    build_zip,
    expected_paths,
    manifest_listing,
    merge_filesets,
)
from orchestral.judge import judge_artifact
from orchestral.logger import EventLogger
from orchestral.manifest import build_manifest, finalize_manifest, write_manifest
from orchestral.metrics import build_metrics
from orchestral.openrouter import OpenRouterVideoSubmittedError
from orchestral.planners import (
    assemble_ce,
    assemble_media,
    assemble_raw,
    delegate,
    delegate_api,
    delegate_constraint,
    delegate_extract,
    delegate_image,
    delegate_multi,
    delegate_needle,
    delegate_sql,
    delegate_video,
    plan_ce,
    plan_raw,
)
from orchestral.providers import provider_for, provider_key
from orchestral.sqlexec import run_sql_check
from orchestral.storage import RunMeta, RunStore
from orchestral.taxonomy import classify_exception


class ValidationError(Exception):
    """Raised when a run fails structural validation."""


class RunCancelled(Exception):
    """Raised when the run's cancel_event is set between steps."""


class Runner:
    def __init__(
        self,
        *,
        dry_run: bool = False,
        planner: str = "raw",
        runs_dir: str | Path = "runs",
        prompt_variant: str | None = None,
        sweep: dict[str, Any] | None = None,
        use_judge_cache: bool = True,
        store: RunStore | None = None,
        clients: dict[str, Any] | None = None,
        run_group: str | None = None,
        replicate: int | None = None,
        seed: int | None = None,
        verbose: bool = False,
        cancel_event: threading.Event | None = None,
        on_run_created: Any = None,
    ):
        self.dry_run = dry_run
        self.planner = planner
        self.prompt_variant = prompt_variant
        self.sweep = sweep
        self.run_group = run_group
        self.replicate = replicate
        self.seed = seed
        self.verbose = verbose
        self.cancel_event = cancel_event
        # called with run_id as soon as the run dir exists — lets a caller
        # (e.g. the TUI) map a job to its in-flight run before run() returns
        self.on_run_created = on_run_created
        self.use_judge_cache = use_judge_cache
        self.store = store or RunStore(runs_dir)
        # role ("orchestrator"/"worker"/"judge") -> Provider, injected for tests
        self._injected_clients = dict(clients or {})
        # legacy single-client handle; tests may set this directly
        self.client: Any = None
        self._owned_clients: list[Any] = []

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RunCancelled("cancelled by user")

    def _resolve_clients(
        self,
        orchestrator: ModelConfig,
        worker: ModelConfig,
        judge: ModelConfig | None,
    ) -> dict[str, Any]:
        """Map each role to a Provider, deduplicating by provider config.

        Roles sharing a provider config share one client; injected clients win
        over resolution. Returns {} for dry runs (call sites pass None).
        """
        if self.dry_run:
            return {}
        resolved: dict[str, Any] = {}
        cache: dict[tuple[str, str, str], Any] = {}
        for role, model in (("orchestrator", orchestrator), ("worker", worker), ("judge", judge)):
            if model is None:
                continue
            if role in self._injected_clients:
                resolved[role] = self._injected_clients[role]
                continue
            key = provider_key(model)
            if key not in cache:
                try:
                    cache[key] = provider_for(model)
                except Exception:
                    for c in cache.values():
                        c.close()
                    raise
            resolved[role] = cache[key]
        self._owned_clients = list(cache.values())
        return resolved

    def run(self, task: TaskSpec, orchestrator: ModelConfig, worker: ModelConfig, judge: ModelConfig | None = None) -> RunMeta:
        # Resolve providers before the run dir exists — a bad provider config
        # fails fast instead of leaving a run stuck at "running". Pre-run
        # failures have no run dir to log to, so they land in the store's
        # root-level debug.jsonl instead.
        try:
            role_clients = self._resolve_clients(orchestrator, worker, judge)
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.store.debug_log(
                    "providers", "client resolution failed",
                    error=str(exc), error_type=type(exc).__name__,
                    error_category=classify_exception(exc),
                    task=task.id, orchestrator=orchestrator.slug, worker=worker.slug,
                )
            raise
        providers = {
            role: provider_key(model)
            for role, model in (("orchestrator", orchestrator), ("worker", worker), ("judge", judge))
            if model is not None
        }
        env = _environment()
        run_config = {
            "dry_run": self.dry_run,
            "planner": self.planner,
            "prompt_variant": self.prompt_variant,
            "sweep": self.sweep,
            "judge": judge.slug if judge else None,
            "run_group": self.run_group,
            "replicate": self.replicate,
            "seed": self.seed,
            "orchestrator": orchestrator.to_dict(),
            "worker": worker.to_dict(),
        }
        try:
            run_id, run_dir = self.store.new_run(
                orchestrator.slug,
                task.id,
                worker.slug,
                run_group=self.run_group,
                replicate=self.replicate,
                env=env,
                config=run_config,
            )
            logger = EventLogger(
                run_dir, store=self.store, run_id=run_id,
                dry_run=self.dry_run, verbose=self.verbose,
            )
            # wire the client's debug sink (http retries, video polls) to debug.jsonl
            for c in role_clients.values():
                with contextlib.suppress(AttributeError):
                    c.debug = logger.log_debug
        except Exception:
            # resolved clients are live httpx.Clients — don't leak them when
            # run-dir/logger construction fails before the main try/finally
            for c in self._owned_clients:
                with contextlib.suppress(Exception):
                    c.close()
            raise
        manifest = build_manifest(
            run_id=run_id, task=task, orchestrator=orchestrator,
            worker=worker, judge=judge, providers=providers, env=env,
            config=run_config, planner=self.planner,
            prompt_variant=self.prompt_variant, dry_run=self.dry_run,
            seed=self.seed, run_group=self.run_group, replicate=self.replicate,
        )
        write_manifest(run_dir, manifest)
        if self.on_run_created is not None:
            self.on_run_created(run_id)
        logger.lifecycle("run.created", phase="init", run_id=run_id, dry_run=self.dry_run)
        logger.lifecycle(
            "task.loaded", phase="init", task_id=task.id, task_type=task.type,
            task_hash=manifest["task_hash"],
        )
        logger.log(
            phase="init",
            step=0,
            event_type="run.started",
            model="",
            role="harness",
            input_data={
                "task": task.id,
                "orchestrator": orchestrator.slug,
                "worker": worker.slug,
                "planner": self.planner,
            },
            output_data={
                "run_id": run_id,
                "dry_run": self.dry_run,
                "providers": {
                    role: {"provider": p, "base_url": u, "api_key_env": e}
                    for role, (p, u, e) in providers.items()
                },
                "env": env,
            },
            reasoning="Initialised run directory, SQLite index, and event log.",
        )

        ledger = CostLedger()
        meta: RunMeta | None = None
        t0 = time.perf_counter()

        try:
            self._check_cancelled()
            # 1. Plan
            logger.lifecycle("orchestrator.started", phase="plan", role="orchestrator")
            if self.planner == "ce-plan":
                plan, plan_costs = plan_ce(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=1,
                    client=role_clients.get("orchestrator"),
                    dry_run=self.dry_run,
                    prompt_variant=self.prompt_variant,
                )
            else:
                plan, plan_costs = plan_raw(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=1,
                    client=role_clients.get("orchestrator"),
                    dry_run=self.dry_run,
                    prompt_variant=self.prompt_variant,
                )
            (run_dir / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
            ledger.add_many(plan_costs)
            logger.lifecycle(
                "orchestrator.completed", phase="plan", role="orchestrator",
                planner=self.planner,
                cost_usd=sum(float(c.get("usd", 0.0)) for c in plan_costs if isinstance(c, dict)),
            )

            # 2. Delegate each subtask to the worker
            is_image = task.type == "image"
            is_video = task.type == "video"
            # code tasks share the fileset protocol: workers return files,
            # the merge+zip is identical, only validation differs
            is_multi = task.type in ("multi-file", "code")
            is_constraint = task.type == "constraint"
            is_needle = task.type == "needle"
            is_sql = task.type == "sql"
            is_extract = task.type == "extract"
            is_api = task.type == "api"
            is_media = is_image or is_video
            results: list[dict[str, Any]] = []
            media_paths: list[Path | None] = []
            file_sets: list[tuple[int, dict[str, str]]] = []
            media_ext = _artifact_ext(task.type)
            subtasks = plan.get("subtasks") or plan.get("sections", {}).get("subtasks", [])
            if not subtasks:
                # Raw/none planners may emit no subtasks; every task type's
                # delegate paths are single-shot over one subtask (extract,
                # sql, api, needle, constraint, media, multi). With zero
                # subtasks the loop would run zero times and the assembly
                # branches would write empty artifacts — fall back to one
                # default subtask so the worker still runs.
                subtasks = [{"id": "s0", "description": task.prompt}]
            logger.lifecycle(
                "delegation.created", phase="delegate",
                subtasks=len(subtasks),
                subtask_ids=[(s.get("id") if isinstance(s, dict) else i) for i, s in enumerate(subtasks)],
            )
            for i, sub in enumerate(subtasks):
                self._check_cancelled()
                # models sometimes return a list of strings; normalize to dicts
                if isinstance(sub, str):
                    sub = {"id": i, "description": sub}
                elif not isinstance(sub, dict):
                    sub = {"id": i, "description": str(sub)}
                wid = f"worker-{i}"
                out: dict[str, Any] | None = None
                media_bytes: bytes = b""
                files: dict[str, str] = {}
                attempts = max(1, worker.retry_limit + 1)
                logger.lifecycle(
                    "worker.started", phase="delegate", role="worker",
                    worker_id=wid, subtask_id=sub.get("id", i),
                    description=str(sub.get("description", ""))[:200],
                )
                for attempt in range(attempts):
                    try:
                        if is_image:
                            out, media_bytes, worker_costs = delegate_image(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        elif is_video:
                            out, media_bytes, worker_costs = delegate_video(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                                seed=self.seed,
                            )
                        elif is_sql:
                            out, worker_costs = delegate_sql(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        elif is_extract:
                            out, worker_costs = delegate_extract(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        elif is_api:
                            out, worker_costs = delegate_api(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        elif is_multi:
                            out, files, worker_costs = delegate_multi(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        elif is_needle:
                            out, worker_costs = delegate_needle(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        elif is_constraint:
                            out, worker_costs = delegate_constraint(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        else:
                            out, worker_costs = delegate(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                            )
                        ledger.add_many(worker_costs)
                    except Exception as exc:
                        out = None
                        cat = classify_exception(exc)
                        logger.log(
                            phase="delegate",
                            step=i + 3,
                            event_type="worker_error",
                            model=worker.slug,
                            role="worker",
                            worker_id=wid,
                            input_data={"subtask": sub},
                            output_data={"attempt": attempt + 1, "max_attempts": attempts},
                            reasoning=f"Worker call raised an exception on attempt {attempt + 1}.",
                            error=str(exc),
                            metadata={"error_category": cat, "subtask_id": sub.get("id", i)},
                        )
                        # Any post-submission video failure (terminal status,
                        # poll exhaustion, timeout, unsafe URL, download) is
                        # unrecoverable by retry — resubmitting bills a new job.
                        will_retry = not (
                            isinstance(exc, OpenRouterVideoSubmittedError) or attempt + 1 >= attempts
                        )
                        logger.log_debug(
                            "delegate", "worker call failed",
                            subtask_id=sub.get("id", i), attempt=attempt + 1,
                            error_type=type(exc).__name__, error_category=cat,
                            will_retry=will_retry,
                        )
                        if not will_retry:
                            logger.lifecycle(
                                "worker.failed", phase="delegate", role="worker",
                                worker_id=wid, subtask_id=sub.get("id", i),
                                attempts=attempt + 1, error_category=cat,
                            )
                            raise
                        logger.lifecycle(
                            "worker.progress", phase="delegate", role="worker",
                            worker_id=wid, subtask_id=sub.get("id", i),
                            attempt=attempt + 1, reason="error_retry",
                        )
                        continue
                    if _subtask_produced_output(out, media_bytes, files, is_media, is_multi):
                        break
                    if attempt + 1 < attempts:
                        # a retry will actually follow — log it as such
                        logger.log(
                            phase="delegate",
                            step=i + 3,
                            event_type="worker_retry",
                            model=worker.slug,
                            role="worker",
                            worker_id=wid,
                            input_data={"subtask": sub},
                            output_data={"attempt": attempt + 1, "max_attempts": attempts},
                            reasoning=f"Worker returned empty output on attempt {attempt + 1}; retrying.",
                            metadata={"subtask_id": sub.get("id", i)},
                        )
                        logger.lifecycle(
                            "worker.progress", phase="delegate", role="worker",
                            worker_id=wid, subtask_id=sub.get("id", i),
                            attempt=attempt + 1, reason="empty_output_retry",
                        )
                        logger.log_debug(
                            "delegate", "empty worker output; retrying",
                            subtask_id=sub.get("id", i), attempt=attempt + 1,
                        )
                if not out:  # None or exhausted-empty dict must not reach assembly
                    logger.lifecycle(
                        "worker.failed", phase="delegate", role="worker",
                        worker_id=wid, subtask_id=sub.get("id", i),
                        attempts=attempts, error_category="empty_output",
                    )
                    raise ValidationError(f"Worker {worker.slug} produced no output for subtask {sub.get('id')}")
                out["attempts"] = attempt + 1
                logger.lifecycle(
                    "worker.completed", phase="delegate", role="worker",
                    worker_id=wid, subtask_id=sub.get("id", i),
                    attempts=attempt + 1,
                )
                results.append(out)
                (run_dir / f"worker-{i}.json").write_text(json.dumps(out, indent=2, default=str))
                if is_multi:
                    file_sets.append((sub.get("id", i), files))
                if is_media and media_bytes:
                    media_path = run_dir / f"worker-{i}{media_ext}"
                    media_path.write_bytes(media_bytes)
                    media_paths.append(media_path)
                else:
                    media_paths.append(None)

            # 3. Assemble final artifact
            assembly_step = 3 + len(subtasks)
            logger.lifecycle("synthesis.started", phase="assemble", role="orchestrator")
            judge_bytes: bytes | None = None
            judge_text: str | None = None
            if is_media:
                if any(p is not None for p in media_paths):
                    position, assembly_costs = assemble_media(
                        logger=logger,
                        task=task,
                        orchestrator=orchestrator,
                        step=assembly_step,
                        results=results,
                        client=role_clients.get("orchestrator"),
                        dry_run=self.dry_run,
                    )
                else:
                    position, assembly_costs = 0, []
                ledger.add_many(assembly_costs)
                selected = media_paths[position] if 0 <= position < len(media_paths) else None
                if selected is None:
                    # orchestrator picked a subtask with no media; fall back to first captured
                    selected = next((p for p in media_paths if p is not None), None)
                artifact_bytes = selected.read_bytes() if selected else b""
                (run_dir / f"artifact{media_ext}").write_bytes(artifact_bytes)
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path=f"artifact{media_ext}", bytes=len(artifact_bytes),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                if is_image:
                    passes, report = self._validate_image(task, artifact_bytes)
                    judge_bytes = artifact_bytes
                else:
                    passes, report = self._validate_video(task, artifact_bytes)
            elif is_needle:
                # Same raw-text candidate pick as constraint; validation is
                # the constraint checks (expected token present, decoys absent).
                position, assembly_costs = assemble_media(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=assembly_step,
                    results=results,
                    client=role_clients.get("orchestrator"),
                    dry_run=self.dry_run,
                )
                ledger.add_many(assembly_costs)
                chosen = results[position] if results else {}
                artifact = str(chosen.get("content") or "")
                (run_dir / "artifact.txt").write_text(artifact, encoding="utf-8")
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.txt", bytes=len(artifact.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = self._validate(task, artifact)
                judge_text = artifact
            elif is_constraint:
                # Orchestrator picks the best candidate; the artifact is the
                # raw text so word budgets and pattern checks apply to worker
                # output, not HTML scaffolding.
                position, assembly_costs = assemble_media(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=assembly_step,
                    results=results,
                    client=role_clients.get("orchestrator"),
                    dry_run=self.dry_run,
                )
                ledger.add_many(assembly_costs)
                chosen = results[position] if results else {}
                artifact = str(chosen.get("content") or "")
                (run_dir / "artifact.txt").write_text(artifact, encoding="utf-8")
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.txt", bytes=len(artifact.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = self._validate(task, artifact)
                judge_text = artifact
            elif is_sql:
                # Orchestrator picks the candidate query; validation runs it
                # read-only against the task fixture's reference result.
                position, assembly_costs = assemble_media(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=assembly_step,
                    results=results,
                    client=role_clients.get("orchestrator"),
                    dry_run=self.dry_run,
                )
                ledger.add_many(assembly_costs)
                chosen = results[position] if results else {}
                candidate_sql = str(chosen.get("query") or "")
                (run_dir / "artifact.sql").write_text(candidate_sql, encoding="utf-8")
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.sql", bytes=len(candidate_sql.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = self._validate_sql(task, candidate_sql)
                judge_text = candidate_sql
            elif is_extract:
                # Orchestrator picks the best candidate extraction; grading is
                # deterministic per-field against metadata.expected.
                position, assembly_costs = assemble_media(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=assembly_step,
                    results=results,
                    client=role_clients.get("orchestrator"),
                    dry_run=self.dry_run,
                )
                ledger.add_many(assembly_costs)
                chosen = results[position] if results else {}
                artifact = str(chosen.get("content") or "")
                (run_dir / "artifact.json").write_text(artifact, encoding="utf-8")
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.json", bytes=len(artifact.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = self._validate_extract(task, artifact)
                judge_text = artifact
            elif is_api:
                # Orchestrator picks the best candidate plan; validation
                # replays it over real loopback HTTP against the task's stub.
                position, assembly_costs = assemble_media(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=assembly_step,
                    results=results,
                    client=role_clients.get("orchestrator"),
                    dry_run=self.dry_run,
                )
                ledger.add_many(assembly_costs)
                chosen = results[position] if results else {}
                plan_text = str(chosen.get("content") or "")
                (run_dir / "artifact.json").write_text(plan_text, encoding="utf-8")
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.json", bytes=len(plan_text.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = self._validate_api(task, plan_text)
                judge_text = plan_text
            elif is_multi:
                # Deterministic merge + zip: no orchestrator call, and the
                # artifact is bytes — the HTML branch below writes text.
                merged, conflicts = merge_filesets(file_sets)
                if not merged:
                    raise ValidationError("No files were produced for the multi-file task")
                artifact_bytes = build_zip(merged)
                (run_dir / "artifact.zip").write_bytes(artifact_bytes)
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.zip", bytes=len(artifact_bytes),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                if task.type == "code":
                    passes, report = self._validate_code(task, merged)
                else:
                    passes, report = self._validate_multi(task, artifact_bytes)
                report["files"] = sorted(merged)
                report["merge_conflicts"] = conflicts
                # the judge sees a content-free listing — file bodies never
                # enter the judge prompt, events, or report
                judge_text = manifest_listing(merged)
            else:
                if self.planner == "ce-plan":
                    artifact, assembly_costs = assemble_ce(
                        logger=logger,
                        task=task,
                        orchestrator=orchestrator,
                        step=assembly_step,
                        results=results,
                        client=role_clients.get("orchestrator"),
                        dry_run=self.dry_run,
                    )
                else:
                    artifact, assembly_costs = assemble_raw(
                        logger=logger,
                        task=task,
                        orchestrator=orchestrator,
                        step=assembly_step,
                        results=results,
                        client=role_clients.get("orchestrator"),
                        dry_run=self.dry_run,
                    )
                ext = _artifact_ext(task.type)
                (run_dir / f"artifact{ext}").write_text(artifact)
                ledger.add_many(assembly_costs)
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path=f"artifact{ext}", bytes=len(artifact.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = self._validate(task, artifact)
                judge_text = artifact

            # 4. Judge (optional; image tasks need a vision-capable judge model;
            # video judging is deferred — there is no video-input judge path yet)
            if judge is not None and is_video:
                logger.log(
                    phase="judge",
                    step=assembly_step + 2,
                    event_type="judge_skipped",
                    model=judge.slug,
                    role="judge",
                    input_data={"task": task.id},
                    output_data={"reason": "video judging deferred — no video-input judge path"},
                    reasoning="Judge skipped: video artifacts cannot be judged by the current judge path.",
                )
            elif judge is not None:
                if is_image and judge.metadata.get("vision") is not True:
                    logger.log(
                        phase="judge",
                        step=assembly_step + 2,
                        event_type="judge_warning",
                        model=judge.slug,
                        role="judge",
                        input_data={"task": task.id},
                        output_data={},
                        reasoning="Judge model has no `vision: true` metadata; image judging may fail at the API.",
                    )
                judge_result, judge_costs = self._judge_with_cache(
                    logger=logger,
                    step=assembly_step + 3,
                    task=task,
                    client=role_clients.get("judge"),
                    artifact_bytes=judge_bytes,
                    artifact_text=judge_text,
                    judge=judge,
                    language="text" if is_multi else "html",
                )
                ledger.add_many(judge_costs)
                report["judge"] = judge_result
                code_execution_pending = (
                    task.type == "code" and report.get("execution", {}).get("executed") is not True
                )
                if judge_result.get("score") is not None and not code_execution_pending:
                    report["score"] = judge_result["score"]
                if judge_result.get("passed") is not None:
                    passes = passes and judge_result["passed"]

            logger.lifecycle(
                "evaluation.completed", phase="validate", role="judge" if judge else "harness",
                passes=passes, score=report.get("score"),
                checks=report.get("checks") or {},
            )
            (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))

            # 5. Screenshot for HTML artifacts (optional, degrades cleanly)
            if task.type == "html":
                artifact_path = run_dir / "artifact.html"
                if artifact_path.exists():
                    try:
                        from orchestral.shots import ScreenshotUnavailable, capture_html
                    except ImportError:
                        pass  # shots extras not installed — screenshot degrades cleanly
                    else:
                        try:
                            capture_html(artifact_path, run_dir / "screenshot.png")
                        except ScreenshotUnavailable as exc:
                            logger.log(
                                phase="shots",
                                step=assembly_step + 5,
                                event_type="screenshot_skipped",
                                model="",
                                role="harness",
                                input_data={"artifact": str(artifact_path)},
                                output_data={"reason": str(exc)},
                                reasoning="Playwright or browser binaries unavailable; screenshot skipped.",
                            )

            # 6. Final accounting
            total_cost = ledger.total_cost_usd()
            total_input = ledger.total_input_tokens()
            total_output = ledger.total_output_tokens()
            (run_dir / "cost.json").write_text(json.dumps(ledger.to_breakdown(), indent=2, default=str))
            logger.lifecycle(
                "usage.recorded", phase="end",
                total_cost_usd=total_cost, input_tokens=total_input,
                output_tokens=total_output,
            )

            meta = self.store.get_run(run_id)
            assert meta is not None
            meta.status = "finished"
            meta.finished_at = datetime.now(UTC).isoformat()
            meta.total_cost_usd = total_cost
            meta.total_input_tokens = total_input
            meta.total_output_tokens = total_output
            meta.passes = passes
            meta.score = report.get("score")
            meta.latency_ms = (time.perf_counter() - t0) * 1000
            if passes is False:
                # distinguish a judge rejection of a structurally-valid
                # artifact from a failed structural check
                checks = report.get("checks") or {}
                judge_res = report.get("judge") or {}
                meta.failure_reason = (
                    "judge"
                    if all(checks.values()) and judge_res.get("passed") is False
                    else "validation"
                )
            self.store.update_meta(meta)

            logger.log(
                phase="end",
                step=assembly_step + 4,
                event_type="run.completed",
                model="",
                role="harness",
                input_data={"total_cost": total_cost, "total_tokens": total_input + total_output},
                output_data={"status": "finished", "passes": passes, "score": meta.score},
                reasoning=f"Run finished. Cost ${total_cost:.4f}, tokens {total_input + total_output}, passes={passes}.",
            )
            with contextlib.suppress(Exception):
                finalize_manifest(
                    run_dir, status="passed" if passes else "failed",
                    passes=passes, score=meta.score,
                    failure_reason=meta.failure_reason,
                )
            _write_metrics(run_dir)  # after run.completed so the event is counted

            return meta

        except RunCancelled:
            # cancel is a normal outcome, not an error — mark and return
            with contextlib.suppress(Exception):
                meta = self.store.get_run(run_id)
                if meta is not None:
                    meta.status = "cancelled"
                    meta.finished_at = datetime.now(UTC).isoformat()
                    meta.latency_ms = (time.perf_counter() - t0) * 1000
                    meta.failure_reason = "cancelled"
                    self.store.update_meta(meta)
            with contextlib.suppress(Exception):
                logger.lifecycle("run.cancelled", phase="end", status="cancelled")
            with contextlib.suppress(Exception):
                finalize_manifest(run_dir, status="cancelled", failure_reason="cancelled")
            _write_metrics(run_dir)
            return self.store.get_run(run_id) or meta or RunMeta(
                run_id=run_id, orchestrator=orchestrator.slug, task_id=task.id,
                worker=worker.slug, status="cancelled",
                started_at="", run_dir=str(run_dir),
            )

        except Exception as exc:
            cat = classify_exception(exc)
            # bookkeeping must never mask the real exception — a locked index
            # or dead log handle inside the handler would otherwise replace it
            with contextlib.suppress(Exception):
                meta = self.store.get_run(run_id)
                if meta is not None:
                    meta.status = "failed"
                    meta.finished_at = datetime.now(UTC).isoformat()
                    meta.latency_ms = (time.perf_counter() - t0) * 1000
                    meta.failure_reason = f"exception:{cat}"
                    self.store.update_meta(meta)
            with contextlib.suppress(Exception):
                logger.log(
                    phase="end",
                    step=-1,
                    event_type="run.failed",
                    model="",
                    role="harness",
                    input_data={},
                    output_data={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "error_category": cat,
                    },
                    reasoning="Run failed with an exception.",
                    error=str(exc),
                    metadata={"error_category": cat},
                )
            with contextlib.suppress(Exception):
                finalize_manifest(run_dir, status="failed", failure_reason=f"exception:{cat}")
            _write_metrics(run_dir)
            raise
        finally:
            logger.close()
            for c in self._owned_clients:
                c.close()
            if self.client is not None:
                self.client.close()

    def _judge_with_cache(
        self,
        *,
        logger: EventLogger,
        step: int,
        task: TaskSpec,
        artifact_bytes: bytes | None,
        artifact_text: str | None,
        judge: ModelConfig,
        client: Any = None,
        language: str = "html",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Judge an artifact, serving identical artifacts from the persistent cache.

        The cache is keyed on (task, judge slug, artifact sha256) and participates
        only in live runs — dry runs never read or write it so they stay
        deterministic. A per-key lock serializes get->call->put across threads so
        two parallel runs with identical artifacts don't duplicate the API call.
        """
        client = client or self.client
        if self.dry_run:
            return judge_artifact(
                logger=logger, step=step, task=task, artifact=artifact_text or "",
                judge=judge, client=client, dry_run=True, image_bytes=artifact_bytes,
                language=language,
            )

        payload = artifact_bytes if artifact_bytes is not None else (artifact_text or "").encode()
        # task prompt is part of the key so a task edit under the same id invalidates
        sha = hashlib.sha256(task.prompt.encode() + b"\0" + payload).hexdigest()
        with self.store.judge_lock((task.id, judge.slug, sha)):
            if self.use_judge_cache:
                cached = self.store.get_judge_result(task.id, judge.slug, sha)
                if cached is not None:
                    logger.log(
                        phase="judge",
                        step=step,
                        event_type="judge_cache_hit",
                        model=judge.slug,
                        role="judge",
                        input_data={"task": task.id, "artifact_sha256": sha},
                        output_data=cached,
                        reasoning="Judge result served from cache; no API call made.",
                    )
                    return cached, []
            result, costs = judge_artifact(
                logger=logger, step=step, task=task, artifact=artifact_text or "",
                judge=judge, client=client, dry_run=False, image_bytes=artifact_bytes,
                language=language,
            )
            # synthetic parse-failure results are transient — don't poison the cache
            if not result.get("parse_failed"):
                self.store.put_judge_result(task.id, judge.slug, sha, result)
            return result, costs

    def _validate(self, task: TaskSpec, artifact: str) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"html_parses", "non_empty", "has_title"}
        # "html" is the shorthand used by generated batch tasks
        if "html" in requested:
            requested.discard("html")
            requested |= {"html_parses", "non_empty"}
        checks: dict[str, bool] = {}
        errors: list[str] = []

        if "html_parses" in requested:
            parser = _HTMLValidator()
            try:
                parser.feed(artifact)
            except Exception as exc:
                parser.errors.append(str(exc))
            checks["html_parses"] = not parser.errors
            errors.extend(f"HTML parse error: {e}" for e in parser.errors)

        lowered = artifact.lower()

        if "non_empty" in requested:
            checks["non_empty"] = bool(artifact.strip())
            if not checks["non_empty"]:
                errors.append("Artifact is empty.")
        if "has_title" in requested:
            checks["has_title"] = "<title>" in lowered
            if not checks["has_title"]:
                errors.append("Missing <title>.")
        if "has_cta" in requested:
            checks["has_cta"] = any(
                token in lowered
                for token in ("cta", "sign up", "signup", "subscribe", "get started", "buy now", "learn more")
            )
            if not checks["has_cta"]:
                errors.append("Missing call-to-action.")
        if "has_form" in requested:
            checks["has_form"] = "<form" in lowered
            if not checks["has_form"]:
                errors.append("Missing <form>.")
        if "has_viewport" in requested:
            checks["has_viewport"] = 'name="viewport"' in lowered or "name='viewport'" in lowered
            if not checks["has_viewport"]:
                errors.append("Missing viewport meta tag.")
        if "no_placeholder" in requested:
            checks["no_placeholder"] = not any(
                token in lowered
                for token in ("lorem ipsum", "placeholder text", "todo:", "your text here", "[insert")
            )
            if not checks["no_placeholder"]:
                errors.append("Artifact contains placeholder text.")

        # Metadata-driven constraint checks — used by `constraint` tasks and
        # composable onto any text-producing task. Each fails closed when the
        # check is requested but its metadata key is missing.
        if "within_budget" in requested:
            bounds = {
                "min_chars": len(artifact),
                "max_chars": len(artifact),
                "min_words": len(artifact.split()),
                "max_words": len(artifact.split()),
            }
            declared = {k: task.metadata.get(k) for k in bounds if task.metadata.get(k) is not None}
            checks["within_budget"] = bool(declared)
            if not declared:
                errors.append("within_budget requested but no min/max chars/words in metadata.")
            for key, actual in bounds.items():
                limit = declared.get(key)
                if limit is None:
                    continue
                violated = actual < int(limit) if key.startswith("min") else actual > int(limit)
                if violated:
                    checks["within_budget"] = False
                    errors.append(f"{key} violated: {actual} vs limit {limit}.")
        if "has_required" in requested:
            required = task.metadata.get("required") or []
            missing_req = [t for t in required if str(t).lower() not in lowered]
            checks["has_required"] = bool(required) and not missing_req
            if not required:
                errors.append("has_required requested but metadata.required is empty.")
            elif missing_req:
                errors.append(f"Missing required token(s): {', '.join(map(str, missing_req))}.")
        if "no_forbidden" in requested:
            forbidden = task.metadata.get("forbidden") or []
            hits = [t for t in forbidden if str(t).lower() in lowered]
            checks["no_forbidden"] = bool(forbidden) and not hits
            if not forbidden:
                errors.append("no_forbidden requested but metadata.forbidden is empty.")
            elif hits:
                errors.append(f"Forbidden token(s) present: {', '.join(map(str, hits))}.")
        if "matches_pattern" in requested:
            pattern = task.metadata.get("pattern")
            if not pattern:
                checks["matches_pattern"] = False
                errors.append("matches_pattern requested but metadata.pattern is empty.")
            else:
                try:
                    checks["matches_pattern"] = re.search(str(pattern), artifact, re.DOTALL) is not None
                    if not checks["matches_pattern"]:
                        errors.append(f"Artifact does not match pattern {pattern!r}.")
                except re.error as exc:
                    checks["matches_pattern"] = False
                    errors.append(f"metadata.pattern is not a valid regex: {exc}.")
        if "no_pattern" in requested:
            pattern = task.metadata.get("forbidden_pattern")
            if not pattern:
                checks["no_pattern"] = False
                errors.append("no_pattern requested but metadata.forbidden_pattern is empty.")
            else:
                try:
                    checks["no_pattern"] = re.search(str(pattern), artifact, re.DOTALL) is None
                    if not checks["no_pattern"]:
                        errors.append(f"Artifact matches forbidden pattern {pattern!r}.")
                except re.error as exc:
                    checks["no_pattern"] = False
                    errors.append(f"metadata.forbidden_pattern is not a valid regex: {exc}.")

        unknown = sorted(requested - VALIDATION_CHECKS["html"])
        if unknown:
            errors.append(f"Unknown validation check(s): {', '.join(unknown)}.")
        passes, report = _validation_report(task, checks, errors, len(artifact))
        return passes and not unknown, report

    def _validate_image(self, task: TaskSpec, artifact: bytes) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"non_empty", "png_signature"}
        checks: dict[str, bool] = {}
        errors: list[str] = []

        if "non_empty" in requested:
            checks["non_empty"] = bool(artifact)
            if not checks["non_empty"]:
                errors.append("Artifact is empty.")
        if "png_signature" in requested:
            checks["png_signature"] = artifact.startswith(PNG_MAGIC) and artifact.endswith(PNG_IEND)
            if not checks["png_signature"]:
                errors.append("Artifact is not a well-formed PNG (bad magic or missing IEND).")

        unknown = sorted(requested - VALIDATION_CHECKS["image"])
        if unknown:
            errors.append(f"Unknown validation check(s): {', '.join(unknown)}.")
        passes, report = _validation_report(task, checks, errors, len(artifact))
        return passes and not unknown, report

    def _validate_multi(self, task: TaskSpec, artifact: bytes) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"non_empty", "zip_signature"}
        known = VALIDATION_CHECKS["multi-file"]
        checks: dict[str, bool] = {}
        errors: list[str] = []

        if "non_empty" in requested:
            checks["non_empty"] = bool(artifact)
            if not checks["non_empty"]:
                errors.append("Artifact is empty.")
        present: dict[str, int] = {}
        if "zip_signature" in requested or "has_paths" in requested:
            try:
                with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
                    present = {
                        info.filename: info.file_size
                        for info in archive.infolist()
                        if not info.filename.endswith("/")
                    }
            except zipfile.BadZipFile:
                present = {}
        if "zip_signature" in requested:
            checks["zip_signature"] = zipfile.is_zipfile(io.BytesIO(artifact))
            if not checks["zip_signature"]:
                errors.append("Artifact is not a readable zip archive.")
        if "has_paths" in requested:
            declared = expected_paths(task.metadata)
            missing = [p for p in declared if present.get(p, 0) <= 0]
            checks["has_paths"] = bool(declared) and not missing
            if not declared:
                errors.append("has_paths requested but metadata.expected_paths is empty.")
            elif missing:
                errors.append(f"Missing or empty expected files: {', '.join(missing)}.")

        unknown = sorted(requested - known)
        if unknown:
            errors.append(f"Unknown validation check(s): {', '.join(unknown)}.")
        passes, report = _validation_report(task, checks, errors, len(artifact))
        return passes and not unknown, report

    def _validate_code(self, task: TaskSpec, files: dict[str, str]) -> tuple[bool, dict[str, Any]]:
        """Validate a code file set without executing generated code.

        Expected files must exist. Live validation calls the fail-closed code
        execution boundary; dry runs only compile-check the Python files, so
        no model code ever runs in either path.
        """
        module = str(task.metadata.get("module") or "solution.py")
        declared = expected_paths(task.metadata) or [module]
        missing = [p for p in declared if not files.get(p)]
        errors = [f"Missing or empty expected files: {', '.join(missing)}."] if missing else []
        checks: dict[str, bool] = {"expected_paths": not missing}
        quality = check_code_quality(files, task.metadata)
        checks["quality_ok"] = not quality["violations"]
        errors.extend(quality["violations"])

        report: dict[str, Any] = {
            "task_id": task.id,
            "checks": checks,
            "errors": errors,
            "quality": quality,
            "score": None,
        }
        if missing:
            return False, report

        if self.dry_run:
            import py_compile
            import tempfile

            compiled = True
            with tempfile.TemporaryDirectory(prefix="orchestral-code-") as tmp:
                materialize(files, Path(tmp))
                for rel in files:
                    if rel.endswith(".py"):
                        try:
                            py_compile.compile(str(Path(tmp) / rel), doraise=True)
                        except py_compile.PyCompileError as exc:
                            compiled = False
                            errors.append(f"{rel}: {exc.msg}")
            checks["compiles"] = compiled
            report["executed"] = False
            return compiled and checks["quality_ok"], report

        suite = run_unittest_suite(
            files,
            str(task.metadata.get("tests") or ""),
            timeout_seconds=float(task.metadata.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        )
        report["execution"] = suite
        suite_passed = (
            suite.get("executed") is True
            and int(suite.get("tests_run") or 0) > 0
            and bool(suite.get("ok"))
        )
        checks["tests_pass"] = suite_passed
        if not suite.get("executed"):
            errors.append(suite.get("error", "tests did not execute"))
        elif not suite_passed:
            errors.append("code execution did not complete a successful non-empty test suite")
        report["score"] = score_from_report(suite)
        return bool(all(checks.values())), report

    def _validate_video(self, task: TaskSpec, artifact: bytes) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"non_empty", "mp4_signature"}
        checks: dict[str, bool] = {}
        errors: list[str] = []

        if "non_empty" in requested:
            checks["non_empty"] = bool(artifact)
            if not checks["non_empty"]:
                errors.append("Artifact is empty.")
        if "mp4_signature" in requested:
            # ISO-BMFF: first box must be ftyp (4-byte size + 'ftyp' at offset 4)
            checks["mp4_signature"] = len(artifact) >= 12 and artifact[4:8] == b"ftyp"
            if not checks["mp4_signature"]:
                errors.append("Artifact is not a well-formed MP4 (missing leading ftyp box).")

        unknown = sorted(requested - VALIDATION_CHECKS["video"])
        if unknown:
            errors.append(f"Unknown validation check(s): {', '.join(unknown)}.")
        passes, report = _validation_report(task, checks, errors, len(artifact))
        return passes and not unknown, report

    def _validate_sql(self, task: TaskSpec, sql: str) -> tuple[bool, dict[str, Any]]:
        report = run_sql_check(task.metadata, sql)
        checks = {"executed": report["executed"], "matches_reference": report["match"]}
        errors = [report["error"]] if report.get("error") else []
        passes, out = _validation_report(task, checks, errors, len(sql))
        out.update(
            {
                "score": report["score"],
                "ordered": report["ordered"],
                "rows_expected": report["rows_expected"],
                "rows_got": report["rows_got"],
            }
        )
        if report.get("expected_preview"):
            out["expected_preview"] = report["expected_preview"]
            out["got_preview"] = report["got_preview"]
        return passes, out

    def _validate_extract(self, task: TaskSpec, artifact: str) -> tuple[bool, dict[str, Any]]:
        report = check_extraction(task.metadata, artifact)
        report["task_id"] = task.id
        report["artifact_length"] = len(artifact)
        return bool(report.get("passes")), report

    def _validate_api(self, task: TaskSpec, plan_text: str) -> tuple[bool, dict[str, Any]]:
        report = check_api(task.metadata, plan_text)
        report["task_id"] = task.id
        report["artifact_length"] = len(plan_text)
        return bool(report.get("passes")), report


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB`\x82"


def _validation_report(
    task: TaskSpec, checks: dict[str, bool], errors: list[str], artifact_length: int
) -> tuple[bool, dict[str, Any]]:
    report: dict[str, Any] = {
        "task_id": task.id,
        "artifact_length": artifact_length,
        "checks": checks,
        "errors": errors,
        "score": None,
    }
    # zero recognised checks means the validation list was unknown names — a
    # silent pass would hide the typo, so require at least one check
    return bool(checks) and all(checks.values()), report


def _subtask_produced_output(
    out: dict[str, Any], media_bytes: bytes, files: dict[str, str], is_media: bool, is_multi: bool
) -> bool:
    """Did this attempt produce anything worth keeping?

    Media and multi-file subtasks have their own success signals — a multi-file
    record carries paths, not `content`, so the text check would retry forever.
    """
    if is_media:
        return bool(media_bytes)
    if is_multi:
        return bool(files)
    return bool(out.get("content"))


def _artifact_ext(task_type: str) -> str:
    return {"html": ".html", "image": ".png", "video": ".mp4", "api": ".json", "multi-file": ".zip", "code": ".zip", "sql": ".sql", "extract": ".json"}.get(task_type, ".txt")


class _HTMLValidator(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def handle_starttag(self, tag, attrs):
        pass


def _environment() -> dict[str, Any]:
    """Provenance labels for a run: what ran it, where, on which code."""
    return {
        "git_sha": _git_sha(),
        "python": platform.python_version(),
        "platform": f"{platform.system().lower()}/{platform.machine()}",
        "orchestral_version": _orch_version(),
    }


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _orch_version() -> str:
    try:
        from importlib.metadata import version

        return version("orchestral")
    except Exception:
        return "unknown"


def _write_metrics(run_dir: Path) -> None:
    """Derive metrics.json from the run's events.jsonl. Best-effort — a
    metrics bug must never fail or mask a run's real outcome."""
    try:
        metrics = build_metrics(run_dir / "events.jsonl")
        (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    except Exception:
        pass
