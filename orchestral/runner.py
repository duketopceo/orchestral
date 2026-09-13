"""Orchestrator -> worker -> assemble -> validate loop with full logging."""

from __future__ import annotations

import hashlib
import html.parser
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import CostLedger
from orchestral.fileset import (
    build_zip,
    expected_paths,
    manifest_listing,
    merge_filesets,
)
from orchestral.judge import judge_artifact
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterVideoSubmittedError
from orchestral.planners import (
    assemble_ce,
    assemble_media,
    assemble_raw,
    delegate,
    delegate_image,
    delegate_multi,
    delegate_video,
    plan_ce,
    plan_raw,
)
from orchestral.providers import provider_for, provider_key
from orchestral.storage import RunMeta, RunStore


class ValidationError(Exception):
    """Raised when a run fails structural validation."""


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
    ):
        self.dry_run = dry_run
        self.planner = planner
        self.prompt_variant = prompt_variant
        self.sweep = sweep
        self.use_judge_cache = use_judge_cache
        self.store = store or RunStore(runs_dir)
        # role ("orchestrator"/"worker"/"judge") -> Provider, injected for tests
        self._injected_clients = dict(clients or {})
        # legacy single-client handle; tests may set this directly
        self.client: Any = None
        self._owned_clients: list[Any] = []

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
        # fails fast instead of leaving a run stuck at "running".
        role_clients = self._resolve_clients(orchestrator, worker, judge)
        providers = {
            role: provider_key(model)
            for role, model in (("orchestrator", orchestrator), ("worker", worker), ("judge", judge))
            if model is not None
        }
        run_id, run_dir = self.store.new_run(
            orchestrator.slug,
            task.id,
            worker.slug,
            config={
                "dry_run": self.dry_run,
                "planner": self.planner,
                "prompt_variant": self.prompt_variant,
                "sweep": self.sweep,
                "judge": judge.slug if judge else None,
                "orchestrator": orchestrator.to_dict(),
                "worker": worker.to_dict(),
            },
        )
        logger = EventLogger(run_dir)
        logger.log(
            phase="init",
            step=0,
            event_type="run_start",
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
            },
            reasoning="Initialised run directory, SQLite index, and event log.",
        )

        ledger = CostLedger()
        meta: RunMeta | None = None

        try:
            # 1. Plan
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

            # 2. Delegate each subtask to the worker
            is_image = task.type == "image"
            is_video = task.type == "video"
            is_multi = task.type == "multi-file"
            is_media = is_image or is_video
            results: list[dict[str, Any]] = []
            media_paths: list[Path | None] = []
            file_sets: list[tuple[int, dict[str, str]]] = []
            media_ext = _artifact_ext(task.type)
            subtasks = plan.get("subtasks") or plan.get("sections", {}).get("subtasks", [])
            for i, sub in enumerate(subtasks):
                # models sometimes return a list of strings; normalize to dicts
                if isinstance(sub, str):
                    sub = {"id": i, "description": sub}
                elif not isinstance(sub, dict):
                    sub = {"id": i, "description": str(sub)}
                out: dict[str, Any] | None = None
                media_bytes: bytes = b""
                files: dict[str, str] = {}
                attempts = max(1, worker.retry_limit + 1)
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
                            )
                        else:
                            out, worker_costs = delegate(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                worker=worker,
                                client=role_clients.get("worker"),
                                dry_run=self.dry_run,
                            )
                        ledger.add_many(worker_costs)
                    except Exception as exc:
                        out = None
                        logger.log(
                            phase="delegate",
                            step=i + 3,
                            event_type="worker_error",
                            model=worker.slug,
                            role="worker",
                            input_data={"subtask": sub},
                            output_data={"attempt": attempt + 1, "max_attempts": attempts},
                            reasoning=f"Worker call raised an exception on attempt {attempt + 1}.",
                            error=str(exc),
                        )
                        # Any post-submission video failure (terminal status,
                        # poll exhaustion, timeout, unsafe URL, download) is
                        # unrecoverable by retry — resubmitting bills a new job.
                        if isinstance(exc, OpenRouterVideoSubmittedError) or attempt + 1 >= attempts:
                            raise
                        continue
                    if _subtask_produced_output(out, media_bytes, files, is_media, is_multi):
                        break
                    logger.log(
                        phase="delegate",
                        step=i + 3,
                        event_type="worker_retry",
                        model=worker.slug,
                        role="worker",
                        input_data={"subtask": sub},
                        output_data={"attempt": attempt + 1, "max_attempts": attempts},
                        reasoning=f"Worker returned empty output on attempt {attempt + 1}; retrying.",
                    )
                if out is None:
                    raise ValidationError(f"Worker {worker.slug} produced no output for subtask {sub.get('id')}")
                out["attempts"] = attempt + 1
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
                if is_image:
                    passes, report = self._validate_image(task, artifact_bytes)
                    judge_bytes = artifact_bytes
                else:
                    passes, report = self._validate_video(task, artifact_bytes)
            elif is_multi:
                # Deterministic merge + zip: no orchestrator call, and the
                # artifact is bytes — the HTML branch below writes text.
                merged, conflicts = merge_filesets(file_sets)
                if not merged:
                    raise ValidationError("No files were produced for the multi-file task")
                artifact_bytes = build_zip(merged)
                (run_dir / "artifact.zip").write_bytes(artifact_bytes)
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
                if judge_result.get("score") is not None:
                    report["score"] = judge_result["score"]
                if judge_result.get("passed") is not None:
                    passes = passes and judge_result["passed"]

            (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))

            # 5. Screenshot for HTML artifacts (optional, degrades cleanly)
            if task.type == "html":
                artifact_path = run_dir / "artifact.html"
                if artifact_path.exists():
                    try:
                        from orchestral.shots import ScreenshotUnavailable, capture_html

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

            meta = self.store.get_run(run_id)
            assert meta is not None
            meta.status = "finished"
            meta.finished_at = datetime.now(UTC).isoformat()
            meta.total_cost_usd = total_cost
            meta.total_input_tokens = total_input
            meta.total_output_tokens = total_output
            meta.passes = passes
            meta.score = report.get("score")
            self.store.update_meta(meta)

            logger.log(
                phase="end",
                step=assembly_step + 4,
                event_type="run_end",
                model="",
                role="harness",
                input_data={"total_cost": total_cost, "total_tokens": total_input + total_output},
                output_data={"status": "finished", "passes": passes, "score": meta.score},
                reasoning=f"Run finished. Cost ${total_cost:.4f}, tokens {total_input + total_output}, passes={passes}.",
            )

            return meta

        except Exception as exc:
            meta = self.store.get_run(run_id)
            if meta is not None:
                meta.status = "failed"
                meta.finished_at = datetime.now(UTC).isoformat()
                self.store.update_meta(meta)
            logger.log(
                phase="end",
                step=-1,
                event_type="run_failed",
                model="",
                role="harness",
                input_data={},
                output_data={"error": str(exc), "error_type": type(exc).__name__},
                reasoning="Run failed with an exception.",
                error=str(exc),
            )
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

        return _validation_report(task, checks, errors, len(artifact))

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

        return _validation_report(task, checks, errors, len(artifact))

    def _validate_multi(self, task: TaskSpec, artifact: bytes) -> tuple[bool, dict[str, Any]]:
        requested = set(task.validation) if task.validation else {"non_empty", "zip_signature"}
        known = {"non_empty", "zip_signature", "has_paths"}
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

        return _validation_report(task, checks, errors, len(artifact))


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
    return {"html": ".html", "image": ".png", "video": ".mp4", "api": ".json", "multi-file": ".zip"}.get(task_type, ".txt")


class _HTMLValidator(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def handle_starttag(self, tag, attrs):
        pass
