#!/usr/bin/env python3
"""orchestral CLI: run orchestrator × worker evals and report results."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, find_task, load_models, load_task, load_yaml
from orchestral.planners import available_prompt_variants, load_prompt_variant
from orchestral.privacy import scrub_all
from orchestral.reporter import generate_dashboard, generate_html_report, model_history
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.tui import run_tui


def _model_map(models_dir: str) -> dict[str, ModelConfig]:
    return {m.slug: m for m in load_models(models_dir)}


def _model_from_arg(slug: str, models_dir: str = "models", known: dict[str, ModelConfig] | None = None) -> ModelConfig:
    cfg = (known if known is not None else _model_map(models_dir)).get(slug)
    if cfg is not None:
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


def _judge_from_arg(args: argparse.Namespace, known: dict[str, ModelConfig] | None = None) -> ModelConfig | None:
    judge = _model_from_arg(args.judge, args.models_dir, known) if getattr(args, "judge", None) else None
    return replace(judge, role="judge") if judge is not None else None


def _check_prompt_variant(args: argparse.Namespace) -> None:
    """Fail fast on an unknown --prompt-variant before any run starts."""
    variant = getattr(args, "prompt_variant", None)
    if not variant:
        return
    try:
        load_prompt_variant(variant)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def _run_preamble(args: argparse.Namespace) -> tuple[RunStore, dict[str, ModelConfig], ModelConfig | None]:
    """Shared command preamble: API-key guard, prompt-variant check, model map,
    one RunStore (primes WAL before parallel runners), and the optional judge."""
    if not args.dry_run and not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Pass --dry-run to test the harness without calling OpenRouter.")
        sys.exit(1)
    if getattr(args, "retry_limit", None) is not None and not 0 <= args.retry_limit <= 10:
        print("--retry-limit must be between 0 and 10", file=sys.stderr)
        sys.exit(1)
    _check_prompt_variant(args)
    store = RunStore(args.runs_dir)
    known = _model_map(args.models_dir)
    return store, known, _judge_from_arg(args, known)


def _runner_kwargs(args: argparse.Namespace, store: RunStore, **extra: Any) -> dict[str, Any]:
    return {
        "dry_run": args.dry_run,
        "planner": args.planner,
        "runs_dir": args.runs_dir,
        "prompt_variant": getattr(args, "prompt_variant", None),
        "use_judge_cache": not getattr(args, "no_judge_cache", False),
        "store": store,
        **extra,
    }


def _apply_retry_limit(worker: ModelConfig, args: argparse.Namespace) -> ModelConfig:
    if getattr(args, "retry_limit", None) is not None:
        worker = replace(worker, retry_limit=args.retry_limit)
    return worker


def cmd_init(args: argparse.Namespace) -> None:
    store = RunStore(args.runs_dir)
    print(f"Run store ready at {store.root}")
    print(f"Index at {store.db}")


def cmd_run(args: argparse.Namespace) -> None:
    store, known, judge = _run_preamble(args)
    task = load_task(_task_from_arg(args.task, args.tasks_dir))
    orchestrator = replace(_model_from_arg(args.orchestrator, args.models_dir, known), role="orchestrator")
    worker = _apply_retry_limit(replace(_model_from_arg(args.worker, args.models_dir, known), role="worker"), args)

    meta = Runner(**_runner_kwargs(args, store)).run(task, orchestrator, worker, judge)
    if args.json:
        print(json.dumps(meta.to_dict(), indent=2, default=str))
        return
    print(f"Run {meta.run_id} {meta.status}")
    print(f"  Directory: {meta.run_dir}")
    print(f"  Cost: ${meta.total_cost_usd:.6f} | Tokens: {meta.total_input_tokens + meta.total_output_tokens}")
    print(f"  Passes: {meta.passes} | Score: {meta.score}")


def cmd_grid(args: argparse.Namespace) -> None:
    store, known, judge = _run_preamble(args)
    task = load_task(_task_from_arg(args.task, args.tasks_dir))

    def _configured_workers() -> list[ModelConfig]:
        pool = [m for m in known.values() if m.role == "worker"]
        if task.type == "image":
            # only workers that can generate images
            return [m for m in pool if m.supports("image")]
        # image-only workers can't produce text artifacts; a multimodal worker
        # (modalities includes text) or one with no modalities stays eligible
        return [m for m in pool if not m.metadata.get("modalities") or m.supports("text")]

    if args.orchestrators:
        orchestrators = [_model_from_arg(s, args.models_dir, known) for s in _slugs_from_arg(args.orchestrators)]
    else:
        orchestrators = [m for m in known.values() if m.role == "orchestrator"]
    if args.workers:
        workers = [_model_from_arg(s, args.models_dir, known) for s in _slugs_from_arg(args.workers)]
    else:
        workers = _configured_workers()
    if not orchestrators or not workers:
        print("No orchestrator/worker models configured for this task type. Pass --orchestrators and --workers, or add role/modalities fields in models/*.yaml.")
        sys.exit(1)
    results: list[dict[str, Any]] = []

    pairings = [(o, w) for o in orchestrators for w in workers]

    def _one(orchestrator: ModelConfig, worker: ModelConfig) -> dict[str, Any]:
        # copy per pairing — ModelConfig objects from `known` are shared across threads
        orchestrator = replace(orchestrator, role="orchestrator")
        worker = _apply_retry_limit(replace(worker, role="worker"), args)
        meta = Runner(**_runner_kwargs(args, store)).run(task, orchestrator, worker, judge)
        return {
            "orchestrator": orchestrator.slug,
            "worker": worker.slug,
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    failures = 0
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, o, w): (o.slug, w.slug) for o, w in pairings}
            for fut in as_completed(futures):
                o_slug, w_slug = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    failures += 1
                    print(f"[fail] {o_slug} × {w_slug}: {exc}", file=sys.stderr)
        results.sort(key=lambda r: (r["orchestrator"], r["worker"]))
    else:
        for o, w in pairings:
            try:
                results.append(_one(o, w))
            except Exception as exc:
                failures += 1
                print(f"[fail] {o.slug} × {w.slug}: {exc}", file=sys.stderr)

    print("\nGrid summary")
    print(f"{'orchestrator':<40} {'worker':<40} {'cost':>10} {'tokens':>8} {'pass':>6} {'score':>6}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        print(f"{r['orchestrator']:<40} {r['worker']:<40} ${r['cost']:.6f} {r['tokens']:>8} {str(r['passes']):>6} {score:>6}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    if failures:
        sys.exit(1)


def _task_index(tasks_dir: str) -> dict[str, Path]:
    """One-pass task id -> path index for resolving many ids at once."""
    index: dict[str, Path] = {}
    for f in sorted(Path(tasks_dir).rglob("*.yaml")):
        data = load_yaml(f)
        if isinstance(data, dict) and data.get("id"):
            index[data["id"]] = f
    return index


def cmd_batch(args: argparse.Namespace) -> None:
    store, known, judge = _run_preamble(args)

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
        index = _task_index(args.tasks_dir)
        for t in args.batch_tasks:
            p = index.get(t) or (Path(t) if Path(t).exists() else None)
            if p is None:
                raise FileNotFoundError(f"No task found for id '{t}' in {args.tasks_dir}")
            paths.append(p)

    orchestrator = replace(_model_from_arg(args.orchestrator, args.models_dir, known), role="orchestrator")
    worker = _apply_retry_limit(replace(_model_from_arg(args.worker, args.models_dir, known), role="worker"), args)

    results: list[dict[str, Any]] = []

    def _one(path: Path) -> dict[str, Any]:
        task = load_task(path)
        meta = Runner(**_runner_kwargs(args, store)).run(task, orchestrator, worker, judge)
        return {
            "task_id": task.id,
            "task_path": str(path),
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    failures = 0
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, p): p for p in paths}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    failures += 1
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
    if failures:
        sys.exit(1)


# Supported ablation knobs: name -> value caster
SWEEP_KNOBS = {"retry_limit": int, "prompt_variant": str}
MAX_SWEEP_VALUES = 8


def cmd_ablate(args: argparse.Namespace) -> None:
    knob, sep, raw = args.sweep.partition("=")
    if not sep or knob not in SWEEP_KNOBS:
        print(f"--sweep must be <knob>=<csv> with knob one of: {', '.join(SWEEP_KNOBS)}", file=sys.stderr)
        sys.exit(1)
    caster = SWEEP_KNOBS[knob]
    try:
        values = [caster(v) for v in _slugs_from_arg(raw)]
    except ValueError:
        print(f"Invalid value in --sweep {args.sweep}: expected {caster.__name__}", file=sys.stderr)
        sys.exit(1)
    if knob == "retry_limit" and any(not 0 <= v <= 10 for v in values):
        print("retry_limit sweep values must be between 0 and 10", file=sys.stderr)
        sys.exit(1)
    if not values or len(values) > MAX_SWEEP_VALUES:
        print(f"--sweep needs 1-{MAX_SWEEP_VALUES} values", file=sys.stderr)
        sys.exit(1)
    if knob == "prompt_variant":
        known_variants = available_prompt_variants()
        bad = [v for v in values if v not in known_variants]
        if bad:
            print(f"Unknown prompt variant(s) {bad}. Available: {', '.join(known_variants) or '(none)'}", file=sys.stderr)
            sys.exit(1)

    store, known, judge = _run_preamble(args)
    task = load_task(_task_from_arg(args.task, args.tasks_dir))
    orchestrator = replace(_model_from_arg(args.orchestrator, args.models_dir, known), role="orchestrator")
    base_worker = _model_from_arg(args.worker, args.models_dir, known)

    def _one(value: Any) -> dict[str, Any]:
        worker = replace(base_worker, role="worker", retry_limit=value) if knob == "retry_limit" else _apply_retry_limit(replace(base_worker, role="worker"), args)
        kwargs = _runner_kwargs(args, store, sweep={"knob": knob, "value": value})
        if knob == "prompt_variant":
            kwargs["prompt_variant"] = value
        meta = Runner(**kwargs).run(task, orchestrator, worker, judge)
        return {
            "value": value,
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    results: list[dict[str, Any]] = []
    failures = 0
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, v): v for v in values}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    failures += 1
                    print(f"[fail] {knob}={futures[fut]}: {exc}", file=sys.stderr)
    else:
        for v in values:
            try:
                results.append(_one(v))
            except Exception as exc:
                failures += 1
                print(f"[fail] {knob}={v}: {exc}", file=sys.stderr)
    order = {v: i for i, v in enumerate(values)}
    results.sort(key=lambda r: order[r["value"]])

    print(f"\nAblation: {knob} on {task.id} ({orchestrator.slug} → {base_worker.slug})")
    print(f"{knob:<16} {'cost':>10} {'tokens':>8} {'pass':>6} {'score':>6}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        print(f"{str(r['value']):<16} ${r['cost']:.6f} {r['tokens']:>8} {str(r['passes']):>6} {score:>6}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    if failures:
        sys.exit(1)


def cmd_history(args: argparse.Namespace) -> None:
    store = RunStore(args.runs_dir)
    runs = store.list_runs(
        orchestrator=args.orchestrator,
        worker=args.worker,
        limit=None,
    )
    history = model_history(runs)
    for role in ("orchestrator", "worker", "judge"):
        table = history[role]
        if not table:
            continue
        print(f"\n{role.capitalize()} history")
        print(f"{'model':<45} {'runs':>5} {'pass%':>7} {'avg score':>9} {'avg cost':>11} {'total cost':>11}")
        for name, s in sorted(table.items(), key=lambda kv: -kv[1]["total_cost"]):
            pass_pct = f"{s['pass_rate'] * 100:.1f}%" if s["pass_rate"] is not None else "-"
            score = f"{s['avg_score']:.2f}" if s["avg_score"] is not None else "-"
            print(f"{name:<45} {s['runs']:>5} {pass_pct:>7} {score:>9} ${s['avg_cost']:>10.6f} ${s['total_cost']:>10.4f}")


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
        # only trust avg score when every run in the group was judged;
        # a partial score set would hide unscored runs' pass results
        avg_score = sum(scored) / len(scored) if len(scored) == len(group) and scored else None
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


def cmd_shots(args: argparse.Namespace) -> None:
    from orchestral.shots import ScreenshotUnavailable, browser_session, capture_run

    store = RunStore(args.runs_dir)
    runs = store.list_runs(task_id=args.task, limit=None)
    captured = current = failed = no_artifact = 0
    try:
        with browser_session() as browser:
            for r in runs:
                try:
                    _, status = capture_run(r.run_dir, force=args.all, browser=browser)
                except ScreenshotUnavailable as exc:
                    failed += 1
                    if failed == 1:
                        print(f"Screenshot unavailable: {exc}")
                    continue
                if status == "captured":
                    captured += 1
                elif status == "current":
                    current += 1
                else:
                    no_artifact += 1
    except ScreenshotUnavailable as exc:
        print(f"Screenshot unavailable: {exc}")
        return
    print(f"screenshots: {captured} captured, {current} already current, {no_artifact} no HTML artifact, {failed} unavailable")


def main() -> None:
    p = argparse.ArgumentParser(description="orchestral eval harness")
    p.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    p.add_argument("--tasks-dir", default="tasks", help="Task spec directory")
    p.add_argument("--models-dir", default="models", help="Model config directory")
    sub = p.add_subparsers(dest="cmd")

    init = sub.add_parser("init", help="Create the runs directory and SQLite index")
    init.set_defaults(func=cmd_init)

    def _add_run_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--planner", default="raw", choices=["raw", "ce-plan"], help="Orchestrator planning strategy: raw or ce-plan")
        sp.add_argument("--judge", default=None, help="OpenRouter model slug for an optional LLM-as-judge pass (vision-capable for image tasks)")
        sp.add_argument("--no-judge-cache", action="store_true", help="Bypass judge result cache reads (still writes)")
        sp.add_argument("--retry-limit", type=int, default=None, help="Override the worker's retry_limit for this invocation")
        sp.add_argument("--prompt-variant", default=None, help="Orchestrator prompt variant from prompts/orchestrator-<name>.md")
        sp.add_argument("--dry-run", action="store_true", help="Do not call OpenRouter; generate sample data for storage testing")
        sp.add_argument("--json", action="store_true", help="Output the summary as JSON")

    run = sub.add_parser("run", help="Run one orchestrator × worker pairing")
    run.add_argument("--task", required=True, help="Task id or path")
    run.add_argument("--orchestrator", required=True, help="OpenRouter model slug for the orchestrator")
    run.add_argument("--worker", required=True, help="OpenRouter model slug for the worker")
    _add_run_flags(run)
    run.set_defaults(func=cmd_run)

    grid = sub.add_parser("grid", help="Run a matrix of orchestrators × workers")
    grid.add_argument("--task", required=True, help="Task id or path")
    grid.add_argument("--orchestrators", default=None, help="Comma-separated OpenRouter model slugs (default: all models with role=orchestrator)")
    grid.add_argument("--workers", default=None, help="Comma-separated OpenRouter model slugs (default: all models with role=worker)")
    grid.add_argument("--jobs", type=int, default=1, help="Run pairings in parallel with N workers")
    _add_run_flags(grid)
    grid.set_defaults(func=cmd_grid)

    batch = sub.add_parser("batch", help="Run one orchestrator × worker pairing across many tasks")
    batch.add_argument("--batch-dir", default=None, help="Directory of task .yaml files to run")
    batch.add_argument("--batch-tasks", nargs="+", default=None, help="Task ids or paths to run")
    batch.add_argument("--orchestrator", required=True, help="OpenRouter model slug for the orchestrator")
    batch.add_argument("--worker", required=True, help="OpenRouter model slug for the worker")
    batch.add_argument("--jobs", type=int, default=1, help="Run tasks in parallel with N workers")
    _add_run_flags(batch)
    batch.set_defaults(func=cmd_batch)

    ablate = sub.add_parser("ablate", help="Sweep one knob (retry_limit, prompt_variant) for a pairing")
    ablate.add_argument("--task", required=True, help="Task id or path")
    ablate.add_argument("--orchestrator", required=True, help="OpenRouter model slug for the orchestrator")
    ablate.add_argument("--worker", required=True, help="OpenRouter model slug for the worker")
    ablate.add_argument("--sweep", required=True, help="knob=v1,v2,... (knobs: retry_limit, prompt_variant)")
    ablate.add_argument("--jobs", type=int, default=1, help="Run sweep points in parallel with N workers")
    _add_run_flags(ablate)
    ablate.set_defaults(func=cmd_ablate)

    history = sub.add_parser("history", help="Per-model aggregate history across all stored runs")
    history.add_argument("--orchestrator", help="Filter by orchestrator")
    history.add_argument("--worker", help="Filter by worker")
    history.set_defaults(func=cmd_history)

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

    shots = sub.add_parser("shots", help="Screenshot HTML artifacts in stored runs (requires playwright extra)")
    shots.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    shots.add_argument("--task", default=None, help="Only capture runs for this task id")
    shots.add_argument("--all", action="store_true", help="Re-capture even when screenshots are current")
    shots.set_defaults(func=cmd_shots)

    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
