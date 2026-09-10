"""Orchestrator -> worker -> assemble -> validate loop with full logging."""

from __future__ import annotations

import html.parser
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.costs import CostLedger
from orchestral.logger import EventLogger
from orchestral.openrouter import OpenRouterClient
from orchestral.planners import assemble_ce, assemble_raw, delegate, plan_ce, plan_raw
from orchestral.storage import RunMeta, RunStore


class ValidationError(Exception):
    """Raised when a run fails structural validation."""


class Runner:
    def __init__(self, *, dry_run: bool = False, planner: str = "raw"):
        self.dry_run = dry_run
        self.planner = planner
        self.store = RunStore()
        self.client: OpenRouterClient | None = None
        if not dry_run:
            self.client = OpenRouterClient()

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
            results: list[dict[str, Any]] = []
            subtasks = plan.get("subtasks") or plan.get("sections", {}).get("subtasks", [])
            for i, sub in enumerate(subtasks):
                # models sometimes return a list of strings; normalize to dicts
                if isinstance(sub, str):
                    sub = {"id": i, "description": sub}
                elif not isinstance(sub, dict):
                    sub = {"id": i, "description": str(sub)}
                out, worker_costs = delegate(
                    logger=logger,
                    step=i + 3,
                    subtask=sub,
                    worker=worker,
                    client=self.client,
                    dry_run=self.dry_run,
                )
                results.append(out)
                (run_dir / f"worker-{i}.json").write_text(json.dumps(out, indent=2, default=str))
                ledger.add_many(worker_costs)

            # 3. Assemble final artifact
            assembly_step = 3 + len(subtasks)
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

            # 4. Validate
            passes, report = self._validate(task, artifact)
            (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))

            # 5. Final accounting
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
            if self.client is not None:
                self.client.close()

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
