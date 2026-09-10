"""Orchestrator -> worker -> assemble -> validate loop with full logging."""

from __future__ import annotations

import html.parser
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec
from orchestral.logger import EventLogger
from orchestral.storage import RunMeta, RunStore


class ValidationError(Exception):
    """Raised when a run fails structural validation."""


class Runner:
    def __init__(self, *, dry_run: bool = False):
        self.dry_run = dry_run
        self.store = RunStore()

    def run(self, task: TaskSpec, orchestrator: ModelConfig, worker: ModelConfig) -> RunMeta:
        run_id, run_dir = self.store.new_run(
            orchestrator.slug,
            task.id,
            worker.slug,
            config={
                "dry_run": self.dry_run,
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
            input_data={"task": task.id, "orchestrator": orchestrator.slug, "worker": worker.slug},
            output_data={"run_id": run_id, "dry_run": self.dry_run},
            reasoning="Initialised run directory, SQLite index, and event log.",
        )

        cost_breakdown: list[dict[str, Any]] = []
        meta: RunMeta | None = None

        try:
            # 1. Plan
            plan, plan_cost = self._plan(logger, task, orchestrator)
            (run_dir / "plan.json").write_text(json.dumps(plan, indent=2, default=str))
            cost_breakdown.append(plan_cost)

            # 2. Delegate each subtask to the worker
            results: list[dict[str, Any]] = []
            for i, sub in enumerate(plan.get("subtasks", [])):
                out, worker_cost = self._delegate(logger, i, sub, worker)
                results.append(out)
                (run_dir / f"worker-{i}.json").write_text(json.dumps(out, indent=2, default=str))
                cost_breakdown.append(worker_cost)

            # 3. Assemble final artifact
            artifact, assembly_cost = self._assemble(logger, task, orchestrator, results)
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
                step=len(cost_breakdown),
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

    def _plan(self, logger: EventLogger, task: TaskSpec, orchestrator: ModelConfig) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.dry_run:
            raise NotImplementedError("Real OpenRouter calls are not wired yet.")

        # dry-run: produce a deterministic-looking plan
        plan = {
            "task_id": task.id,
            "orchestrator": orchestrator.slug,
            "subtasks": [
                {"id": 0, "description": "Write the HTML head and page structure"},
                {"id": 1, "description": "Write the hero section"},
                {"id": 2, "description": "Write the signup form"},
            ],
            "reasoning": "Decomposed the landing page into structure, hero, and form subtasks so a cheap worker can write each independently.",
        }
        cost = self._fake_call(logger, phase="plan", step=1, model=orchestrator.slug, role="orchestrator", input_data={"prompt": task.prompt}, output_data=plan, reasoning=plan["reasoning"])
        return plan, cost

    def _delegate(self, logger: EventLogger, step: int, subtask: dict[str, Any], worker: ModelConfig) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.dry_run:
            raise NotImplementedError("Real OpenRouter calls are not wired yet.")

        output = {
            "subtask_id": subtask["id"],
            "content": f"<!-- worker output for subtask {subtask['id']}: {subtask['description']} -->",
            "notes": "Dry-run worker output.",
        }
        cost = self._fake_call(
            logger,
            phase="delegate",
            step=step + 2,
            model=worker.slug,
            role="worker",
            input_data={"subtask": subtask},
            output_data=output,
            reasoning=f"Executed subtask {subtask['id']} with {worker.slug}.",
        )
        return output, cost

    def _assemble(self, logger: EventLogger, task: TaskSpec, orchestrator: ModelConfig, results: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        if not self.dry_run:
            raise NotImplementedError("Real OpenRouter calls are not wired yet.")

        pieces = [r["content"] for r in results]
        artifact = _build_html(task.prompt, pieces)
        cost = self._fake_call(
            logger,
            phase="assemble",
            step=len(results) + 2,
            model=orchestrator.slug,
            role="orchestrator",
            input_data={"worker_outputs": results},
            output_data={"artifact_length": len(artifact)},
            reasoning="Merged worker outputs into a single HTML artifact.",
        )
        return artifact, cost

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

        report: dict[str, Any] = {
            "task_id": task.id,
            "artifact_length": len(artifact),
            "checks": {"parses": not parser.errors, "title": "<title>" in artifact, "non_empty": bool(artifact.strip())},
            "errors": parser.errors + errors,
            # score is a stub until LLM-as-judge or human grading
            "score": None,
        }
        passes = not (parser.errors or errors)
        return passes, report

    def _fake_call(
        self,
        logger: EventLogger,
        phase: str,
        step: int,
        model: str,
        role: str,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        reasoning: str,
    ) -> dict[str, Any]:
        """Simulate an LLM call with deterministic-ish token counts and cost."""
        # Naive token estimate: ~1 token per 4 chars
        input_chars = len(json.dumps(input_data, default=str))
        output_chars = len(json.dumps(output_data, default=str))
        input_tokens = max(100, input_chars // 4 + random.randint(0, 20))
        output_tokens = max(50, output_chars // 4 + random.randint(0, 20))

        # parse the model slug to get a price per token from the task config
        price_in, price_out = _prices_from_slug(model)
        cost_usd = (input_tokens * price_in) + (output_tokens * price_out)

        logger.log_llm_call(
            phase=phase,
            step=step,
            model=model,
            role=role,
            messages=[{"role": "user", "content": json.dumps(input_data)}],
            completion=output_data,
            reasoning=reasoning,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=random.uniform(80, 1200),
        )

        return {
            "phase": phase,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }


def _artifact_ext(task_type: str) -> str:
    return {"html": ".html", "image": ".png", "video": ".mp4", "api": ".json", "multi-file": ".zip"}.get(task_type, ".txt")


def _build_html(prompt: str, pieces: list[str]) -> str:
    body = "\n".join(f"<section>{p}</section>" for p in pieces)
    return (
        "<!doctype html>\n"
        "<html lang='en'>\n"
        "<head><meta charset='utf-8'><title>orchestral dry-run</title></head>\n"
        f"<body>\n<h1>{html.escape(prompt[:80])}</h1>\n{body}\n</body>\n"
        "</html>"
    )


class _HTMLValidator(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def handle_starttag(self, tag, attrs):
        pass


def _prices_from_slug(slug: str) -> tuple[float, float]:
    """Return cheap placeholder prices per token for dry-run cost math."""
    if "deepseek" in slug.lower():
        return 0.03 / 1_000_000, 0.10 / 1_000_000
    if "glm" in slug.lower():
        return 0.075 / 1_000_000, 0.25 / 1_000_000
    if "fable" in slug.lower():
        return 10.0 / 1_000_000, 50.0 / 1_000_000
    return 1.0 / 1_000_000, 3.0 / 1_000_000
