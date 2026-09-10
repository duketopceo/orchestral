"""Orchestrator -> worker -> assemble -> validate loop with full logging."""

from __future__ import annotations

import html.parser
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.logger import EventLogger
from orchestral.planners import assemble_ce, assemble_raw, plan_ce, plan_raw
from orchestral.storage import RunMeta, RunStore


class ValidationError(Exception):
    """Raised when a run fails structural validation."""


class Runner:
    def __init__(self, *, dry_run: bool = False, planner: str = "raw"):
        self.dry_run = dry_run
        self.planner = planner
        self.store = RunStore()

    def run(self, task: TaskSpec, orchestrator: ModelConfig, worker: ModelConfig) -> RunMeta:
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

        cost_breakdown: list[dict[str, Any]] = []
        meta: RunMeta | None = None

        try:
            # 1. Plan
            if self.planner == "ce-plan":
                plan, plan_cost = plan_ce(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=1,
                    dry_run=self.dry_run,
                )
            else:
                plan, plan_cost = plan_raw(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=1,
                    dry_run=self.dry_run,
                )
            (run_dir / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
            cost_breakdown.append(plan_cost)

            # 2. Delegate each subtask to the worker
            results: list[dict[str, Any]] = []
            subtasks = plan.get("subtasks") or plan.get("sections", {}).get("subtasks", [])
            for i, sub in enumerate(subtasks):
                out, worker_cost = self._delegate(logger, i + 3, sub, worker)
                results.append(out)
                (run_dir / f"worker-{i}.json").write_text(json.dumps(out, indent=2, default=str))
                cost_breakdown.append(worker_cost)

            # 3. Assemble final artifact
            assembly_step = 3 + len(subtasks)
            if self.planner == "ce-plan":
                artifact, assembly_cost = assemble_ce(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=assembly_step,
                    results=results,
                    dry_run=self.dry_run,
                )
            else:
                artifact, assembly_cost = assemble_raw(
                    logger=logger,
                    task=task,
                    orchestrator=orchestrator,
                    step=assembly_step,
                    results=results,
                    dry_run=self.dry_run,
                )
            ext = _artifact_ext(task.type)
            (run_dir / f"artifact{ext}").write_text(artifact)
            cost_breakdown.append(assembly_cost)

            # 4. Validate
            passes, report = self._validate(task, artifact)
            (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))

            # 5. Final accounting
            total_cost = sum(c["cost_usd"] for c in cost_breakdown)
            total_input = sum(c["input_tokens"] for c in cost_breakdown)
            total_output = sum(c["output_tokens"] for c in cost_breakdown)
            (run_dir / "cost.json").write_text(json.dumps(cost_breakdown, indent=2, default=str))

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
                step=assembly_step + 2,
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

    def _delegate(self, logger: EventLogger, step: int, subtask: dict[str, Any], worker: ModelConfig) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.dry_run:
            raise NotImplementedError("Real OpenRouter calls are not wired yet.")

        output = {
            "subtask_id": subtask["id"],
            "content": f"<!-- worker output for subtask {subtask['id']}: {subtask['description']} -->",
            "notes": "Dry-run worker output.",
        }

        input_chars = len(json.dumps(subtask, default=str))
        output_chars = len(json.dumps(output, default=str))
        input_tokens = max(50, input_chars // 4 + random.randint(0, 10))
        output_tokens = max(30, output_chars // 4 + random.randint(0, 10))
        price_in, price_out = _prices_from_slug(worker.slug)
        cost_usd = (input_tokens * price_in) + (output_tokens * price_out)

        logger.log_llm_call(
            phase="delegate",
            step=step,
            model=worker.slug,
            role="worker",
            messages=[{"role": "user", "content": json.dumps(subtask)}],
            completion=output,
            reasoning=f"Executed subtask {subtask['id']} with {worker.slug}.",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=random.uniform(80, 1200),
        )

        return output, {
            "phase": "delegate",
            "model": worker.slug,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }

    def _validate(self, task: TaskSpec, artifact: str) -> tuple[bool, dict[str, Any]]:
        errors: list[str] = []
        parser = _HTMLValidator()
        try:
            parser.feed(artifact)
        except Exception as exc:
            errors.append(f"HTML parse error: {exc}")

        if not artifact.strip():
            errors.append("Artifact is empty.")
        if "<title>" not in artifact:
            errors.append("Missing <title>.")
        if "meta name='viewport'" not in artifact and 'meta name="viewport"' not in artifact:
            errors.append("Missing viewport meta tag.")

        report: dict[str, Any] = {
            "task_id": task.id,
            "artifact_length": len(artifact),
            "checks": {
                "parses": not parser.errors,
                "title": "<title>" in artifact,
                "non_empty": bool(artifact.strip()),
                "viewport": "meta name='viewport'" in artifact or 'meta name="viewport"' in artifact,
            },
            "errors": parser.errors + errors,
            "score": None,
        }
        passes = not (parser.errors or errors)
        return passes, report


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


def _prices_from_slug(slug: str) -> tuple[float, float]:
    if "deepseek" in slug.lower():
        return 0.03 / 1_000_000, 0.10 / 1_000_000
    if "glm" in slug.lower():
        return 0.075 / 1_000_000, 0.25 / 1_000_000
    if "fable" in slug.lower():
        return 10.0 / 1_000_000, 50.0 / 1_000_000
    return 1.0 / 1_000_000, 3.0 / 1_000_000
