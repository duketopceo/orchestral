#!/usr/bin/env python3
"""orchestral CLI: run orchestrator × worker evals and report results."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.calibrate import agreement_metrics, collect_pairs, load_labels
from orchestral.config import (
    ConfigError,
    ModelConfig,
    find_task,
    load_models,
    load_task,
    load_yaml,
)
from orchestral.export import leaderboard_csv, run_audit_markdown, runs_csv
from orchestral.fileset import expected_paths, member_requirements
from orchestral.planners import available_prompt_variants, load_prompt_variant
from orchestral.pricing import DEFAULT_DRIFT_THRESHOLD, pricing_drift
from orchestral.privacy import scrub_all
from orchestral.providers import provider_for, provider_key
from orchestral.reporter import generate_dashboard, generate_html_report, model_history
from orchestral.runner import Runner
from orchestral.stats import aggregate, pairing_leaderboard
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


def _eligible_workers(pool: list[ModelConfig], task_type: str) -> list[ModelConfig]:
    """Filter a worker pool to models that can produce the task's artifact.

    Media tasks need a model declaring the modality; text tasks accept
    multimodal or modality-free workers but not media-only ones.
    """
    if task_type in ("image", "video"):
        return [m for m in pool if m.supports(task_type)]
    return [m for m in pool if not m.metadata.get("modalities") or m.supports("text")]


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
    """Shared command preamble: prompt-variant check, model map,
    one RunStore (primes WAL before parallel runners), and the optional judge."""
    if getattr(args, "retry_limit", None) is not None and not 0 <= args.retry_limit <= 10:
        print("--retry-limit must be between 0 and 10", file=sys.stderr)
        sys.exit(1)
    replicates = getattr(args, "replicates", None)
    if replicates is not None and replicates < 1:
        print("--replicates must be at least 1", file=sys.stderr)
        sys.exit(1)
    if replicates and replicates > 1 and getattr(args, "replicate", None) is not None:
        print("--replicate and --replicates are mutually exclusive", file=sys.stderr)
        sys.exit(1)
    _check_prompt_variant(args)
    store = RunStore(args.runs_dir)
    known = _model_map(args.models_dir)
    return store, known, _judge_from_arg(args, known)


def _check_provider_envs(args: argparse.Namespace, *models: ModelConfig | None) -> None:
    """Fail fast naming every API-key env var the selected models' providers need."""
    if args.dry_run:
        return
    missing = sorted({
        env
        for m in models
        if m is not None
        for env in [provider_key(m)[2]]
        if env and not os.environ.get(env)
    })
    if missing:
        print(
            f"API key env var(s) not set: {', '.join(missing)}. "
            "Set them for the configured providers, or pass --dry-run.",
            file=sys.stderr,
        )
        sys.exit(1)


def _runner_kwargs(args: argparse.Namespace, store: RunStore, **extra: Any) -> dict[str, Any]:
    return {
        "dry_run": args.dry_run,
        "planner": args.planner,
        "runs_dir": args.runs_dir,
        "prompt_variant": getattr(args, "prompt_variant", None),
        "use_judge_cache": not getattr(args, "no_judge_cache", False),
        "run_group": getattr(args, "group", None),
        "replicate": getattr(args, "replicate", None),
        "seed": getattr(args, "seed", None),
        "verbose": getattr(args, "verbose", False),
        "store": store,
        **extra,
    }


def _apply_retry_limit(worker: ModelConfig, args: argparse.Namespace) -> ModelConfig:
    if getattr(args, "retry_limit", None) is not None:
        worker = replace(worker, retry_limit=args.retry_limit)
    return worker


def _resolve_replicates(args: argparse.Namespace) -> tuple[str | None, int]:
    """Return (run_group, count). Auto-names a group when N > 1 and none
    was given so every replicate of the invocation shares a label."""
    n = getattr(args, "replicates", None) or 1  # preamble already rejects n < 1
    group = getattr(args, "group", None)
    if n > 1 and not group:
        # uuid suffix: two invocations in the same second must not merge cells
        group = f"rep-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    return group, n


def _rep_kwargs(args: argparse.Namespace, store: RunStore, group: str | None, i: int | None) -> dict[str, Any]:
    """Runner kwargs for replicate `i` (None when not replicating). When
    --seed S is set, replicate i records seed S+i-1 — varying seeds measure
    variance; identical-seed reruns need separate invocations."""
    kwargs = _runner_kwargs(args, store, run_group=group, replicate=i)
    seed = getattr(args, "seed", None)
    if seed is not None and i is not None:
        kwargs["seed"] = seed + i - 1
    return kwargs


def cmd_init(args: argparse.Namespace) -> None:
    store = RunStore(args.runs_dir)
    print(f"Run store ready at {store.root}")
    print(f"Index at {store.db}")


