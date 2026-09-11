"""Orchestrator -> worker -> assemble -> validate loop with full logging."""

from __future__ import annotations

import html.parser
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import CostLedger
from orchestral.judge import judge_artifact
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterClient
from orchestral.planners import (
    assemble_ce,
    assemble_image,
    assemble_raw,
    delegate,
    delegate_image,
    plan_ce,
    plan_raw,
)
from orchestral.storage import RunMeta, RunStore


class ValidationError(Exception):
    """Raised when a run fails structural validation."""


class Runner:
    def __init__(self, *, dry_run: bool = False, planner: str = "raw", runs_dir: str | Path = "runs"):
        self.dry_run = dry_run
        self.planner = planner
        self.store = RunStore(runs_dir)
        self.client: OpenRouterClient | None = None
        if not dry_run:
            self.client = OpenRouterClient()

    def run(self, task: TaskSpec, orchestrator: ModelConfig, worker: ModelConfig, judge: ModelConfig | None = None) -> RunMeta:
        run_id, run_dir = self.store.new_run(
            orchestrator.slug,
            task.id,
            worker.slug,
            config={
                "dry_run": self.dry_run,
                "planner": self.planner,
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
            output_data={"run_id": run_id, "dry_run": self.dry_run},
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
                    client=self.client,
                    dry_run=self.dry_run,
                )
            else:
                plan, plan_costs = plan_raw(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=1,
                    client=self.client,
                    dry_run=self.dry_run,
                )
            (run_dir / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
            ledger.add_many(plan_costs)

            # 2. Delegate each subtask to the worker
            is_image = task.type == "image"
            results: list[dict[str, Any]] = []
            image_paths: list[Path | None] = []
            subtasks = plan.get("subtasks") or plan.get("sections", {}).get("subtasks", [])
            for i, sub in enumerate(subtasks):
                # models sometimes return a list of strings; normalize to dicts
                if isinstance(sub, str):
                    sub = {"id": i, "description": sub}
                elif not isinstance(sub, dict):
                    sub = {"id": i, "description": str(sub)}
                out: dict[str, Any] | None = None
                img_bytes: bytes = b""
                attempts = max(1, worker.retry_limit + 1)
                for attempt in range(attempts):
                    try:
                        if is_image:
                            out, img_bytes, worker_costs = delegate_image(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                worker=worker,
                                client=self.client,
                                dry_run=self.dry_run,
                            )
                        else:
                            out, worker_costs = delegate(
                                logger=logger,
                                step=i + 3,
                                subtask=sub,
                                worker=worker,
                                client=self.client,
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
                        if attempt + 1 >= attempts:
                            raise
                        continue
                    if (img_bytes if is_image else out.get("content")):
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
                if is_image and img_bytes:
                    img_path = run_dir / f"worker-{i}.png"
                    img_path.write_bytes(img_bytes)
                    image_paths.append(img_path)
                else:
                    image_paths.append(None)

            # 3. Assemble final artifact
            assembly_step = 3 + len(subtasks)
            if is_image:
                if any(p is not None for p in image_paths):
                    position, assembly_costs = assemble_image(
                        logger=logger,
                        task=task,
                        orchestrator=orchestrator,
                        step=assembly_step,
                        results=results,
                        client=self.client,
                        dry_run=self.dry_run,
                    )
                else:
                    position, assembly_costs = 0, []
                ledger.add_many(assembly_costs)
                selected = image_paths[position] if 0 <= position < len(image_paths) else None
                if selected is None:
                    # orchestrator picked a subtask with no image; fall back to first captured
                    selected = next((p for p in image_paths if p is not None), None)
                artifact_bytes = selected.read_bytes() if selected else b""
                (run_dir / "artifact.png").write_bytes(artifact_bytes)
                passes, report = self._validate_image(task, artifact_bytes)
            else:
                if self.planner == "ce-plan":
                    artifact, assembly_costs = assemble_ce(
                        logger=logger,
                        task=task,
                        orchestrator=orchestrator,
                        step=assembly_step,
                        results=results,
                        client=self.client,
                        dry_run=self.dry_run,
                    )
                else:
                    artifact, assembly_costs = assemble_raw(
                        logger=logger,
                        task=task,
                        orchestrator=orchestrator,
                        step=assembly_step,
                        results=results,
                        client=self.client,
                        dry_run=self.dry_run,
                    )
                ext = _artifact_ext(task.type)
                (run_dir / f"artifact{ext}").write_text(artifact)
                ledger.add_many(assembly_costs)
                passes, report = self._validate(task, artifact)

            # 4. Judge (optional — text artifacts only)
            if judge is not None and not is_image:
                judge_step = assembly_step + 3
                judge_result, judge_costs = judge_artifact(
                    logger=logger,
                    step=judge_step,
                    task=task,
                    artifact=artifact,
                    judge=judge,
                    client=self.client,
                    dry_run=self.dry_run,
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
            meta.finished_at = datetime.now(timezone.utc).isoformat()
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
                meta.finished_at = datetime.now(timezone.utc).isoformat()
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
            if self.client is not None:
                self.client.close()

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
