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

from orchestral.agentexec import (
    ExecutorCancelled,
    launch_gate,
    preflight,
)
from orchestral.apistub import check_api
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
    files_listing_with_content,
    member_requirements,
    merge_filesets,
)
from orchestral.judge import (
    JUDGE_CHAT_ARTIFACT_CAP,
    JUDGE_DECISIONS_ARTIFACT_CAP,
    is_decisions_model,
    judge_artifact,
)
from orchestral.logger import EventLogger
from orchestral.manifest import build_manifest, finalize_manifest, write_manifest
from orchestral.metrics import build_metrics
from orchestral.openrouter import OpenRouterVideoSubmittedError
from orchestral.planners import (
    assemble_ce,
    assemble_media,
    assemble_raw,
    delegate,
    delegate_agentic,
    delegate_api,
    delegate_constraint,
    delegate_extract,
    delegate_image,
    delegate_multi,
    delegate_needle,
    delegate_patch,
    delegate_sql,
    delegate_terminal,
    delegate_video,
    plan_ce,
    plan_raw,
)
from orchestral.privacy import scrub_text
from orchestral.providers import provider_for, provider_key
from orchestral.sqlexec import run_sql_check
from orchestral.storage import RunMeta, RunStore
from orchestral.taxonomy import classify_exception, retryable
from orchestral.terminal import check_terminal


class ValidationError(Exception):
    """Raised when a run fails structural validation."""