# metadata keys each task type cannot function without — checked by `validate`
_REQUIRED_META: dict[str, tuple[str, ...]] = {
    "api": ("stub", "calls"),
    "sql": ("schema", "reference_sql"),
    "needle": ("document", "expected_answer"),
    "extract": ("expected", "fields"),
    "terminal": ("fs", "expect"),
    "swe-patch": ("files", "tests"),
    "bugfix": ("files", "tests"),
    "code": ("module", "tests"),
    "multi-file": ("expected_paths",),
}
# validation names that read a metadata key — requesting the check without
# the metadata silently no-ops or errors at run time
_VALIDATION_META = {
    "has_required": "required",
    "member_required": "member_required",
    "no_forbidden": "forbidden",
    "exact_answer": "expected_answer",
    "matches_pattern": "pattern",
    "within_budget": ("min_chars", "max_chars", "min_words", "max_words"),
}
# check names each validator actually implements — a typo'd name fails the run
# at validation time (validators fail closed on unknowns) but validate should
# catch it before a grid spends money on it. Execution-graded types
# (code/bugfix/swe-patch/sql/extract/api/terminal) ignore validation: by design.
_TEXT_CHECKS = {
    "html", "html_parses", "non_empty", "has_title", "has_cta", "has_form",
    "has_viewport", "no_placeholder", "within_budget", "has_required",
    "no_forbidden", "exact_answer", "matches_pattern", "no_pattern",
}
_CHECK_NAMES = {
    "html": _TEXT_CHECKS,
    "constraint": _TEXT_CHECKS,
    "needle": _TEXT_CHECKS,
    "pipeline": _TEXT_CHECKS,
    "multi-file": {"non_empty", "zip_signature", "has_paths", "member_required"},
    "image": {"non_empty", "png_signature"},
    "video": {"non_empty", "mp4_signature"},
}


def cmd_validate(args: argparse.Namespace) -> None:
    """Parse every spec and check per-type contracts — catches the silent
    failures that otherwise only surface mid-run (missing metadata, a
    validation name with no backing key, unparseable YAML)."""
    failures = 0
    for path in sorted(Path(args.tasks_dir).rglob("*.yaml")):
        try:
            task = load_task(path)
        except Exception as exc:
            failures += 1
            print(f"  FAIL {path.name}: {exc}")
            continue
        missing = [k for k in _REQUIRED_META.get(task.type, ()) if k not in task.metadata]
        try:
            expected_paths(task.metadata)
            member_requirements(task.metadata)
        except Exception as exc:
            missing.append(f"path sanitizer: {exc}")
        known = _CHECK_NAMES.get(task.type)
        if known is not None:
            for check in sorted(set(task.validation) - known):
                missing.append(f"unknown validation check '{check}' for type {task.type}")
        for check in task.validation:
            want = _VALIDATION_META.get(check)
            if isinstance(want, str):
                want = (want,)
            if want and not any(task.metadata.get(k) for k in want):
                missing.append(f"{want[0]} (required by validation '{check}')")
        if missing:
            failures += 1
            print(f"  FAIL {path.name}: missing metadata {', '.join(missing)}")
        else:
            print(f"  ok   {path.name} ({task.type})")
    try:
        load_models(args.models_dir)
    except Exception as exc:
        failures += 1
        print(f"  FAIL {args.models_dir}: {exc}")
    print(f"{failures} spec problem(s)" if failures else "All specs valid")
    sys.exit(1 if failures else 0)


def cmd_run(args: argparse.Namespace) -> None:
    store, known, judge = _run_preamble(args)
    task = load_task(_task_from_arg(args.task, args.tasks_dir))
    orchestrator = replace(_model_from_arg(args.orchestrator, args.models_dir, known), role="orchestrator")
    worker = _apply_retry_limit(replace(_model_from_arg(args.worker, args.models_dir, known), role="worker"), args)
    _check_provider_envs(args, orchestrator, worker, judge)

    group, n_reps = _resolve_replicates(args)
    metas = []
    failures = 0
    for i in range(1, n_reps + 1):
        rep = i if n_reps > 1 else getattr(args, "replicate", None)
        try:
            metas.append(Runner(**_rep_kwargs(args, store, group, rep)).run(task, orchestrator, worker, judge))
        except Exception as exc:
            # Runner.record_failure persists the failed run before re-raising;
            # keep going so one flake doesn't lose the remaining replicates
            failures += 1
            print(f"[fail] rep {i}: {exc}", file=sys.stderr)
    if args.json:
        out: Any = [m.to_dict() for m in metas] if n_reps > 1 else (metas[0].to_dict() if metas else None)
        print(json.dumps(out, indent=2, default=str))
    else:
        for meta in metas:
            label = f"Run {meta.run_id} {meta.status}"
            if n_reps > 1:
                label += f"  [rep {meta.replicate}/{n_reps}]"
            print(label)
            print(f"  Directory: {meta.run_dir}")
            print(f"  Cost: ${meta.total_cost_usd:.6f} | Tokens: {meta.total_input_tokens + meta.total_output_tokens} | Latency: {meta.latency_ms:.0f}ms")
            print(f"  Passes: {meta.passes} | Score: {meta.score} | Failure: {meta.failure_reason or '-'}")
        if n_reps > 1:
            cells = aggregate(store.list_runs(run_group=group, task_id=task.id))
            for cell in cells:
                score = f"{cell.score_mean:.2f}±{cell.score_sd:.2f}" if cell.score_mean is not None else "-"
                print(f"\nReplicate summary ({cell.run_group or group}, n={cell.runs})")
                print(f"  pass rate: {cell.pass_rate:.0%} | score: {score} | cost: ${cell.cost_mean:.6f}±${cell.cost_sd:.6f}")
                if cell.failures:
                    print(f"  failures: {cell.failures}")
    if failures:
        sys.exit(1)


