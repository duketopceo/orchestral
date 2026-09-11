#!/usr/bin/env python3
"""orchestral CLI: run orchestrator × worker evals and report results."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, find_task, load_models, load_task
from orchestral.privacy import scrub_all
from orchestral.reporter import generate_dashboard, generate_html_report
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.tui import run_tui


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


def _slugs_from_arg(arg: str) -> list[str]:
    return [s.strip() for s in arg.split(",") if s.strip()]


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
    judge = _model_from_arg(args.judge, args.models_dir) if args.judge else None
    if judge is not None:
        judge.role = "judge"

    runner = Runner(dry_run=args.dry_run, planner=args.planner, runs_dir=args.runs_dir)
    meta = runner.run(task, orchestrator, worker, judge)
    print(f"Run {meta.run_id} {meta.status}")
    print(f"  Directory: {meta.run_dir}")
    print(f"  Cost: ${meta.total_cost_usd:.6f} | Tokens: {meta.total_input_tokens + meta.total_output_tokens}")
    print(f"  Passes: {meta.passes} | Score: {meta.score}")


def cmd_grid(args: argparse.Namespace) -> None:
    if not args.dry_run and not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Pass --dry-run to test the harness without calling OpenRouter.")
        sys.exit(1)

    task_path = _task_from_arg(args.task, args.tasks_dir)
    task = load_task(task_path)
    RunStore(args.runs_dir)  # ensure WAL mode is set before parallel runners connect

    if args.orchestrators and args.workers:
        orchestrators = [_model_from_arg(s, args.models_dir) for s in _slugs_from_arg(args.orchestrators)]
        workers = [_model_from_arg(s, args.models_dir) for s in _slugs_from_arg(args.workers)]
    else:
        configured = load_models(args.models_dir)
        orchestrators = [m for m in configured if m.role == "orchestrator"]
        workers = [m for m in configured if m.role == "worker"]
        if not orchestrators or not workers:
            print("No orchestrator/worker models configured. Pass --orchestrators and --workers, or add role fields in models/*.yaml.")
            sys.exit(1)
    results: list[dict[str, Any]] = []

    pairings = [(o, w) for o in orchestrators for w in workers]

    def _one(orchestrator: ModelConfig, worker: ModelConfig) -> dict[str, Any]:
        orchestrator.role = "orchestrator"
        worker.role = "worker"
        runner = Runner(dry_run=args.dry_run, planner=args.planner, runs_dir=args.runs_dir)
        meta = runner.run(task, orchestrator, worker)
        return {
            "orchestrator": orchestrator.slug,
            "worker": worker.slug,
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, o, w): (o.slug, w.slug) for o, w in pairings}
            for fut in as_completed(futures):
                o_slug, w_slug = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    print(f"[fail] {o_slug} × {w_slug}: {exc}", file=sys.stderr)
        results.sort(key=lambda r: (r["orchestrator"], r["worker"]))
    else:
        for o, w in pairings:
            results.append(_one(o, w))

    print("\nGrid summary")
    print(f"{'orchestrator':<40} {'worker':<40} {'cost':>10} {'tokens':>8} {'pass':>6} {'score'}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        print(f"{r['orchestrator']:<40} {r['worker']:<40} ${r['cost']:.6f} {r['tokens']:>8} {str(r['passes']):>6} {score}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))


def cmd_batch(args: argparse.Namespace) -> None:
    if not args.dry_run and not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Pass --dry-run to test the harness without calling OpenRouter.")
        sys.exit(1)

    if not args.batch_dir and not args.batch_tasks:
        print("Pass either --batch-dir or --batch-tasks")
        sys.exit(1)

    paths: list[Path] = []
    if args.batch_dir:
        dir_path = Path(args.batch_dir)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"Batch directory not found: {args.batch_dir}")
        paths = sorted(dir_path.glob("*.yaml"))
        if not paths:
            raise FileNotFoundError(f"No .yaml task files in {args.batch_dir}")
    else:
        for t in args.batch_tasks:
            paths.append(_task_from_arg(t, args.tasks_dir))

    orchestrator = _model_from_arg(args.orchestrator, args.models_dir)
    worker = _model_from_arg(args.worker, args.models_dir)
    orchestrator.role = "orchestrator"
    worker.role = "worker"
    RunStore(args.runs_dir)  # ensure WAL mode is set before parallel runners connect

    results: list[dict[str, Any]] = []

    def _one(path: Path) -> dict[str, Any]:
        task = load_task(path)
        runner = Runner(dry_run=args.dry_run, planner=args.planner, runs_dir=args.runs_dir)
        meta = runner.run(task, orchestrator, worker)
        return {
            "task_id": task.id,
            "task_path": str(path),
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, p): p for p in paths}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    print(f"[fail] {futures[fut]}: {exc}", file=sys.stderr)
        results.sort(key=lambda r: r["task_id"])
    else:
        for path in paths:
            results.append(_one(path))

    print(f"\nBatch summary ({len(results)} tasks)")
    print(f"{'task_id':<30} {'cost':>10} {'tokens':>8} {'pass':>6} {'score':>6}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        print(f"{r['task_id']:<30} ${r['cost']:.6f} {r['tokens']:>8} {str(r['passes']):>6} {score:>6}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))


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

    if args.pairings:
        _print_pairing_table(runs)
        return

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


def _print_pairing_table(runs: list[Any]) -> None:
    """Aggregate runs by orchestrator × worker, sorted by quality per dollar."""
    groups: dict[tuple[str, str], list[Any]] = {}
    for r in runs:
        if r.status != "finished":
            continue
        groups.setdefault((r.orchestrator, r.worker), []).append(r)

    rows = []
    for (orch, work), group in groups.items():
        cost = sum(r.total_cost_usd for r in group)
        tokens = sum(r.total_input_tokens + r.total_output_tokens for r in group)
        passed = sum(1 for r in group if r.passes)
        scored = [r.score for r in group if r.score is not None]
        avg_score = sum(scored) / len(scored) if scored else None
        # quality = avg score if judged, else pass rate; per dollar of total spend
        quality = avg_score if avg_score is not None else passed / len(group)
        qpd = quality / cost if cost > 0 else float("inf")
        rows.append((orch, work, len(group), passed, avg_score, cost, tokens, qpd))

    rows.sort(key=lambda r: -r[7])
    print(f"{'orchestrator':<35} {'worker':<35} {'runs':>5} {'pass':>5} {'avg score':>9} {'total cost':>11} {'tokens':>8} {'quality/$':>10}")
    print("-" * 135)
    for orch, work, n, passed, avg_score, cost, tokens, qpd in rows:
        score = f"{avg_score:.2f}" if avg_score is not None else "-"
        print(f"{orch:<35} {work:<35} {n:>5} {passed:>5} {score:>9} ${cost:>10.4f} {tokens:>8} {qpd:>10.1f}")


def cmd_scrub(args: argparse.Namespace) -> None:
    copied = scrub_all(Path(args.runs_dir), Path(args.scrub_dir))
    print(f"Scrubbed {len(copied)} runs to {args.scrub_dir}")
    for c in copied:
        print(f"  {c}")


def cmd_dashboard(args: argparse.Namespace) -> None:
    path = generate_dashboard(args.runs_dir, args.reports_dir)
    print(f"Dashboard generated: {path}")
    print("Open it in a browser, or run: python -m http.server -d reports 8080")


def cmd_tui(args: argparse.Namespace) -> None:
    run_tui(runs_dir=args.runs_dir, refresh=args.refresh)


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
    run.add_argument("--judge", default=None, help="OpenRouter model slug for an optional LLM-as-judge pass")
    run.add_argument("--dry-run", action="store_true", help="Do not call OpenRouter; generate sample data for storage testing")
    run.set_defaults(func=cmd_run)

    grid = sub.add_parser("grid", help="Run a matrix of orchestrators × workers")
    grid.add_argument("--task", required=True, help="Task id or path")
    grid.add_argument("--orchestrators", default=None, help="Comma-separated OpenRouter model slugs (default: all models with role=orchestrator)")
    grid.add_argument("--workers", default=None, help="Comma-separated OpenRouter model slugs (default: all models with role=worker)")
    grid.add_argument("--planner", default="raw", choices=["raw", "ce-plan"], help="Orchestrator planning strategy")
    grid.add_argument("--jobs", type=int, default=1, help="Run pairings in parallel with N workers")
    grid.add_argument("--dry-run", action="store_true", help="Do not call OpenRouter; generate sample data for storage testing")
    grid.add_argument("--json", action="store_true", help="Output grid summary as JSON")
    grid.set_defaults(func=cmd_grid)

    batch = sub.add_parser("batch", help="Run one orchestrator × worker pairing across many tasks")
    batch.add_argument("--batch-dir", default=None, help="Directory of task .yaml files to run")
    batch.add_argument("--batch-tasks", nargs="+", default=None, help="Task ids or paths to run")
    batch.add_argument("--orchestrator", required=True, help="OpenRouter model slug for the orchestrator")
    batch.add_argument("--worker", required=True, help="OpenRouter model slug for the worker")
    batch.add_argument("--planner", default="raw", choices=["raw", "ce-plan"], help="Orchestrator planning strategy")
    batch.add_argument("--jobs", type=int, default=1, help="Run tasks in parallel with N workers")
    batch.add_argument("--dry-run", action="store_true", help="Do not call OpenRouter; generate sample data for storage testing")
    batch.add_argument("--json", action="store_true", help="Output batch summary as JSON")
    batch.set_defaults(func=cmd_batch)

    report = sub.add_parser("report", help="List and compare stored runs")
    report.add_argument("--html", action="store_true", help="Generate a static HTML drill-down report in reports_dir")
    report.add_argument("--reports-dir", default="reports", help="Output directory for HTML reports")
    report.add_argument("--task", help="Filter by task id")
    report.add_argument("--orchestrator", help="Filter by orchestrator")
    report.add_argument("--worker", help="Filter by worker")
    report.add_argument("--sort", default="started_at", help="Column to sort by")
    report.add_argument("--desc", action="store_true", default=True, help="Sort descending")
    report.add_argument("--pairings", action="store_true", help="Aggregate by orchestrator × worker, sorted by quality per dollar")
    report.add_argument("--limit", type=int, default=None, help="Limit number of rows")
    report.add_argument("--json", action="store_true", help="Output as JSON")
    report.set_defaults(func=cmd_report)

    scrub = sub.add_parser("scrub", help="Redact sensitive data from all runs for sharing")
    scrub.add_argument("--runs-dir", default="runs", help="Source runs directory")
    scrub.add_argument("--scrub-dir", default="runs-pub", help="Where to write scrubbed runs")
    scrub.set_defaults(func=cmd_scrub)

    dashboard = sub.add_parser("dashboard", help="Generate a unified stats dashboard")
    dashboard.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    dashboard.add_argument("--reports-dir", default="reports", help="Output directory for HTML reports")
    dashboard.set_defaults(func=cmd_dashboard)

    tui = sub.add_parser("tui", help="Render a terminal dashboard")
    tui.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    tui.add_argument("--refresh", action="store_true", help="Auto-refresh every 5s")
    tui.set_defaults(func=cmd_tui)

    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