# task types whose assembly is "orchestrator picks the best candidate":
# type -> (artifact filename, result key to read, validator method name)
def _plan_self_output(plan: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Extract a self-executed artifact from a zero-subtask plan.

    Orchestrators sometimes answer the task directly instead of delegating:
    code/multi-file plans carry {"files": [{path, content}, ...]} (or a
    {path: content} map); text tasks carry a {"content"|"answer"|"query"|
    "output"|"response"|"text": str} payload. Returns (files, text) —
    both empty when the plan has no self-executed output.
    """
    files: dict[str, str] = {}
    raw_files = plan.get("files")
    if isinstance(raw_files, dict):
        files = {str(k): str(v) for k, v in raw_files.items() if str(v).strip()}
    elif isinstance(raw_files, list):
        for f in raw_files:
            if isinstance(f, dict) and f.get("path") and str(f.get("content") or "").strip():
                files[str(f["path"])] = str(f["content"])
    text = ""
    for key in ("content", "answer", "query", "output", "response", "text"):
        v = plan.get(key)
        if isinstance(v, str) and v.strip():
            text = v
            break
    return files, text


_CANDIDATE_TASKS: dict[str, tuple[str, str, str]] = {
    "needle": ("artifact.txt", "content", "_validate"),
    "constraint": ("artifact.txt", "content", "_validate"),
    "sql": ("artifact.sql", "query", "_validate_sql"),
    "extract": ("artifact.json", "content", "_validate_extract"),
    "api": ("artifact.json", "content", "_validate_api"),
    "terminal": ("artifact.json", "content", "_validate_terminal"),
    "swe-patch": ("artifact.diff", "content", "_validate_patch"),
}


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
        allow_agent_exec: bool = False,
        sandbox: str = "local",
        sandbox_image: str | None = None,
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
        # launch-context opt-in for agent-CLI workers — one leg of the
        # executor trust boundary (worker flag + task declaration + this)
        self.allow_agent_exec = allow_agent_exec
        # called with run_id as soon as the run dir exists — lets a caller
        # (e.g. the TUI) map a job to its in-flight run before run() returns
        self.on_run_created = on_run_created
        self.use_judge_cache = use_judge_cache
        # Library callers historically received local execution. Production
        # launch surfaces pass Docker explicitly; keeping the old default
        # avoids turning existing mock/test callers into Docker-dependent
        # tests while the CLI/TUI/web surfaces fail closed on isolation.
        self.sandbox = sandbox
        self.sandbox_image = sandbox_image
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
            if role == "worker" and (model.metadata or {}).get("executor"):
                # executor workers are agent CLIs, not chat providers —
                # provider_for would raise ProviderConfigError on them
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

    def _resolve_executor(self, task: TaskSpec, worker: ModelConfig) -> Any:
        """The KTD12 dispatch conjunction, checked explicitly.

        Executor routing requires all three: `worker.metadata.executor`
        (adapter name), `task.metadata.requires_executor`, and the launch
        opt-in. Every half-state fails loudly — an executor worker on an
        undeclared task is executor_preflight (never retries), and a
        declared task on a chat worker is a launch validation error. The
        executor-side checks live in `agentexec.launch_gate` so the CLI
        env check, web registry, and TUI modal enforce the same rules.
        """
        name = (worker.metadata or {}).get("executor")
        if not name:
            if (task.metadata or {}).get("requires_executor"):
                raise ValidationError(
                    f"Task {task.id} declares metadata.requires_executor but "
                    f"worker {worker.slug} is not an executor worker"
                )
            return None
        return launch_gate(
            worker, task, allow_agent_exec=self.allow_agent_exec, probe=False,
        )

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
            "sandbox": self.sandbox,
            "sandbox_image": self.sandbox_image,
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
            # Executor dispatch (KTD12): the adapter resolves only when the
            # worker flag, task declaration, and launch opt-in all agree —
            # every half-state is a recorded failure, not a silent fallback.
            executor = self._resolve_executor(task, worker)
            if executor is not None:
                manifest["worker_executor"] = {
                    "adapter": executor.name,
                    "argv_hash": hashlib.sha256(
                        "\0".join(
                            executor.run_argv(executor.binary, "<prompt>")
                        ).encode()
                    ).hexdigest(),
                    "cli_version": None,
                }
                # the chat worker system prompt never reaches an agent CLI
                manifest["worker_prompt_hash"] = None
                write_manifest(run_dir, manifest)
                manifest["worker_executor"]["cli_version"] = preflight(executor)
                write_manifest(run_dir, manifest)
            agent_diffs: list[str] = []

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
            is_multi = task.type in ("multi-file", "code", "bugfix")
            is_constraint = task.type == "constraint"
            is_needle = task.type == "needle"
            is_sql = task.type == "sql"
            is_extract = task.type == "extract"
            is_api = task.type == "api"
            is_terminal = task.type == "terminal"
            is_patch = task.type == "swe-patch"
            is_pipeline = task.type == "pipeline"
            is_media = is_image or is_video
            results: list[dict[str, Any]] = []
            media_paths: list[Path | None] = []
            file_sets: list[tuple[Any, dict[str, str]]] = []
            media_ext = _artifact_ext(task.type)
            # schema variants: canonical {"subtasks": [...]}, nested
            # {"sections": {"subtasks": [...]}}, or a steps-style
            # {"plan": [...]} — models legitimately emit all three
            subtasks = (
                plan.get("subtasks")
                or plan.get("sections", {}).get("subtasks", [])
                or plan.get("plan")
                or []
            )
            if not isinstance(subtasks, list):
                raise ValidationError(
                    f"Plan subtasks must be a list, got {type(subtasks).__name__}"
                )
            # cost-control gate: an orchestrator that over-decomposes burns a
            # worker call per subtask — exceeding the budget is a recorded
            # failure, not a silent truncation
            max_subtasks = int(task.metadata.get("max_subtasks") or 20)
            if len(subtasks) > max_subtasks:
                raise ValidationError(
                    f"Plan produced {len(subtasks)} subtasks, over max_subtasks {max_subtasks}"
                )
            # Zero-delegation plans: some orchestrators answer the task
            # themselves ({"files": ...} or {"content"/"answer": ...})
            # instead of producing subtasks. That is real
            # delegation-vs-self-execution signal — accept the plan's own
            # payload as the artifact source so the run is judged on merit,
            # and mark it `delegated: false` so aggregates can filter it.
            self_executed = False
            if not subtasks:
                self_files, self_text = _plan_self_output(plan)
                if self_files or self_text:
                    self_executed = True
                    logger.lifecycle(
                        "orchestrator.self_executed", phase="delegate",
                        role="orchestrator",
                        files=len(self_files), chars=len(self_text),
                    )
                    results.append({
                        "subtask_id": "orchestrator",
                        "content": self_text or files_listing_with_content(self_files, total_chars=JUDGE_CHAT_ARTIFACT_CAP),
                        "query": self_text,
                        "self_executed": True,
                    })
                    (run_dir / "worker-self.json").write_text(
                        json.dumps({"files": self_files, "content": self_text}, indent=2, default=str))
                    if self_files:
                        file_sets.append(("orchestrator", self_files))
                else:
                    # Raw/none planners may emit no subtasks; every task
                    # type's delegate paths are single-shot over one subtask
                    # (extract, sql, api, needle, constraint, media, multi).
                    # With zero subtasks the loop would run zero times and the
                    # assembly branches would write empty artifacts — fall
                    # back to one default subtask so the worker still runs.
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
                # the canonical brief always reaches the worker — an
                # orchestrator's thin description shouldn't leave the worker
                # guessing the spec (review: workers hallucinated the task)
                sub = {**sub}
                sub.setdefault("task_prompt", task.prompt)
                if is_pipeline and results:
                    # each subtask sees the outputs of every prior subtask —
                    # the chain is the test: does context actually propagate
                    sub = {**sub, "prior_outputs": [
                        {"subtask_id": r.get("subtask_id"), "content": r.get("content")}
                        for r in results
                    ]}
                out: dict[str, Any] | None = None
                media_bytes: bytes = b""
                files: dict[str, str] = {}
                # executor workers default to zero retries — a respawned
                # agent CLI is a fresh process with fresh cost, not a cheap
                # chat retry. The schema default (2) counts as "unspecified";
                # a non-default value is an explicit opt-in to retries.
                if executor is not None:
                    attempts = 1 if worker.retry_limit == 2 else max(1, worker.retry_limit + 1)
                else:
                    attempts = max(1, worker.retry_limit + 1)
                logger.lifecycle(
                    "worker.started", phase="delegate", role="worker",
                    worker_id=wid, subtask_id=sub.get("id", i),
                    description=str(sub.get("description", ""))[:200],
                )
                for attempt in range(attempts):
                    self._check_cancelled()
                    try:
                        if executor is not None:
                            out, files, agent_diff, worker_costs = delegate_agentic(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                adapter=executor,
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                                cancel_event=self.cancel_event,
                                evidence_dir=run_dir / "raw" / f"{wid}-attempt-{attempt + 1}",
                                repo_root=Path.cwd(),
                            )
                            if agent_diff:
                                agent_diffs.append(agent_diff)
                        elif is_image:
                            out, media_bytes, worker_costs = delegate_image(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
                            )
                        elif is_terminal:
                            out, worker_costs = delegate_terminal(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                                cancel_event=self.cancel_event,
                            )
                        elif is_patch:
                            out, worker_costs = delegate_patch(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                task=task,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                                attempt=attempt + 1,
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
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
                                cancel_event=self.cancel_event,
                            )
                        ledger.add_many(worker_costs)
                    except ExecutorCancelled as exc:
                        # agent-CLI cancel maps onto the run's own taxonomy —
                        # without this the generic handler could retry it
                        raise RunCancelled("cancelled during agent execution") from exc
                    except RunCancelled:
                        # cancel must not be retried — re-raise before the
                        # generic handler turns it into a respawn
                        raise
                    except Exception as exc:
                        out = None
                        cat = classify_exception(exc)
                        # executor errors can carry argv/env text — bounded
                        # and scrubbed before they land in a published event
                        err_text = str(exc)
                        if executor is not None:
                            err_text = scrub_text(err_text)[:1000]
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
                            error=err_text,
                            metadata={"error_category": cat, "subtask_id": sub.get("id", i)},
                        )
                        # Any post-submission video failure (terminal status,
                        # poll exhaustion, timeout, unsafe URL, download) is
                        # unrecoverable by retry — resubmitting bills a new job.
                        # Non-retryable categories (config, executor preflight,
                        # spawn failure) reproduce identically — fail fast.
                        will_retry = not (
                            isinstance(exc, OpenRouterVideoSubmittedError)
                            or not retryable(cat)
                            or attempt + 1 >= attempts
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
            elif task.type in _CANDIDATE_TASKS:
                # Orchestrator picks the winning worker output; the chosen
                # payload becomes the artifact and the type's validator runs.
                artifact_name, key, validate_name = _CANDIDATE_TASKS[task.type]
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
                artifact = str(chosen.get(key) or chosen.get("content") or "")
                (run_dir / artifact_name).write_text(artifact, encoding="utf-8")
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path=artifact_name, bytes=len(artifact.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = getattr(self, validate_name)(task, artifact)
                judge_text = artifact
            elif is_pipeline:
                # The chain's final output IS the artifact — orchestration
                # value was in the plan; assembly adds no synthesis call.
                if self.dry_run and task.metadata.get("reference_text"):
                    artifact = str(task.metadata["reference_text"])
                elif results:
                    artifact = str(results[-1].get("content") or "")
                else:
                    raise ValidationError("No output produced for the pipeline task")
                (run_dir / "artifact.txt").write_text(artifact, encoding="utf-8")
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.txt", bytes=len(artifact.encode()),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                passes, report = self._validate(task, artifact)
                judge_text = artifact
            elif is_multi:
                # Deterministic merge + zip: no orchestrator call, and the
                # artifact is bytes — the HTML branch below writes text.
                merged, conflicts = merge_filesets(file_sets)
                if not merged:
                    raise ValidationError("No files were produced for the multi-file task")
                artifact_bytes = build_zip(
                    merged,
                    preserve_case=executor is not None,
                    allow_hidden=bool((task.metadata or {}).get("allow_hidden")),
                )
                (run_dir / "artifact.zip").write_bytes(artifact_bytes)
                logger.lifecycle(
                    "artifact.saved", phase="assemble",
                    path="artifact.zip", bytes=len(artifact_bytes),
                )
                logger.lifecycle("synthesis.completed", phase="assemble", role="orchestrator")
                logger.lifecycle("evaluation.started", phase="validate")
                if task.type in ("code", "bugfix"):
                    passes, report = self._validate_code(
                        task, merged, preserve_case=executor is not None)
                else:
                    passes, report = self._validate_multi(
                        task, artifact_bytes, preserve_case=executor is not None)
                report["files"] = sorted(merged)
                report["merge_conflicts"] = conflicts
                # the judge reads bounded member bodies — a name-only listing
                # let verdicts be computed blind. Bodies enter the judge
                # prompt (and its logged events) but never report["files"].
                cap = (JUDGE_DECISIONS_ARTIFACT_CAP
                       if judge is not None and is_decisions_model(judge)
                       else JUDGE_CHAT_ARTIFACT_CAP)
                judge_text = files_listing_with_content(merged, total_chars=cap)
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
                if self.dry_run:
                    required = [str(t) for t in (task.metadata.get("required") or [])]
                    if required:
                        artifact += "\n" + " ".join(required)
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

            report["delegated"] = not self_executed
            report["subtasks"] = len(subtasks)

            # the judge reads the harvested diff for executor runs — the
            # real change under test, agent-controlled text and all (the
            # injection milestone already flagged instruction-shaped lines)
            if agent_diffs:
                judge_text = "\n".join(agent_diffs)

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
                try:
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
                except Exception as exc:
                    # the judge is advisory — its failure must not convert a
                    # mechanically-verified run into a `failed` record; the
                    # exception records judge_inconclusive on the judge axis
                    # (the same rule as parse failure and null verdicts)
                    logger.log(
                        phase="judge",
                        step=assembly_step + 3,
                        event_type="judge_error",
                        model=judge.slug,
                        role="judge",
                        input_data={"task": task.id},
                        output_data={},
                        error=str(exc),
                        reasoning="Judge call failed; keeping the mechanical verdict.",
                    )
                    report["judge_error"] = str(exc)
                    report["judge"] = {
                        "score": None, "passed": None, "inconclusive": True,
                        "model": judge.slug,
                        "reasoning": f"judge call failed: {str(exc)[:200]}",
                    }
                else:
                    ledger.add_many(judge_costs)
                    report["judge"] = judge_result
                    report.setdefault("judges", {})[judge.slug] = judge_result
                    # KTD14: the judge never mutates `passes` or `score` — the
                    # stored verdict is mechanical-only; judge evidence lives
                    # on the judge_* fields and report.judge.

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
                        from orchestral.shots import capture_html
                    except ImportError:
                        pass  # shots extras not installed — screenshot degrades cleanly
                    else:
                        try:
                            capture_html(artifact_path, run_dir / "screenshot.png")
                        except Exception as exc:
                            # observability garnish, never a run outcome
                            logger.log(
                                phase="shots",
                                step=assembly_step + 5,
                                event_type="screenshot_skipped",
                                model="",
                                role="harness",
                                input_data={"artifact": str(artifact_path)},
                                output_data={"reason": str(exc)},
                                reasoning="Screenshot capture failed or unavailable; skipped.",
                            )

            # 6. Final accounting
            total_cost, total_input, total_output = _flush_ledger(run_dir, ledger)
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
            meta.delegated = report.get("delegated")
            judge_result = report.get("judge") or {}
            if not judge_result.get("inconclusive"):
                meta.judge_score = judge_result.get("score")
                meta.judge_passed = judge_result.get("passed")
            meta.latency_ms = (time.perf_counter() - t0) * 1000
            if passes is False:
                meta.failure_reason = "validation"
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
            # cancel is a normal outcome, not an error — mark and return.
            # Whatever the run spent before the cancel still counts.
            with contextlib.suppress(Exception):
                _flush_ledger(run_dir, ledger)
            with contextlib.suppress(Exception):
                meta = self.store.get_run(run_id)
                if meta is not None:
                    meta.status = "cancelled"
                    meta.finished_at = datetime.now(UTC).isoformat()
                    meta.latency_ms = (time.perf_counter() - t0) * 1000
                    meta.failure_reason = "cancelled"
                    meta.total_cost_usd = ledger.total_cost_usd()
                    meta.total_input_tokens = ledger.total_input_tokens()
                    meta.total_output_tokens = ledger.total_output_tokens()
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
            # or dead log handle inside the handler would otherwise replace it.
            # The run's accrued spend is recorded before it dies.
            with contextlib.suppress(Exception):
                _flush_ledger(run_dir, ledger)
            with contextlib.suppress(Exception):
                meta = self.store.get_run(run_id)
                if meta is not None:
                    meta.status = "failed"
                    meta.finished_at = datetime.now(UTC).isoformat()
                    meta.latency_ms = (time.perf_counter() - t0) * 1000
                    meta.failure_reason = f"exception:{cat}"
                    meta.total_cost_usd = ledger.total_cost_usd()
                    meta.total_input_tokens = ledger.total_input_tokens()
                    meta.total_output_tokens = ledger.total_output_tokens()
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
            # inconclusive results (parse failure, null verdict, skips) are
            # transient no-answers — don't poison the cache with them
            if not result.get("inconclusive"):
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
        if "exact_answer" in requested:
            expected = task.metadata.get("expected_answer")
            if expected is None:
                checks["exact_answer"] = False
                errors.append("exact_answer requested but metadata.expected_answer is missing.")
            else:
                checks["exact_answer"] = artifact.strip() == str(expected).strip()
                if not checks["exact_answer"]:
                    errors.append("Artifact is not exactly the expected answer.")
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

        known = {
            "html", "html_parses", "non_empty", "has_title", "has_cta", "has_form",
            "has_viewport", "no_placeholder", "within_budget", "has_required",
            "no_forbidden", "exact_answer", "matches_pattern", "no_pattern",
        }
        unknown = sorted(requested - known)
        if unknown:
            errors.append(f"Unknown validation check(s): {', '.join(unknown)}.")
        passes, report = _validation_report(task, checks, errors, len(artifact))
        return passes and not unknown, report

    def _validate_image(self, task: TaskSpec, artifact: bytes) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"non_empty", "png_signature"}
        known = {"non_empty", "png_signature"}
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

        unknown = sorted(requested - known)
        if unknown:
            errors.append(f"Unknown validation check(s): {', '.join(unknown)}.")
        passes, report = _validation_report(task, checks, errors, len(artifact))
        return passes and not unknown, report

    def _validate_multi(
        self,
        task: TaskSpec,
        artifact: bytes,
        *,
        preserve_case: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"non_empty", "zip_signature"}
        known = {"non_empty", "zip_signature", "has_paths", "member_required"}
        checks: dict[str, bool] = {}
        errors: list[str] = []

        if "non_empty" in requested:
            checks["non_empty"] = bool(artifact)
            if not checks["non_empty"]:
                errors.append("Artifact is empty.")

        archive: zipfile.ZipFile | None = None
        if requested & {"zip_signature", "has_paths", "member_required"}:
            try:
                archive = zipfile.ZipFile(io.BytesIO(artifact))
            except zipfile.BadZipFile:
                archive = None
        if "zip_signature" in requested:
            checks["zip_signature"] = archive is not None
            if not checks["zip_signature"]:
                errors.append("Artifact is not a readable zip archive.")
        if "has_paths" in requested:
            present = (
                {info.filename: info.file_size for info in archive.infolist() if not info.filename.endswith("/")}
                if archive is not None else {}
            )
            declared = expected_paths(task.metadata, preserve_case=preserve_case)
            missing = [p for p in declared if present.get(p, 0) <= 0]
            checks["has_paths"] = bool(declared) and not missing
            if not declared:
                errors.append("has_paths requested but metadata.expected_paths is empty.")
            elif missing:
                errors.append(f"Missing or empty expected files: {', '.join(missing)}.")
        if "member_required" in requested:
            member_req = member_requirements(task.metadata, preserve_case=preserve_case)
            if not member_req:
                checks["member_required"] = False
                errors.append("member_required requested but metadata.member_required is empty.")
            elif archive is None:
                checks["member_required"] = False
                errors.append("Artifact is not a readable zip archive.")
            else:
                member_missing: list[str] = []
                token_missing: list[str] = []
                for member, tokens in member_req.items():
                    try:
                        raw = archive.read(member)
                    except KeyError:
                        member_missing.append(member)
                        continue
                    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, OSError) as exc:
                        member_missing.append(f"{member} (unreadable: {type(exc).__name__})")
                        continue
                    try:
                        text = raw.decode("utf-8").lower()
                    except UnicodeDecodeError:
                        member_missing.append(f"{member} (not decodable text)")
                        continue
                    absent = [t for t in tokens if t.lower() not in text]
                    if absent:
                        token_missing.append(f"{member}: {', '.join(absent)}")
                checks["member_required"] = not (member_missing or token_missing)
                if member_missing:
                    errors.append(f"Members listed in member_required absent: {', '.join(member_missing)}.")
                if token_missing:
                    errors.append(f"Required content missing in members: {'; '.join(token_missing)}.")
        if archive is not None:
            archive.close()

        unknown = sorted(requested - known)
        if unknown:
            errors.append(f"Unknown validation check(s): {', '.join(unknown)}.")
        passes, report = _validation_report(task, checks, errors, len(artifact))
        return passes and not unknown, report

    def _validate_code(
        self,
        task: TaskSpec,
        files: dict[str, str],
        *,
        preserve_case: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        """Run the task's hidden unittest source against the merged file set.

        Expected files must exist; live runs execute the suite in a
        subprocess (see codeexec for containment notes). Dry runs only
        compile-check the Python files — no model code ever ran, so the
        report says `executed: false`. `preserve_case` selects the executor
        canonical variant for expected-path matching (Main.java keeps case).
        """
        module = str(task.metadata.get("module") or "solution.py")
        declared = expected_paths(task.metadata, preserve_case=preserve_case) or [module]
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
            sandbox=self.sandbox,
            sandbox_image=self.sandbox_image,
        )
        report["execution"] = suite
        checks["tests_pass"] = bool(suite.get("ok"))
        if not suite.get("executed"):
            errors.append(suite.get("error", "tests did not execute"))
        report["score"] = score_from_report(suite)
        return bool(all(checks.values())), report

    def _validate_video(self, task: TaskSpec, artifact: bytes) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"non_empty", "mp4_signature"}
        known = {"non_empty", "mp4_signature"}
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

        unknown = sorted(requested - known)
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

    def _validate_terminal(self, task: TaskSpec, plan_text: str) -> tuple[bool, dict[str, Any]]:
        report = check_terminal(task.metadata, plan_text)
        report["task_id"] = task.id
        report["artifact_length"] = len(plan_text)
        return bool(report.get("passes")), report

    def _validate_patch(self, task: TaskSpec, patch_text: str) -> tuple[bool, dict[str, Any]]:
        """Apply the worker's diff to metadata.files; run the hidden tests.

        `applies` is its own gate — a diff that doesn't apply fails before
        tests run, so patch-craft is measured independently of correctness.
        """
        from orchestral.patch import PatchError, apply_unified_diff, extract_patch

        repo = {str(k): str(v) for k, v in (task.metadata.get("files") or {}).items()}
        checks: dict[str, bool] = {}
        errors: list[str] = []
        report: dict[str, Any] = {
            "task_id": task.id,
            "artifact_length": len(patch_text),
            "checks": checks,
            "errors": errors,
            "score": None,
        }
        diff = extract_patch(patch_text)
        checks["extracted"] = diff is not None
        if diff is None:
            errors.append("artifact does not contain a unified diff")
            return False, report
        try:
            patched = apply_unified_diff(repo, diff)
            checks["applies"] = True
        except PatchError as exc:
            checks["applies"] = False
            errors.append(f"patch does not apply: {exc}")
            return False, report

        quality = check_code_quality(patched, task.metadata)
        checks["quality_ok"] = not quality["violations"]
        errors.extend(quality["violations"])
        report["quality"] = quality
        report["files"] = sorted(patched)

        if self.dry_run:
            report["executed"] = False
            return checks["applies"] and checks["quality_ok"], report
        suite = run_unittest_suite(
            patched,
            str(task.metadata.get("tests") or ""),
            timeout_seconds=float(task.metadata.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            sandbox=self.sandbox,
            sandbox_image=self.sandbox_image,
        )
        report["execution"] = suite
        checks["tests_pass"] = bool(suite.get("ok"))
        if not suite.get("executed"):
            errors.append(suite.get("error", "tests did not execute"))
        report["score"] = score_from_report(suite)
        return bool(all(checks.values())), report


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
    return {"html": ".html", "image": ".png", "video": ".mp4", "api": ".json", "terminal": ".json", "swe-patch": ".diff", "multi-file": ".zip", "code": ".zip", "bugfix": ".zip", "sql": ".sql", "extract": ".json"}.get(task_type, ".txt")


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


def _flush_ledger(run_dir: Path, ledger: CostLedger) -> tuple[float, int, int]:
    """Write cost.json and return (usd, input_tokens, output_tokens).

    Called on every exit path — a failed or cancelled run still spent real
    money, and spend_today/leaderboards read meta.total_cost_usd."""
    total_cost = ledger.total_cost_usd()
    total_input = ledger.total_input_tokens()
    total_output = ledger.total_output_tokens()
    (run_dir / "cost.json").write_text(json.dumps(ledger.to_breakdown(), indent=2, default=str))
    return total_cost, total_input, total_output


def _write_metrics(run_dir: Path) -> None:
    """Derive metrics.json from the run's events.jsonl. Best-effort — a
    metrics bug must never fail or mask a run's real outcome."""
    try:
        metrics = build_metrics(run_dir / "events.jsonl")
        (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    except Exception:
        pass