def cmd_grid(args: argparse.Namespace) -> None:
    store, known, judge = _run_preamble(args)
    task = load_task(_task_from_arg(args.task, args.tasks_dir))

    def _configured_workers() -> list[ModelConfig]:
        pool = [m for m in known.values() if m.role == "worker"]
        return _eligible_workers(pool, task.type)

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
    _check_provider_envs(args, *orchestrators, *workers, judge)
    results: list[dict[str, Any]] = []

    group, n_reps = _resolve_replicates(args)
    cells = [(o, w, i) for o in orchestrators for w in workers for i in range(1, n_reps + 1)]

    def _one(orchestrator: ModelConfig, worker: ModelConfig, rep: int) -> dict[str, Any]:
        # copy per pairing — ModelConfig objects from `known` are shared across threads
        orchestrator = replace(orchestrator, role="orchestrator")
        worker = _apply_retry_limit(replace(worker, role="worker"), args)
        kwargs = _rep_kwargs(args, store, group, rep if n_reps > 1 else getattr(args, "replicate", None))
        meta = Runner(**kwargs).run(task, orchestrator, worker, judge)
        return {
            "orchestrator": orchestrator.slug,
            "worker": worker.slug,
            "replicate": rep if n_reps > 1 else meta.replicate,
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    failures = 0
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, o, w, i): (o.slug, w.slug, i) for o, w, i in cells}
            for fut in as_completed(futures):
                o_slug, w_slug, i = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    failures += 1
                    print(f"[fail] {o_slug} × {w_slug} rep {i}: {exc}", file=sys.stderr)
        results.sort(key=lambda r: (r["orchestrator"], r["worker"], r["replicate"] or 0))
    else:
        for o, w, i in cells:
            try:
                results.append(_one(o, w, i))
            except Exception as exc:
                failures += 1
                print(f"[fail] {o.slug} × {w.slug} rep {i}: {exc}", file=sys.stderr)

    print("\nGrid summary")
    rep_col = f"{'rep':>4} " if n_reps > 1 else ""
    print(f"{'orchestrator':<40} {'worker':<40} {rep_col}{'cost':>10} {'tokens':>8} {'pass':>6} {'score':>6}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        rep = f"{r['replicate']:>4} " if n_reps > 1 else ""
        print(f"{r['orchestrator']:<40} {r['worker']:<40} {rep}${r['cost']:.6f} {r['tokens']:>8} {r['passes']!s:>6} {score:>6}")

    if n_reps > 1:
        for cell in aggregate(store.list_runs(run_group=group, task_id=task.id)):
            score = f"{cell.score_mean:.2f}±{cell.score_sd:.2f}" if cell.score_mean is not None else "-"
            print(f"[{group}] {cell.orchestrator} × {cell.worker}: n={cell.runs} pass={cell.pass_rate:.0%} score={score} cost=${cell.cost_mean:.6f}±${cell.cost_sd:.6f}")

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
    _check_provider_envs(args, orchestrator, worker, judge)

    results: list[dict[str, Any]] = []

    group, n_reps = _resolve_replicates(args)
    cells = [(p, i) for p in paths for i in range(1, n_reps + 1)]

    def _one(path: Path, rep: int) -> dict[str, Any]:
        task = load_task(path)
        kwargs = _rep_kwargs(args, store, group, rep if n_reps > 1 else getattr(args, "replicate", None))
        meta = Runner(**kwargs).run(task, orchestrator, worker, judge)
        return {
            "task_id": task.id,
            "task_path": str(path),
            "replicate": rep if n_reps > 1 else meta.replicate,
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    failures = 0
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, p, i): (p, i) for p, i in cells}
            for fut in as_completed(futures):
                p, i = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    failures += 1
                    print(f"[fail] {p} rep {i}: {exc}", file=sys.stderr)
        results.sort(key=lambda r: (r["task_id"], r["replicate"] or 0))
    else:
        for path, i in cells:
            try:
                results.append(_one(path, i))
            except Exception as exc:
                failures += 1
                print(f"[fail] {path} rep {i}: {exc}", file=sys.stderr)

    print(f"\nBatch summary ({len(results)} runs across {len(paths)} tasks)")
    rep_col = f"{'rep':>4} " if n_reps > 1 else ""
    print(f"{'task_id':<30} {rep_col}{'cost':>10} {'tokens':>8} {'pass':>6} {'score':>6}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        rep = f"{r['replicate']:>4} " if n_reps > 1 else ""
        print(f"{r['task_id']:<30} {rep}${r['cost']:.6f} {r['tokens']:>8} {r['passes']!s:>6} {score:>6}")

    if n_reps > 1:
        for cell in aggregate(store.list_runs(run_group=group)):
            score = f"{cell.score_mean:.2f}±{cell.score_sd:.2f}" if cell.score_mean is not None else "-"
            print(f"[{group}] {cell.task_id}: n={cell.runs} pass={cell.pass_rate:.0%} score={score} cost=${cell.cost_mean:.6f}±${cell.cost_sd:.6f}")

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
    _check_provider_envs(args, orchestrator, base_worker, judge)

    group, n_reps = _resolve_replicates(args)

    def _one(value: Any, rep: int) -> dict[str, Any]:
        worker = replace(base_worker, role="worker", retry_limit=value) if knob == "retry_limit" else _apply_retry_limit(replace(base_worker, role="worker"), args)
        kwargs = _rep_kwargs(args, store, group, rep if n_reps > 1 else getattr(args, "replicate", None))
        kwargs["sweep"] = {"knob": knob, "value": value}
        if knob == "prompt_variant":
            kwargs["prompt_variant"] = value
        meta = Runner(**kwargs).run(task, orchestrator, worker, judge)
        return {
            "value": value,
            "replicate": rep if n_reps > 1 else meta.replicate,
            "passes": meta.passes,
            "score": meta.score,
            "cost": meta.total_cost_usd,
            "tokens": meta.total_input_tokens + meta.total_output_tokens,
            "run_id": meta.run_id,
        }

    results: list[dict[str, Any]] = []
    cells = [(v, i) for v in values for i in range(1, n_reps + 1)]
    failures = 0
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_one, v, i): (v, i) for v, i in cells}
            for fut in as_completed(futures):
                v, i = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    failures += 1
                    print(f"[fail] {knob}={v} rep {i}: {exc}", file=sys.stderr)
    else:
        for v, i in cells:
            try:
                results.append(_one(v, i))
            except Exception as exc:
                failures += 1
                print(f"[fail] {knob}={v} rep {i}: {exc}", file=sys.stderr)
    order = {v: i for i, v in enumerate(values)}
    results.sort(key=lambda r: (order[r["value"]], r["replicate"] or 0))

    print(f"\nAblation: {knob} on {task.id} ({orchestrator.slug} → {base_worker.slug})")
    rep_col = f"{'rep':>4} " if n_reps > 1 else ""
    print(f"{knob:<16} {rep_col}{'cost':>10} {'tokens':>8} {'pass':>6} {'score':>6}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        rep = f"{r['replicate']:>4} " if n_reps > 1 else ""
        print(f"{r['value']!s:<16} {rep}${r['cost']:.6f} {r['tokens']:>8} {r['passes']!s:>6} {score:>6}")

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
    if getattr(args, "compare", None):
        _print_group_delta(store, args.compare, json_out=args.json)
        return
    runs = store.list_runs(
        orchestrator=args.orchestrator,
        worker=args.worker,
        task_id=args.task,
        run_group=args.group,
        order_by=args.sort,
        descending=args.desc,
        limit=args.limit,
    )

    if getattr(args, "leaderboard", False):
        min_samples = getattr(args, "min_samples", 10)
        rows = pairing_leaderboard(runs, min_samples=min_samples)
        if args.json:
            print(json.dumps([r.to_dict() for r in rows], indent=2, default=str))
            return
        _print_leaderboard(rows, min_samples)
        return

    if args.pairings:
        _print_pairing_table(runs)
        return

    if args.groups:
        cells = aggregate(runs)
        if args.json:
            print(json.dumps([c.to_dict() for c in cells], indent=2, default=str))
            return
        _print_groups_table(cells)
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
                "latency_ms": r.latency_ms,
                "failure_reason": r.failure_reason,
                "run_group": r.run_group,
                "replicate": r.replicate,
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


def _print_leaderboard(rows: list[Any], min_samples: int) -> None:
    """Pairing leaderboard — one row per (orchestrator, worker)."""
    if not rows:
        print("No runs match.")
        return
    print(f"{'orchestrator':<30} {'worker':<30} {'n':>3} {'tasks':>5} {'pass%':>6} {'med score':>9} {'med cost':>9} {'med ms':>8} {'fail%':>6} {'$/pass':>9}")
    print("-" * 120)
    for p in rows:
        flag = "" if not p.low_sample else " *"
        score = f"{p.score_median:.2f}" if p.score_median is not None else "-"
        cpp = f"${p.cost_per_pass:.4f}" if p.cost_per_pass is not None else "-"
        print(
            f"{p.orchestrator:<30} {p.worker:<30} {p.runs:>3}{flag} {p.tasks_covered:>5} "
            f"{(p.pass_rate or 0) * 100:>5.0f}% {score:>9} ${p.cost_median:>8.4f} "
            f"{p.duration_median_ms:>8.0f} {(p.failure_rate or 0) * 100:>5.0f}% {cpp:>9}"
        )
    if any(p.low_sample for p in rows):
        print(f"\n* fewer than {min_samples} runs — treat the ranking as anecdotal, not evidence.")


def _print_groups_table(cells: list[Any]) -> None:
    """One row per (group, task, orchestrator, worker) cell with variance."""
    if not cells:
        print("No runs match.")
        return
    print(f"{'group':<18} {'task':<18} {'orchestrator':<26} {'worker':<26} {'n':>3} {'pass%':>6} {'score±sd':>12} {'cost±sd':>16} {'p50ms':>8} {'p95ms':>8} {'succ/$':>9} {'failures':<20}")
    print("-" * 175)
    for c in cells:
        score = f"{c.score_mean:.2f}±{c.score_sd:.2f}" if c.score_mean is not None else "-"
        cost = f"${c.cost_mean:.4f}±${c.cost_sd:.4f}"
        spd = f"{c.successes_per_dollar:.0f}" if c.successes_per_dollar is not None else "-"
        fails = ",".join(f"{k.split(':')[-1]}×{v}" for k, v in sorted(c.failures.items()))[:20]
        print(f"{c.run_group or '-':<18} {c.task_id:<18} {c.orchestrator:<26} {c.worker:<26} {c.runs:>3} {c.pass_rate * 100:>5.0f}% {score:>12} {cost:>16} {c.latency_p50:>8.0f} {c.latency_p95:>8.0f} {spd:>9} {fails:<20}")


def _print_group_delta(store: RunStore, spec: str, *, json_out: bool = False) -> None:
    """Compare two run groups cell-by-cell — the v1→v2 evidence question.

    Cells join on (task, orchestrator, worker); each side keeps its own n so
    drift in grid shape is visible rather than silently interpolated."""
    names = _slugs_from_arg(spec)
    if len(names) != 2:
        print("--compare takes exactly two comma-separated run_group names", file=sys.stderr)
        sys.exit(1)
    group_a, group_b = names

    def cells(group: str) -> dict[tuple[str, str, str], Any]:
        return {
            (c.task_id, c.orchestrator, c.worker): c
            for c in aggregate(store.list_runs(run_group=group))
        }

    cells_a, cells_b = cells(group_a), cells(group_b)
    keys = sorted(set(cells_a) | set(cells_b))
    if not keys:
        print(f"No runs in either group ({group_a}, {group_b}).")
        return
    rows: list[dict[str, Any]] = []
    for task_id, orch, worker in keys:
        a, b = cells_a.get((task_id, orch, worker)), cells_b.get((task_id, orch, worker))
        pa = a.pass_rate if a else None
        pb = b.pass_rate if b else None
        if pa is None or pb is None:
            verdict = "one-sided"
        elif pb > pa:
            verdict = "improved"
        elif pb < pa:
            verdict = "regressed"
        else:
            verdict = "stable"
        rows.append({
            "task_id": task_id, "orchestrator": orch, "worker": worker,
            "n_a": a.runs if a else 0, "pass_a": pa, "cost_a": a.cost_total if a else None,
            "n_b": b.runs if b else 0, "pass_b": pb, "cost_b": b.cost_total if b else None,
            "verdict": verdict,
            "failures_a": a.failures if a else {}, "failures_b": b.failures if b else {},
        })
    if json_out:
        print(json.dumps({"group_a": group_a, "group_b": group_b, "cells": rows}, indent=2, default=str))
        return
    print(f"Delta {group_a} -> {group_b}  (cells joined on task x orchestrator x worker)")
    print(f"{'task':<22} {'orchestrator':<24} {'worker':<24} {'n':>7} {'pass':>11} {'delta':>7} {'verdict':<10}")
    print("-" * 115)
    for r in rows:
        n = f"{r['n_a']}/{r['n_b']}"
        pass_a = f"{r['pass_a'] * 100:.0f}%" if r['pass_a'] is not None else "-"
        pass_b = f"{r['pass_b'] * 100:.0f}%" if r['pass_b'] is not None else "-"
        delta = "-" if r["verdict"] == "one-sided" else f"{(r['pass_b'] - r['pass_a']) * 100:+.0f}pp"
        print(f"{r['task_id']:<22} {r['orchestrator']:<24} {r['worker']:<24} {n:>7} {pass_a:>5}->{pass_b:<5} {delta:>7} {r['verdict']:<10}")
    counts = Counter(r["verdict"] for r in rows)
    total_a = sum(r["cost_a"] or 0 for r in rows)
    total_b = sum(r["cost_b"] or 0 for r in rows)
    print("-" * 115)
    print("Verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Cost: {group_a}=${total_a:.4f}  {group_b}=${total_b:.4f}")


def cmd_export(args: argparse.Namespace) -> None:
    """Export run data: CSV for analysis, Markdown for human audit."""
    store = RunStore(args.runs_dir)
    out_path = Path(args.out) if args.out else None

    if args.run:
        meta = store.get_run(args.run)
        if meta is None:
            print(f"No run found with id {args.run}", file=sys.stderr)
            sys.exit(1)
        if args.format == "jsonl":
            src = Path(meta.run_dir) / "events.jsonl"
            content = src.read_text(encoding="utf-8") if src.exists() else ""
        else:
            content = run_audit_markdown(meta.run_dir)
    elif args.leaderboard:
        rows = pairing_leaderboard(store.list_runs(limit=None), min_samples=args.min_samples)
        content = leaderboard_csv(rows)
    else:
        content = runs_csv(store.list_runs(limit=None))

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"Exported to {out_path}")
    else:
        print(content, end="")


