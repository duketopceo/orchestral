#!/usr/bin/env python3
"""orchestral CLI: run orchestrator × worker evals and report results."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from orchestral.config import ModelConfig, find_task, load_models, load_task
from orchestral.privacy import scrub_all
from orchestral.reporter import generate_html_report
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model_from_arg(slug: str, models_dir: str = "models") -> ModelConfig:
    for cfg in load_models(models_dir):
        if cfg.slug == slug:
            return cfg
    # If the slug is not in the config, treat it as an ad-hoc model with cheap defaults.
    return ModelConfig(
        slug=slug,
        name=slug,
        role="unknown",
        input_price_per_mtok=0.03,
        output_price_per_mtok=0.10,
    )


def _task_from_arg(task_id: str, tasks_dir: str = "tasks") -> Path:
    path = find_task(task_id, tasks_dir)
    if path is None:
        # allow full file path
        p = Path(task_id)
        if p.exists():
            return p
        raise FileNotFoundError(f"No task found for id '{task_id}' in {tasks_dir}")
    return path


def cmd_init(args: argparse.Namespace) -> None:
    store = RunStore(args.runs_dir)
    print(f"Run store ready at {store.root}")
    print(f"Index at {store.db}")


def cmd_run(args: argparse.Namespace) -> None:
    if not args.dry_run and not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Pass --dry-run to test the harness without calling OpenRouter.")
        sys.exit(1)

    task_path = _task_from_arg(args.task, args.tasks_dir)
    task = load_task(task_path)
    orchestrator = _model_from_arg(args.orchestrator, args.models_dir)
    worker = _model_from_arg(args.worker, args.models_dir)
    orchestrator.role = "orchestrator"
    worker.role = "worker"

    runner = Runner(dry_run=args.dry_run, planner=args.planner)
    meta = runner.run(task, orchestrator, worker)
    print(f"Run {meta.run_id} {meta.status}")
    print(f"  Directory: {meta.run_dir}")
    print(f"  Cost: ${meta.total_cost_usd:.6f} | Tokens: {meta.total_input_tokens + meta.total_output_tokens}")
    print(f"  Passes: {meta.passes} | Score: {meta.score}")


def cmd_report(args: argparse.Namespace) -> None:
    if args.html:
        path = generate_html_report(args.runs_dir, args.reports_dir)
        print(f"HTML report generated: {path}")
        print("Open it in a browser, or run: python -m http.server -d reports 8080")
        return

    store = RunStore(args.runs_dir)
    runs = store.list_runs(
        orchestrator=args.orchestrator,
        worker=args.worker,
        task_id=args.task,
        order_by=args.sort,
        descending=args.desc,
        limit=args.limit,
    )

    if args.json:
        out = [
            {
                "run_id": r.run_id,
                "orchestrator": r.orchestrator,
                "task_id": r.task_id,
                "worker": r.worker,
                "status": r.status,
                "total_cost_usd": r.total_cost_usd,
                "score": r.score,
                "passes": r.passes,
                "run_dir": r.run_dir,
            }
            for r in runs
        ]
        print(json.dumps(out, indent=2, default=str))
        return

    print(f"{'run_id':<13} {'planner':<10} {'orchestrator':<30} {'task':<20} {'worker':<35} {'cost':>10} {'tokens':>7} {'pass':>5}")
    print("-" * 145)
    for r in runs:
        planner = r.config.get("planner", "raw") if r.config else "raw"
        pass_label = str(r.passes) if r.passes is not None else "-"
        tokens = r.total_input_tokens + r.total_output_tokens
        print(f"{r.run_id:<13} {planner:<10} {r.orchestrator:<30} {r.task_id:<20} {r.worker:<35} ${r.total_cost_usd:>8.4f} {tokens:>7} {pass_label:>5}")

    print()
    summary = store.summary()
    print(f"Total runs: {summary['runs']} | Total cost: ${summary['total_cost_usd']:.4f} | Total tokens: {summary['total_tokens']}")


def cmd_scrub(args: argparse.Namespace) -> None:
    copied = scrub_all(Path(args.runs_dir), Path(args.scrub_dir))
    print(f"Scrubbed {len(copied)} runs to {args.scrub_dir}")
    for c in copied:
        print(f"  {c}")


def main() -> None:
    p = argparse.ArgumentParser(description="orchestral eval harness")
    p.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    p.add_argument("--tasks-dir", default="tasks", help="Task spec directory")
    p.add_argument("--models-dir", default="models", help="Model config directory")
    sub = p.add_subparsers(dest="cmd")

    init = sub.add_parser("init", help="Create the runs directory and SQLite index")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="Run one orchestrator × worker pairing")
    run.add_argument("--task", required=True, help="Task id or path")
    run.add_argument("--orchestrator", required=True, help="OpenRouter model slug for the orchestrator")
    run.add_argument("--worker", required=True, help="OpenRouter model slug for the worker")
    run.add_argument("--planner", default="raw", choices=["raw", "ce-plan"], help="Orchestrator planning strategy: raw or ce-plan")
    run.add_argument("--dry-run", action="store_true", help="Do not call OpenRouter; generate sample data for storage testing")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="List and compare stored runs")
    report.add_argument("--html", action="store_true", help="Generate a static HTML drill-down report in reports_dir")
    report.add_argument("--reports-dir", default="reports", help="Output directory for HTML reports")
    report.add_argument("--task", help="Filter by task id")
    report.add_argument("--orchestrator", help="Filter by orchestrator")
    report.add_argument("--worker", help="Filter by worker")
    report.add_argument("--sort", default="started_at", help="Column to sort by")
    report.add_argument("--desc", action="store_true", default=True, help="Sort descending")
    report.add_argument("--limit", type=int, default=None, help="Limit number of rows")
    report.add_argument("--json", action="store_true", help="Output as JSON")
    report.set_defaults(func=cmd_report)

    scrub = sub.add_parser("scrub", help="Redact sensitive data from all runs for sharing")
    scrub.add_argument("--runs-dir", default="runs", help="Source runs directory")
    scrub.add_argument("--scrub-dir", default="runs-pub", help="Where to write scrubbed runs")
    scrub.set_defaults(func=cmd_scrub)

    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