def cmd_prices(args: argparse.Namespace) -> None:
    """Pricing drift: provider-reported cost vs the configured rate card."""
    store = RunStore(args.runs_dir)
    rows = pricing_drift(
        store.calls_pricing_summary(),
        _model_map(args.models_dir),
        threshold=args.threshold,
    )
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2, default=str))
        return
    if not rows:
        print("No calls indexed yet.")
        return
    print(f"{'model':<38} {'calls':>5} {'api':>4} {'api $':>10} {'cfg $':>10} {'ratio':>7}  drift")
    print("-" * 90)
    for r in rows:
        cfg = f"${r.configured_cost_usd:.4f}" if r.configured_cost_usd is not None else "-"
        ratio = f"{r.ratio:.2f}x" if r.ratio is not None else "-"
        flag = "STALE?" if r.drifted else (r.note or "ok")
        print(f"{r.model:<38} {r.calls:>5} {r.api_calls:>4} ${r.api_cost_usd:>9.4f} {cfg:>10} {ratio:>7}  {flag}")
    drifted = [r for r in rows if r.drifted]
    if drifted:
        print(f"\n{len(drifted)} model(s) beyond {args.threshold:.0%} drift — update models/*.yaml or check for silent rerouting.")

def cmd_calibrate(args: argparse.Namespace) -> None:
    """Judge-vs-human agreement metrics from a labels file."""
    labels = load_labels(args.labels)
    result = collect_pairs(RunStore(args.runs_dir), labels)
    metrics = agreement_metrics(result["pairs"])
    out = {"coverage": result["coverage"], "metrics": metrics}
    if args.json:
        print(json.dumps(out, indent=2))
        return
    cov = result["coverage"]
    print(f"Labeled: {cov['labeled']} | matched to runs: {cov['matched']} | unmatched: {len(cov['unmatched'])}")
    if cov["unmatched"]:
        print(f"  unmatched run_ids: {', '.join(cov['unmatched'][:10])}")
    score = metrics.get("score")
    if score:
        print(f"\nScore agreement (n={score['n']}):")
        print(f"  MAE {score['mae']:.3f} | Pearson {score['pearson'] if score['pearson'] is not None else 'n/a'} | "
              f"Spearman {score['spearman'] if score['spearman'] is not None else 'n/a'}")
        print(f"  human mean {score['human_mean']:.3f} | judge mean {score['judge_mean']:.3f}")
    verdict = metrics.get("verdict")
    if verdict:
        print(f"\nVerdict agreement (n={verdict['n']}):")
        print(f"  accuracy {verdict['accuracy']:.1%} | Cohen's kappa {verdict['kappa'] if verdict['kappa'] is not None else 'n/a'}")
        print(f"  tp {verdict['tp']} | tn {verdict['tn']} | fp {verdict['fp']} | fn {verdict['fn']}")
    if not score and not verdict:
        print("\nNo overlapping pairs — label runs that have judge scores/verdicts.")
        print("Labels file format:")
        print("  labels:\n    - run_id: <prefix>\n      score: 0.8\n      passed: true")


def cmd_review(args: argparse.Namespace) -> None:
    """Frontier-model audit of archived run evidence."""
    from orchestral.review import run_review_batch

    model = _model_from_arg(args.model, args.models_dir)
    client = None if args.dry_run else provider_for(model)
    try:
        result = run_review_batch(
            RunStore(args.runs_dir), model, client,
            run_group=args.group, task_id=args.task,
            orchestrator=args.orchestrator, worker=args.worker,
            limit=args.limit, dry_run=args.dry_run, force=args.force,
            reports_dir=Path(args.reports_dir), tasks_dir=Path(args.tasks_dir),
        )
    finally:
        if client is not None:
            client.close()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    syn = result["corpus"]["synthesis"]
    print(f"reviewed {result['reviewed']} runs ({result['skipped']} already reviewed) — ${result['cost_usd']:.4f}")
    print(f"quality: {result['corpus']['stats']['run_quality']}")
    for iss in syn.get("systemic_issues") or []:
        print(f"  [{iss.get('severity')}] {iss.get('issue')} ({iss.get('run_count')} runs) — {iss.get('fix')}")
    print(f"\nverdict: {syn.get('verdict', '')}")
    print("full report: reports/review-*.md | per-run: <run_dir>/review.json")


def cmd_judge(args: argparse.Namespace) -> None:
    """Retroactively judge artifacts of finished runs (score axis without re-running)."""
    from orchestral.judge import backfill_judgments

    judge = _judge_from_arg(args)
    if judge is None:
        print("error: --judge is required (a model slug configured in models/)")
        raise SystemExit(1)
    _check_provider_envs(args, judge)
    client = None if args.dry_run else provider_for(judge)
    try:
        result = backfill_judgments(
            RunStore(args.runs_dir), judge, client,
            run_group=args.group, task_id=args.task,
            orchestrator=args.orchestrator, worker=args.worker,
            limit=args.limit, jobs=args.jobs, dry_run=args.dry_run,
            force=args.force, tasks_dir=Path(args.tasks_dir),
        )
    finally:
        if client is not None:
            client.close()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"judged {result['judged']} runs with {result['judge']} "
          f"({result['skipped']} skipped, {result['dry_run_judged']} dry-run stubs)")
    scored = [r["score"] for r in result["results"] if r.get("score") is not None]
    if scored:
        print(f"score: mean {sum(scored)/len(scored):.2f} over {len(scored)} judged artifacts")


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
    run_tui(
        runs_dir=args.runs_dir,
        tasks_dir=args.tasks_dir,
        models_dir=args.models_dir,
        reports_dir=args.reports_dir,
        refresh=args.refresh,
    )


def cmd_serve(args: argparse.Namespace) -> None:
    from orchestral.web import run_server

    run_server(
        runs_dir=args.runs_dir,
        tasks_dir=args.tasks_dir,
        models_dir=args.models_dir,
        port=args.port,
        open_browser=args.open,
    )


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

    validate = sub.add_parser("validate", help="Parse all task specs and model configs; check per-type metadata contracts")
    validate.add_argument("--tasks-dir", default="tasks")
    validate.add_argument("--models-dir", default="models")
    validate.set_defaults(func=cmd_validate)

    def _add_run_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--planner", default="raw", choices=["raw", "ce-plan"], help="Orchestrator planning strategy: raw or ce-plan")
        sp.add_argument("--judge", default=None, help="OpenRouter model slug for an optional LLM-as-judge pass (vision-capable for image tasks)")
        sp.add_argument("--no-judge-cache", action="store_true", help="Bypass judge result cache reads (still writes)")
        sp.add_argument("--retry-limit", type=int, default=None, help="Override the worker's retry_limit for this invocation")
        sp.add_argument("--prompt-variant", default=None, help="Orchestrator prompt variant from prompts/orchestrator-<name>.md")
        sp.add_argument("--dry-run", action="store_true", help="Do not call OpenRouter; generate sample data for storage testing")
        sp.add_argument("--json", action="store_true", help="Output the summary as JSON")
        sp.add_argument("--verbose", "-v", action="store_true", help="Echo events and debug records to stderr while running")
        sp.add_argument("--group", default=None, help="Label this run with a group name for replicate/variance analysis")
        sp.add_argument("--replicate", type=int, default=None, help="Replicate index within --group")
        sp.add_argument("--replicates", type=int, default=1, help="Run each cell N times under one --group for variance analysis")
        sp.add_argument("--seed", type=int, default=None, help="Record a seed label on the run config")

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
    report.add_argument("--leaderboard", action="store_true", help="Pairing leaderboard: pass rate, medians, cost per pass, failure rate")
    report.add_argument("--min-samples", type=int, default=10, help="Leaderboard sample-size floor for the low-evidence flag")
    report.add_argument("--groups", action="store_true", help="Aggregate by run_group × task × pairing with variance stats")
    report.add_argument("--group", default=None, help="Only include runs from this run_group")
    report.add_argument("--compare", default=None, metavar="A,B", help="Compare two run groups cell-by-cell (pass-rate delta per task × pairing)")
    report.add_argument("--limit", type=int, default=None, help="Limit number of rows")
    report.add_argument("--json", action="store_true", help="Output as JSON")
    report.set_defaults(func=cmd_report)

    prices = sub.add_parser("prices", help="Pricing drift check — provider-reported cost vs configured rate card")
    prices.add_argument("--threshold", type=float, default=DEFAULT_DRIFT_THRESHOLD, help="Drift fraction that flags a model (default 0.15)")
    prices.add_argument("--json", action="store_true", help="Emit JSON")
    prices.set_defaults(func=cmd_prices)

    export = sub.add_parser("export", help="Export runs as CSV, or a single run as Markdown/JSONL")
    export.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    export.add_argument("--format", choices=["csv", "md", "jsonl"], default="csv", help="Export format")
    export.add_argument("--run", default=None, help="Export a single run id (md audit or jsonl events)")
    export.add_argument("--leaderboard", action="store_true", help="Export pairing leaderboard as CSV")
    export.add_argument("--min-samples", type=int, default=10, help="Leaderboard low-evidence floor")
    export.add_argument("--out", default=None, help="Write to this file instead of stdout")
    export.set_defaults(func=cmd_export)

    scrub = sub.add_parser("scrub", help="Redact sensitive data from all runs for sharing")
    scrub.add_argument("--runs-dir", default="runs", help="Source runs directory")
    scrub.add_argument("--scrub-dir", default="runs-pub", help="Where to write scrubbed runs")
    scrub.set_defaults(func=cmd_scrub)

    dashboard = sub.add_parser("dashboard", help="Generate a unified stats dashboard")
    dashboard.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    dashboard.add_argument("--reports-dir", default="reports", help="Output directory for HTML reports")
    dashboard.set_defaults(func=cmd_dashboard)

    tui = sub.add_parser("tui", help="Interactive experiment observatory (needs the [tui] extra)")
    tui.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    tui.add_argument("--reports-dir", default="reports", help="Output directory for exports")
    tui.add_argument("--refresh", action="store_true", help="Auto-refresh every 5s")
    tui.set_defaults(func=cmd_tui)

    shots = sub.add_parser("shots", help="Screenshot HTML artifacts in stored runs (requires playwright extra)")
    shots.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    shots.add_argument("--task", default=None, help="Only capture runs for this task id")
    shots.add_argument("--all", action="store_true", help="Re-capture even when screenshots are current")
    shots.set_defaults(func=cmd_shots)

    calibrate = sub.add_parser("calibrate", help="Judge-vs-human agreement metrics from a labels file")
    calibrate.add_argument("--labels", required=True, help="YAML labels file (labels: [{run_id, score, passed}])")
    calibrate.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    calibrate.add_argument("--json", action="store_true", help="Machine-readable output")
    calibrate.set_defaults(func=cmd_calibrate)

    review = sub.add_parser("review", help="Frontier-model audit of archived run evidence (writes review.json per run + reports/review-*.md)")
    review.add_argument("--model", default="x-ai/grok-4.3", help="Reviewer model slug (default: x-ai/grok-4.3 — reasoning tier)")
    review.add_argument("--group", default=None, help="Only review runs in this run_group")
    review.add_argument("--task", default=None, help="Only review runs for this task")
    review.add_argument("--orchestrator", default=None)
    review.add_argument("--worker", default=None)
    review.add_argument("--limit", type=int, default=None, help="Cap the number of runs reviewed")
    review.add_argument("--force", action="store_true", help="Re-review runs that already have review.json")
    review.add_argument("--dry-run", action="store_true", help="Write stub reviews without calling the provider")
    review.add_argument("--reports-dir", default="reports", help="Output directory for the corpus report")
    review.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    review.set_defaults(func=cmd_review)

    judge = sub.add_parser("judge", help="Retroactively judge artifacts of finished runs (writes judge result into report.json + index score)")
    judge.add_argument("--judge", required=True, help="Judge model slug (e.g. moonshotai/kimi-k2, x-ai/grok-4.3)")
    judge.add_argument("--group", default=None, help="Only judge runs in this run_group")
    judge.add_argument("--task", default=None, help="Only judge runs for this task")
    judge.add_argument("--orchestrator", default=None)
    judge.add_argument("--worker", default=None)
    judge.add_argument("--limit", type=int, default=None, help="Cap the number of runs judged")
    judge.add_argument("--jobs", type=int, default=4, help="Parallel judge calls")
    judge.add_argument("--force", action="store_true", help="Re-judge runs that already have a judge result")
    judge.add_argument("--dry-run", action="store_true", help="Exercise the path without calling the provider or writing results")
    judge.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    judge.set_defaults(func=cmd_judge)

    serve = sub.add_parser("serve", help="Local web observatory — browse, launch, and cancel runs in a browser (localhost only)")
    serve.add_argument("--port", type=int, default=8787, help="Port to bind on 127.0.0.1 (default 8787)")
    serve.add_argument("--open", action="store_true", help="Open the observatory in a browser")
    serve.set_defaults(func=cmd_serve)

    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        return
    try:
        args.func(args)
    except ConfigError as exc:
        sys.exit(f"error: {exc}")
    except FileNotFoundError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
