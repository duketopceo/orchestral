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

from orchestral.agentexec import ExecutorPreflightError, launch_gate
from orchestral.audit import audit_tree, default_holdout_probe
from orchestral.calibrate import (
    MIN_CALIBRATION_PAIRS,
    agreement_metrics,
    calibration_status,
    collect_pairs,
    emit_label_skeleton,
    load_labels,
    persist_calibration,
)
from orchestral.codeexec import CODE_RUNTIME_ENV, ISOLATED_CODE_RUNTIME
from orchestral.config import (
    ConfigError,
    ModelConfig,
    TaskSpec,
    find_task,
    load_groups,
    load_models,
    load_task,
    load_yaml,
    resolve_judge,
    resolve_model,
)
from orchestral.coverage import coverage_rows, coverage_summary
from orchestral.experiment import (
    Cell,
    cell_state,
    load_matrix,
    rep_target,
    resolve_matrix_tasks,
    run_experiment,
)
from orchestral.export import leaderboard_csv, run_audit_markdown, runs_csv
from orchestral.fileset import expected_paths, required_content
from orchestral.format import NULL_GLYPH
from orchestral.holdout import DEFAULT_ARM_SIZE, DEFAULT_SEED, generate_arm, materialize
from orchestral.judge import DEFAULT_JUDGE
from orchestral.planners import available_prompt_variants, load_prompt_variant
from orchestral.pricing import DEFAULT_DRIFT_THRESHOLD, pricing_drift
from orchestral.privacy import scrub_all
from orchestral.providers import provider_for, provider_key
from orchestral.reporter import generate_dashboard, generate_html_report, model_history
from orchestral.runner import Runner
from orchestral.stats import (
    MIN_LEADERBOARD_SAMPLES,
    aggregate,
    contamination_gap,
    pairing_leaderboard,
)
from orchestral.storage import RunMeta, RunStore
from orchestral.tui import run_tui


def _model_map(models_dir: str) -> dict[str, ModelConfig]:
    return {m.slug: m for m in load_models(models_dir)}


def _model_from_arg(slug: str, models_dir: str = "models", known: dict[str, ModelConfig] | None = None) -> ModelConfig:
    # If the slug is not in the config, treat it as an ad-hoc model with
    # cheap defaults — the shared resolver every launch surface uses.
    return resolve_model(slug, models_dir, known)


def _slugs_from_arg(arg: str) -> list[str]:
    return [s.strip() for s in arg.split(",") if s.strip()]


def _eligible_workers(pool: list[ModelConfig], task: TaskSpec) -> list[ModelConfig]:
    """Filter a worker pool to models that can produce the task's artifact.

    ``requires_executor`` tasks pair only with executor workers declaring
    the task type in ``metadata.capabilities``; ordinary tasks pair with
    chat workers by modality (media tasks need the modality declared).
    Executor workers are excluded from undeclared tasks — the dispatch
    conjunction would reject every cell anyway.
    """
    if (task.metadata or {}).get("requires_executor"):
        return [
            m for m in pool
            if (m.metadata or {}).get("executor")
            and task.type in ((m.metadata or {}).get("capabilities") or [])
        ]
    pool = [m for m in pool if not (m.metadata or {}).get("executor")]
    if task.type in ("image", "video"):
        return [m for m in pool if m.supports(task.type)]
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
    """Judge by default (KTD8): ``--judge`` overrides, ``--no-judge`` opts
    out. The shared resolver makes ``~`` decisions-engine slugs resolvable
    here the same as on web/TUI launches."""
    if getattr(args, "no_judge", False):
        return None
    slug = getattr(args, "judge", None) or DEFAULT_JUDGE
    return resolve_judge(slug, getattr(args, "models_dir", "models"), known)


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
    _budget_check(args, store, 1)
    known = _model_map(args.models_dir)
    return store, known, _judge_from_arg(args, known)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _scoped_mean(store: RunStore, orchestrator: str | None, worker: str | None) -> float:
    """Per-launch estimate: the pairing's own cost history, falling back to
    the global mean, then a conservative default when no history exists."""
    mean = store.mean_run_cost(orchestrator=orchestrator, worker=worker)
    if mean is None:
        mean = store.mean_run_cost()
    return mean if mean is not None else 0.01


def _grid_estimate(store: RunStore, cells: list[tuple[ModelConfig, ModelConfig, int]]) -> float:
    """Aggregate launch estimate honoring per-pairing cost history — a
    cheap pairing shouldn't be priced at the grid's expensive mean."""
    counts: dict[tuple[str, str], int] = {}
    for o, w, _i in cells:
        counts[(o.slug, w.slug)] = counts.get((o.slug, w.slug), 0) + 1
    return sum(_scoped_mean(store, o, w) * n for (o, w), n in counts.items())


def _budget_check(
    args: argparse.Namespace,
    store: RunStore,
    n_runs: int,
    *,
    orchestrator: str | None = None,
    worker: str | None = None,
    estimate: float | None = None,
) -> None:
    """Spend guard. Two independent brakes on top of the provider-side key cap:

    --daily-cap aborts once today's recorded spend reaches the cap;
    --max-cost aborts when this invocation's launch count × historical mean
    cost would exceed the estimate limit. The mean is pairing-scoped when a
    pairing is known (global fallback). Both default on; set 0 to disable.
    In-flight spend is only metered once runs index, so the cap is a
    guardrail, not a realtime limiter."""
    if getattr(args, "dry_run", False):
        return
    daily_cap = getattr(args, "daily_cap", 0) or 0
    max_cost = getattr(args, "max_cost", 0) or 0
    if daily_cap <= 0 and max_cost <= 0:
        return
    spent = store.spend_today()
    if daily_cap > 0 and spent >= daily_cap:
        print(f"Daily cap reached: ${spent:.2f} spent today >= ${daily_cap:.2f} cap. "
              "Raise --daily-cap or wait for UTC midnight.", file=sys.stderr)
        sys.exit(1)
    if estimate is None:
        estimate = n_runs * _scoped_mean(store, orchestrator, worker)
    if max_cost > 0 and estimate > max_cost:
        print(f"Estimated ${estimate:.2f} for {n_runs} launches exceeds --max-cost "
              f"${max_cost:.2f}. Raise it or shrink the matrix.", file=sys.stderr)
        sys.exit(1)
    if daily_cap > 0 and spent + estimate > daily_cap:
        print(f"Estimated ${estimate:.2f} would push today past the ${daily_cap:.2f} "
              f"daily cap (already spent ${spent:.2f}).", file=sys.stderr)
        sys.exit(1)


def _spend_recheck(args: argparse.Namespace, store: RunStore) -> None:
    """Mid-flight spend guard for grids/batches: the aggregate pre-check
    can't see spend that lands between sequential cell launches — re-check
    before each one so in-flight overshoot stays bounded."""
    if getattr(args, "dry_run", False):
        return
    daily_cap = getattr(args, "daily_cap", 0) or 0
    if daily_cap <= 0:
        return
    spent = store.spend_today()
    if spent >= daily_cap:
        print(f"Daily cap reached mid-run: ${spent:.2f} spent today >= "
              f"${daily_cap:.2f} cap, aborting remaining cells.", file=sys.stderr)
        sys.exit(1)


def _check_provider_envs(args: argparse.Namespace, *models: ModelConfig | None) -> None:
    """Fail fast naming every API-key env var the selected models' providers
    need — and, for executor workers, the launch opt-in + adapter binary +
    dedicated credential env keys (the same gate web/TUI launches enforce)."""
    if args.dry_run:
        return
    missing_env: set[str] = set()
    problems: list[str] = []
    for m in models:
        if m is None:
            continue
        if (m.metadata or {}).get("executor"):
            try:
                launch_gate(
                    m,
                    allow_agent_exec=getattr(args, "allow_agent_exec", False),
                )
            except ExecutorPreflightError as exc:
                problems.append(str(exc))
            continue
        env = provider_key(m)[2]
        if env and not os.environ.get(env):
            missing_env.add(env)
    # The isolated code runtime fails closed mid-run if its endpoint contract
    # is absent — catch it here, before any model spend. `harness.py doctor`
    # probes the endpoint itself.
    if os.environ.get(CODE_RUNTIME_ENV, "").strip().lower() == ISOLATED_CODE_RUNTIME:
        for env in ("E2B_DOMAIN", "E2B_API_KEY"):
            if not os.environ.get(env):
                missing_env.add(env)
    if missing_env or problems:
        parts = problems[:]
        if missing_env:
            parts.insert(0, f"API key env var(s) not set: {', '.join(sorted(missing_env))}.")
        print(
            " ".join(parts) + " Set them for the configured providers, or pass --dry-run.",
            file=sys.stderr,
        )
        sys.exit(1)


_CF_HOOK_CLIENT: Any = None


def _cf_sync_hook(store: RunStore) -> Any:
    """Runner.on_run_finished: push the terminal row + calls ledger to the
    hosted observatory when ORCH_CF_SYNC is set (1/true/yes). Best-effort —
    Runner suppresses callback errors, and the sync_dirty journal records the
    run regardless, so `harness.py sync` catches anything the hook misses.
    Holdouts are skipped here too; their journal entries clear as terminal on
    the next sync."""
    if os.environ.get("ORCH_CF_SYNC") not in ("1", "true", "yes"):
        return None
    from orchestral import cf, privacy  # deferred: cf pulls httpx/state deps

    global _CF_HOOK_CLIENT
    if _CF_HOOK_CLIENT is None:
        _CF_HOOK_CLIENT = cf.ingest_client()  # raises SyncError early if creds unset
    client = _CF_HOOK_CLIENT

    def _push(meta: Any) -> None:
        if privacy.run_is_holdout(Path(meta.run_dir)):
            return
        cf.push_run_events_only(client, store, meta)

    return _push


def _runner_kwargs(args: argparse.Namespace, store: RunStore, **extra: Any) -> dict[str, Any]:
    return {
        "dry_run": args.dry_run,
        "planner": args.planner,
        "runs_dir": args.runs_dir,
        "on_run_finished": _cf_sync_hook(store),
        "prompt_variant": getattr(args, "prompt_variant", None),
        "use_judge_cache": not getattr(args, "no_judge_cache", False),
        "jev_assist": getattr(args, "jev_assist", False),
        "run_group": getattr(args, "group", None),
        "replicate": getattr(args, "replicate", None),
        "seed": getattr(args, "seed", None),
        "verbose": getattr(args, "verbose", False),
        "allow_agent_exec": getattr(args, "allow_agent_exec", False),
        "store": store,
        **extra,
    }


def _apply_retry_limit(worker: ModelConfig, args: argparse.Namespace) -> ModelConfig:
    if getattr(args, "retry_limit", None) is not None:
        worker = replace(worker, retry_limit=args.retry_limit)
    return worker


def _attempt_budget(worker: ModelConfig) -> str:
    """State the retry budget that actually applied, per phase.

    Only the delegate loop retries; every orchestrator call, the plan included,
    is single-shot. A failure line that omits this reads as "attempt 1 of some
    policy", which is how a truncated provider response came to be read as a
    harness regression.
    """
    return f"orchestrator 1 per call, worker {worker.retry_limit + 1} (retry_limit={worker.retry_limit})"


def _fail_line(label: str, rep: int, n_reps: int, budget: str, exc: Exception) -> str:
    """One failure line, shared by every harness command.

    `rep` names a replicate, not an attempt — saying so explicitly is the point.
    With one replicate the bare `rep 1` of the previous format was
    indistinguishable from a retry counter.
    """
    return f"[fail] {label} replicate {rep}/{n_reps} (attempts: {budget}): {type(exc).__name__}: {exc}"


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
    "has_content": "required_content",
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
    "multi-file": {"non_empty", "zip_signature", "has_paths", "has_content"},
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
            required_content(task.metadata)
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
        if not task.title or not task.blurb:
            print(f"  warn {path.name}: no title/blurb, observatory shows the raw slug")
        if missing:
            failures += 1
            print(f"  FAIL {path.name}: missing metadata {', '.join(missing)}")
        else:
            print(f"  ok   {path.name} ({task.type})")
    try:
        load_groups(Path(args.tasks_dir).parent / "groups.yaml")
    except Exception as exc:
        failures += 1
        print(f"  FAIL groups.yaml: {exc}")
    try:
        load_models(args.models_dir)
    except Exception as exc:
        failures += 1
        print(f"  FAIL {args.models_dir}: {exc}")
    print(f"{failures} spec problem(s)" if failures else "All specs valid")
    sys.exit(1 if failures else 0)


def _billed(store: RunStore, meta: Any) -> float:
    """A just-finished run's billed cost (the runner's own meta only carries
    the rate-card total)."""
    fresh = store.get_run(meta.run_id)
    return (fresh or meta).display_cost_usd


def cmd_run(args: argparse.Namespace) -> None:
    store, known, judge = _run_preamble(args)
    task = load_task(_task_from_arg(args.task, args.tasks_dir))
    orchestrator = replace(_model_from_arg(args.orchestrator, args.models_dir, known), role="orchestrator")
    worker = _apply_retry_limit(replace(_model_from_arg(args.worker, args.models_dir, known), role="worker"), args)
    _check_provider_envs(args, orchestrator, worker, judge)

    group, n_reps = _resolve_replicates(args)
    # the preamble checked n=1 before the replicate count was known —
    # re-check now so --replicates 10 --max-cost 0.05 prices ten launches
    _budget_check(args, store, n_reps, orchestrator=orchestrator.slug, worker=worker.slug)
    metas = []
    failures = 0
    budget = _attempt_budget(worker)
    for i in range(1, n_reps + 1):
        rep = i if n_reps > 1 else getattr(args, "replicate", None)
        try:
            _spend_recheck(args, store)
            metas.append(Runner(**_rep_kwargs(args, store, group, rep)).run(task, orchestrator, worker, judge))
        except Exception as exc:
            # Runner.record_failure persists the failed run before re-raising;
            # keep going so one flake doesn't lose the remaining replicates
            failures += 1
            print(_fail_line("run", i, n_reps, budget, exc), file=sys.stderr)
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
            print(f"  Cost: ${_billed(store, meta):.6f} | Tokens: {meta.total_input_tokens + meta.total_output_tokens} | Latency: {meta.latency_ms:.0f}ms")
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
        return _eligible_workers(pool, task)

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
    _budget_check(args, store, len(cells), estimate=_grid_estimate(store, cells))

    def _one(orchestrator: ModelConfig, worker: ModelConfig, rep: int) -> dict[str, Any]:
        _spend_recheck(args, store)
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
            futures = {pool.submit(_one, o, w, i): (o.slug, w.slug, i, _attempt_budget(w)) for o, w, i in cells}
            for fut in as_completed(futures):
                o_slug, w_slug, i, budget = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    failures += 1
                    print(_fail_line(f"{o_slug} × {w_slug}", i, n_reps, budget, exc), file=sys.stderr)
        results.sort(key=lambda r: (r["orchestrator"], r["worker"], r["replicate"] or 0))
    else:
        for o, w, i in cells:
            try:
                results.append(_one(o, w, i))
            except Exception as exc:
                failures += 1
                print(_fail_line(f"{o.slug} × {w.slug}", i, n_reps, _attempt_budget(w), exc), file=sys.stderr)

    print("\nGrid summary")
    rep_col = f"{'rep':>4} " if n_reps > 1 else ""
    print(f"{'Orchestrator':<40} {'Worker':<40} {rep_col}{'Cost':>10} {'Tokens':>8} {'Pass':>6} {'Score':>6}")
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
    _budget_check(args, store, len(cells), orchestrator=orchestrator.slug, worker=worker.slug)
    budget = _attempt_budget(worker)

    def _one(path: Path, rep: int) -> dict[str, Any]:
        _spend_recheck(args, store)
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
                    print(_fail_line(f"{p}", i, n_reps, budget, exc), file=sys.stderr)
        results.sort(key=lambda r: (r["task_id"], r["replicate"] or 0))
    else:
        for path, i in cells:
            try:
                results.append(_one(path, i))
            except Exception as exc:
                failures += 1
                print(_fail_line(f"{path}", i, n_reps, budget, exc), file=sys.stderr)

    print(f"\nBatch summary ({len(results)} runs across {len(paths)} tasks)")
    rep_col = f"{'rep':>4} " if n_reps > 1 else ""
    print(f"{'Task':<30} {rep_col}{'Cost':>10} {'Tokens':>8} {'Pass':>6} {'Score':>6}")
    for r in results:
        score = f"{r['score']:.2f}" if r['score'] is not None else "-"
        rep = f"{r['replicate']:>4} " if n_reps > 1 else ""
        print(f"{r['task_id']:<30} {rep}${r['cost']:.6f} {r['tokens']:>8} {r['passes']!s:>6} {score:>6}")

    total_cost = sum(r["cost"] for r in results)
    n_pass = sum(1 for r in results if r["passes"])
    rate = f"{n_pass / len(results):.0%}" if results else "-"
    print(f"TOTAL: {n_pass}/{len(results)} passed ({rate}) | ${total_cost:.6f} | {failures} failures")

    if n_reps > 1:
        for cell in aggregate(store.list_runs(run_group=group)):
            score = f"{cell.score_mean:.2f}±{cell.score_sd:.2f}" if cell.score_mean is not None else "-"
            print(f"[{group}] {cell.task_id}: n={cell.runs} pass={cell.pass_rate:.0%} score={score} cost=${cell.cost_mean:.6f}±${cell.cost_sd:.6f}")

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    if failures:
        sys.exit(1)


def cmd_experiment(args: argparse.Namespace) -> None:
    """Batched A/B driver: every (task, orchestrator, worker) cell runs
    paired baseline/jev-assist replicates with spend, error, and CI gates
    between batches. `--dry-run` prints the priced plan and writes nothing."""
    matrix = load_matrix(args.matrix)
    tasks = resolve_matrix_tasks(matrix, args.tasks_dir)
    cells = matrix.cells

    if args.dry_run:
        store = RunStore(args.runs_dir)
        cell_budget = args.budget / len(cells) if args.budget > 0 else 0.0
        print(f"Experiment {matrix.name!r}: {len(cells)} cell(s), "
              f"budget ${args.budget:.2f} → ${cell_budget:.3f}/cell")
        print(f"{'Cell':<66} {'Est/pair':>9} {'Target':>7} {'State':>9}")
        for cell in cells:
            target, est = rep_target(store, cell, cell_budget)
            state = cell_state(store, matrix.name, cell, target, args.diff_eps)
            print(f"{cell.key:<66} "
                  f"{f'${est:.4f}' if est is not None else '(calib)':>9} "
                  f"{target:>7} {state:>9}")
        return

    store, known, judge = _run_preamble(args)
    models: dict[str, ModelConfig] = {}
    for slug in matrix.orchestrators + matrix.workers:
        models[slug] = resolve_model(slug, args.models_dir, known)
    _check_provider_envs(
        args,
        *[replace(models[s], role="orchestrator") for s in matrix.orchestrators],
        *[replace(models[s], role="worker") for s in matrix.workers],
        judge,
    )

    def _launch(cell: Cell, arm: str, rep: int, group: str, seed: int | None) -> RunMeta:
        orchestrator = replace(models[cell.orchestrator], role="orchestrator")
        worker = _apply_retry_limit(replace(models[cell.worker], role="worker"), args)
        kwargs = _runner_kwargs(
            args, store, run_group=group, replicate=rep, seed=seed,
            jev_assist=(arm == "jev"),
        )
        return Runner(**kwargs).run(tasks[cell.task_id], orchestrator, worker, judge)

    result = run_experiment(
        store, matrix,
        budget=args.budget,
        daily_cap=args.daily_cap,
        batch_size=args.batch_size,
        diff_eps=args.diff_eps,
        seed=args.seed,
        jobs=args.jobs,
        tasks=tasks,
        launch=_launch,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"\nExperiment {result['matrix']!r}: spend ${result['spend']:.4f}"
              + (f" (stopped: {result['stopped']})" if result["stopped"] else ""))
        for key, cell in result["cells"].items():
            note = cell.get("reason") or cell.get("note") or ""
            print(f"  {cell['state']:<8} {key}  {note}")


def cmd_coverage(args: argparse.Namespace) -> None:
    """Done/pending/posted ledger for one experiment matrix."""
    matrix = load_matrix(args.matrix)
    store = RunStore(args.runs_dir)
    rows = coverage_rows(
        store, matrix, budget=args.budget, diff_eps=args.diff_eps
    )
    if args.json:
        print(json.dumps(
            {"matrix": matrix.name,
             "summary": coverage_summary(rows),
             "cells": [r.to_dict() for r in rows]},
            indent=2, default=str))
        return
    summ = coverage_summary(rows)
    print(f"Coverage {matrix.name!r}: {summ['cells']} cell(s): "
          + ", ".join(f"{k} {v}" for k, v in sorted(summ["states"].items()))
          + f" | posted {summ['posted']} | spend ${summ['spend']:.4f}")
    print(f"{'Cell':<62} {'State':<9} {'Base':>7} {'Jev':>7} {'Diff CI':>17} {'Verdict':>13} {'Posted':>6}")
    for r in rows:
        base = f"{r.baseline_passes}/{r.baseline_n}" if r.baseline_n else "-"
        jev = f"{r.jev_passes}/{r.jev_n}" if r.jev_n else "-"
        ci = f"[{r.diff[0]:+.2f},{r.diff[1]:+.2f}]" if r.diff else "-"
        print(f"{r.cell_key:<62} {r.state:<9} {base:>7} {jev:>7} {ci:>17} "
              f"{r.verdict:>13} {'posted' if r.posted else '-':>6}")
        if r.note:
            print(f"    {'':>62} ↳ {r.note}")


def cmd_publish_mark(args: argparse.Namespace) -> None:
    """Check-off surface: 'did this cell/run get published' lives in the
    annotations table, so coverage and the observatory read the same mark."""
    store = RunStore(args.runs_dir)
    note = ": ".join(p for p in (args.url, args.note) if p)
    if args.clear:
        store.set_annotation("post", args.target, "", note="")
        print(f"Cleared publish mark on {args.target}")
        return
    store.set_annotation("post", args.target, "posted", note=note)
    print(f"Marked {args.target} as posted"
          + (f" → {args.url}" if args.url else ""))


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

    def _worker(value: Any) -> ModelConfig:
        if knob == "retry_limit":
            return replace(base_worker, role="worker", retry_limit=value)
        return _apply_retry_limit(replace(base_worker, role="worker"), args)

    def _one(value: Any, rep: int) -> dict[str, Any]:
        worker = _worker(value)
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
                    print(_fail_line(f"{knob}={v}", i, n_reps, _attempt_budget(_worker(v)), exc), file=sys.stderr)
    else:
        for v, i in cells:
            try:
                results.append(_one(v, i))
            except Exception as exc:
                failures += 1
                print(_fail_line(f"{knob}={v}", i, n_reps, _attempt_budget(_worker(v)), exc), file=sys.stderr)
    order = {v: i for i, v in enumerate(values)}
    results.sort(key=lambda r: (order[r["value"]], r["replicate"] or 0))

    print(f"\nAblation: {knob} on {task.id} ({orchestrator.slug} → {base_worker.slug})")
    rep_col = f"{'rep':>4} " if n_reps > 1 else ""
    print(f"{knob:<16} {rep_col}{'Cost':>10} {'Tokens':>8} {'Pass':>6} {'Score':>6}")
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
        print(f"{'Model':<45} {'Runs':>5} {'Pass %':>7} {'Avg score':>9} {'Avg cost':>11} {'Total cost':>11}")
        for name, s in sorted(table.items(), key=lambda kv: -kv[1]["total_cost"]):
            pass_pct = f"{s['pass_rate'] * 100:.1f}%" if s["pass_rate"] is not None else "-"
            score = f"{s['avg_score']:.2f}" if s["avg_score"] is not None else "-"
            print(f"{name:<45} {s['runs']:>5} {pass_pct:>7} {score:>9} ${s['avg_cost']:>10.6f} ${s['total_cost']:>10.4f}")


def cmd_holdout(args: argparse.Namespace) -> None:
    """Generate a holdout arm into a run-scoped directory.

    The arm is written wherever `--out` points and is meant to point somewhere
    git ignores. Nothing in the generator writes into the repository, so the
    caller owns that choice; the warning below is there because publishing the
    arm's task text is the one mistake that makes the whole arm worthless.
    """
    specs = generate_arm(args.count, seed=args.seed)
    out_dir = Path(args.out)
    written = materialize(specs, out_dir)
    print(f"generated {len(written)} holdout spec(s), seed {args.seed} -> {out_dir}")
    by_type: dict[str, int] = {}
    for spec in specs:
        by_type[spec.type] = by_type.get(spec.type, 0) + 1
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    print("\nRun the arm with:")
    print(f"  python harness.py batch --batch-dir {out_dir} --orchestrator <slug> --worker <slug> --dry-run")
    print("\n`--dry-run` is enough to prove the arm is runnable: sql and needle specs are graded")
    print("against real reference answers, so a dry run executes the checks without a provider.")
    print("\nThese task texts are the holdout arm. Keep the directory out of git and out of")
    print("runs-pub/; `harness.py scrub` withholds holdout runs from published output.")
    if args.json:
        print(json.dumps([{"id": s.id, "type": s.type, "path": str(p)} for s, p in zip(specs, written, strict=True)], indent=2))


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
    if getattr(args, "experiment", None):
        matrix = load_matrix(args.experiment)
        cov_rows = coverage_rows(store, matrix, budget=args.budget, diff_eps=args.diff_eps)
        if args.json:
            print(json.dumps({"matrix": matrix.name,
                              "cells": [r.to_dict() for r in cov_rows]},
                             indent=2, default=str))
            return
        print(f"Experiment {matrix.name!r}: baseline vs jev-assist "
              "(mechanical pass is primary; judge deltas are self-referential)")
        print(f"{'Cell':<62} {'Baseline':>10} {'Jev':>10} {'Diff CI':>17} {'Verdict':>13}")
        for row in cov_rows:
            ci = f"[{row.diff[0]:+.2f},{row.diff[1]:+.2f}]" if row.diff else "-"
            print(f"{row.cell_key:<62} "
                  f"{f'{row.baseline_passes}/{row.baseline_n}' if row.baseline_n else '-':>10} "
                  f"{f'{row.jev_passes}/{row.jev_n}' if row.jev_n else '-':>10} "
                  f"{ci:>17} {row.verdict:>13}")
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

    if getattr(args, "contamination", False):
        arms = contamination_gap(runs)
        if args.json:
            print(json.dumps([r.to_dict() for r in arms], indent=2, default=str))
            return
        _print_contamination(arms)
        return

    if getattr(args, "leaderboard", False):
        min_samples = getattr(args, "min_samples", None) or MIN_LEADERBOARD_SAMPLES
        rows = pairing_leaderboard(runs, min_samples=min_samples,
                                   cost_basis="rate_card" if args.json else "billed")
        if args.json:
            print(json.dumps([r.to_dict() for r in rows], indent=2, default=str))
            return
        _print_leaderboard(rows, min_samples, store=store, metas=runs,
                           reports_dir=args.reports_dir)
        return

    if args.pairings:
        _print_pairing_table(runs)
        return

    if args.groups:
        cells = aggregate(runs, cost_basis="rate_card" if args.json else "billed")
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
        print(f"{r.run_id:<13} {planner:<10} {r.orchestrator:<30} {r.task_id:<20} {r.worker:<35} ${r.display_cost_usd:>8.4f} {tokens:>7} {pass_label:>5}")

    print()
    summary = store.summary()
    print(f"Total runs: {summary['runs']} | Total billed cost: ${store.billed_total_usd():.4f} | Total tokens: {summary['total_tokens']}")


def _print_pairing_table(runs: list[Any]) -> None:
    """Aggregate runs by orchestrator × worker, sorted by quality per dollar."""
    groups: dict[tuple[str, str], list[Any]] = {}
    for r in runs:
        if r.status != "finished":
            continue
        groups.setdefault((r.orchestrator, r.worker), []).append(r)

    rows = []
    for (orch, work), group in groups.items():
        cost = sum(r.display_cost_usd for r in group)
        tokens = sum(r.total_input_tokens + r.total_output_tokens for r in group)
        passed = sum(1 for r in group if r.passes)
        scored = [r.score for r in group if r.score is not None]
        # only trust avg score when every run in the group was judged;
        # a partial score set would hide unscored runs' pass results
        avg_score = sum(scored) / len(scored) if len(scored) == len(group) and scored else None
        quality = avg_score if avg_score is not None else passed / len(group)
        qpd = quality / cost if cost > 0 else (float("inf") if quality > 0 else 0.0)
        rows.append((orch, work, len(group), passed, avg_score, cost, tokens, qpd))

    rows.sort(key=lambda r: -r[7])
    print(f"{'orchestrator':<35} {'worker':<35} {'runs':>5} {'pass':>5} {'avg score':>9} {'total cost':>11} {'tokens':>8} {'quality/$':>10}")
    print("-" * 135)
    for orch, work, n, passed, avg_score, cost, tokens, qpd in rows:
        score = f"{avg_score:.2f}" if avg_score is not None else "-"
        print(f"{orch:<35} {work:<35} {n:>5} {passed:>5} {score:>9} ${cost:>10.4f} {tokens:>8} {qpd:>10.1f}")


def _print_leaderboard(
    rows: list[Any],
    min_samples: int,
    store: Any = None,
    metas: list[Any] | None = None,
    reports_dir: str = "reports",
) -> None:
    """Pairing leaderboard — one row per (orchestrator, worker)."""
    if not rows:
        print("No runs match.")
        return
    ranked = [p for p in rows if not p.holdout_only]
    held = [p for p in rows if p.holdout_only]
    if ranked:
        print(f"{'orchestrator':<30} {'worker':<30} {'n':>3} {'tasks':>5} {'pass%':>6} {'med score':>9} {'med judge':>9} {'med cost':>9} {'med ms':>8} {'fail%':>6} {'$/pass':>9} {'holdout':>7}")
        print("-" * 140)
        rank = 0
        divided = False
        for p in ranked:
            if p.low_sample and not divided:
                divided = True
                print(f"{'':<4}── unranked: fewer than {min_samples} runs, anecdote not evidence ──")
            rank += 0 if p.low_sample else 1
            score = f"{p.score_median:.2f}" if p.score_median is not None else "-"
            judge_score = f"{p.judge_score_median:.2f}" if p.judge_score_median is not None else "-"
            cpp = f"${p.cost_per_pass:.4f}" if p.cost_per_pass is not None else "-"
            rank_txt = NULL_GLYPH if p.low_sample else str(rank)
            print(
                f"{rank_txt:>3} {p.orchestrator:<30} {p.worker:<30} {p.runs:>3} {p.tasks_covered:>5} "
                f"{(p.pass_rate or 0) * 100:>5.0f}% {score:>9} {judge_score:>9} ${p.cost_median:>8.4f} "
                f"{p.duration_median_ms:>8.0f} {(p.failure_rate or 0) * 100:>5.0f}% {cpp:>9} "
                f"{p.holdout_runs:>7}"
            )
    if held:
        print(
            f"\n{len(held)} pairing(s) have holdout runs only and are unranked, so they have no "
            "published evidence to rank on. They are listed here so a measured pairing is not "
            "mistaken for an unmeasured one:"
        )
        for p in held:
            print(f"  {p.orchestrator:<30} {p.worker:<30} {p.holdout_runs:>7} holdout run(s)")
    withheld_total = sum(p.holdout_runs for p in rows)
    if withheld_total:
        print(
            f"\nholdout arm: {withheld_total} run(s) excluded from every figure above and "
            f"withheld from publication. Compare arms with: python harness.py report --contamination"
        )
    if store is not None and metas:
        for slug in store.judge_slugs({m.task_id for m in metas}):
            st = calibration_status(reports_dir, slug)
            if st["calibrated"]:
                detail = f"kappa {st['kappa']:.2f} over {st['verdict_pairs']} pairs"
            else:
                detail = (f"uncalibrated: {st['verdict_pairs']} verdict pairs "
                          f"(need {MIN_CALIBRATION_PAIRS}+ labeled, kappa >= 0.7)")
            print(f"judge: {slug}: {detail}")

def _print_contamination(rows: list[Any]) -> None:
    """Published vs holdout means per task type, with the gap between them."""
    if not rows:
        print("No scored runs match, so there is nothing to compare between arms.")
        return
    print("contamination estimate: mean score by arm, per task type")
    print()
    print(f"{'task type':<14} {'pub n':>5} {'pub mean':>9} {'hold n':>6} {'hold mean':>10} {'gap':>8}")
    print("-" * 60)
    for r in rows:
        pub = f"{r.published_mean:.2f}" if r.published_mean is not None else "-"
        hold = f"{r.holdout_mean:.2f}" if r.holdout_mean is not None else "-"
        gap = f"{r.gap:+.2f}" if r.gap is not None else "n/a"
        print(f"{r.task_type:<14} {r.published_n:>5} {pub:>9} {r.holdout_n:>6} {hold:>10} {gap:>8}")
    print()
    comparable = [r for r in rows if r.comparable]
    if not comparable:
        print("No task type appears in both arms, so no gap is defined.")
        print(
            "A gap is only meaningful within one task type: across types it measures a "
            "difference of subject, not of contamination."
        )
        return
    widest = max(comparable, key=lambda r: abs(r.gap or 0.0))
    print(
        f"{len(comparable)} type(s) comparable. A positive gap means the published arm scored "
        "higher than the holdout arm of the same type."
    )
    print(
        f"Widest: {widest.task_type} {widest.gap:+.2f} "
        f"(published n={widest.published_n}, holdout n={widest.holdout_n})."
    )
    thin = [r for r in comparable if min(r.published_n, r.holdout_n) < 5]
    if thin:
        print(
            "Under-powered (fewer than 5 runs on a side): "
            + ", ".join(f"{r.task_type} ({r.published_n}/{r.holdout_n})" for r in thin)
            + "; read these as anecdote."
        )


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
            for c in aggregate(store.list_runs(run_group=group),
                               cost_basis="rate_card" if json_out else "billed")
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
        if args.format == "csv":
            print("error: --format csv exports all runs; use --format md or jsonl with --run", file=sys.stderr)
            sys.exit(2)
        if args.format == "jsonl":
            src = Path(meta.run_dir) / "events.jsonl"
            if not src.exists():
                print(f"error: no events.jsonl for run {args.run} ({meta.run_dir}); nothing to export", file=sys.stderr)
                sys.exit(1)
            content = src.read_text(encoding="utf-8")
        else:
            content = run_audit_markdown(meta.run_dir)
    elif args.format in ("md", "jsonl"):
        print("error: --format md/jsonl require --run; plain exports are CSV only", file=sys.stderr)
        sys.exit(2)
    elif args.leaderboard:
        rows = pairing_leaderboard(store.list_runs(limit=None), min_samples=args.min_samples,
                                   cost_basis="rate_card")  # exports keep the rate-card basis
        content = leaderboard_csv(rows)
    else:
        content = runs_csv(store.list_runs(limit=None))

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"Exported to {out_path}")
    else:
        print(content, end="")


def cmd_dataset(args: argparse.Namespace) -> None:
    """Export an RL-ready dataset — one JSONL record per LLM call (or per run
    with --episodes), each carrying the run's outcome and reward.

    --backfill rebuilds `calls` payloads from events.jsonl first so runs that
    predate payload indexing are included with full text.
    """
    from orchestral.dataset import write_dataset

    store = RunStore(args.runs_dir)
    if args.backfill:
        metas = [m for m in store.list_runs(limit=None) if m.status != "running"]
        n = sum(store.backfill_calls(m) for m in metas)
        print(f"backfilled {n} call payloads across {len(metas)} runs")
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else Path("reports") / f"dataset-steps-{ts}.jsonl"
    ep = (Path(args.episodes_out) if args.episodes_out
          else (Path("reports") / f"dataset-episodes-{ts}.jsonl") if args.episodes else None)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = write_dataset(
        store, out,
        reward=args.reward, include_payloads=not args.no_payloads,
        episodes_path=ep,
        group=args.group, task=args.task,
        orchestrator=args.orchestrator, worker=args.worker,
        include_dry=args.include_dry,
    )
    print(f"wrote {counts['steps']} steps -> {out}")
    if ep:
        print(f"wrote {counts['episodes']} episodes -> {ep}")


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
        print(f"\n{len(drifted)} model(s) beyond {args.threshold:.0%} drift: update models/*.yaml or check for silent rerouting.")

def cmd_models(args: argparse.Namespace) -> None:
    """Provider model catalog — sync the remote list for the observatory."""
    from orchestral import remotecatalog

    if args.models_cmd == "sync":
        url = args.url or remotecatalog.DEFAULT_MODELS_URL
        try:
            catalog = remotecatalog.fetch_remote_catalog(url)
        except Exception as exc:
            print(f"catalog sync failed: {exc}")
            sys.exit(1)
        out = remotecatalog.write_catalog(args.models_dir, catalog)
        free = sum(1 for m in catalog["models"] if m["free"])
        expiring = sum(1 for m in catalog["models"] if m["expires"])
        print(
            f"synced {len(catalog['models'])} models from {url} → {out} "
            f"({free} free, {expiring} with expiry dates)"
        )
        print(
            "note: the provider list says what exists, not what your account "
            "can reach; provider blocks show up as call errors, not here."
        )
        return


def cmd_fixtures(args: argparse.Namespace) -> None:
    """Repo fixtures for v3 real-repo tasks: fetch / check / list."""
    from orchestral.fixtures import (
        FixtureError,
        check_fixtures,
        fetch_fixture,
        load_registry,
        tarball_path,
    )

    root = Path(args.fixtures_dir)
    try:
        registry = load_registry(root)
    except FixtureError as exc:
        print(f"registry error: {exc}")
        sys.exit(1)

    if args.fixtures_cmd == "list":
        if not registry:
            print(f"no fixtures registered in {root / 'registry.yaml'}")
            return
        for fid, spec in registry.items():
            tb = tarball_path(fid, root)
            state = "fetched" if tb.exists() else "pending"
            print(f"{fid:<28} {state:<8} {spec.repo}@{spec.commit[:10]}  {spec.license}")
        return

    if args.fixtures_cmd == "fetch":
        targets = args.ids or sorted(registry)
        for fid in targets:
            try:
                res = fetch_fixture(fid, root)
            except FixtureError as exc:
                print(f"{fid}: {exc}")
                sys.exit(1)
            print(
                f"{res.fixture_id}: {res.repo_members} repo files + "
                f"{res.wheelhouse_members} wheels -> {res.tarball} "
                f"(sha256 {res.sha256[:16]}…)"
            )
            if res.previous_sha256 and res.previous_sha256 != res.sha256:
                print(
                    f"  WARNING: content changed vs previous lock "
                    f"({res.previous_sha256[:16]}… -> {res.sha256[:16]}…): "
                    "upstream repo bytes or resolved wheels differ; verify "
                    "the registry pin and dep versions are what you intended."
                )
        return

    if args.fixtures_cmd == "check":
        problems = check_fixtures(root)
        if problems:
            for p in problems:
                print(f"drift: {p}")
            sys.exit(1)
        print(f"{len(registry)} fixture(s) consistent with registry")


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Judge-vs-human agreement metrics from a labels file."""
    if args.emit:
        yaml_text = emit_label_skeleton(
            RunStore(args.runs_dir),
            run_group=None if args.emit == "all" else args.emit,
        )
        reports_dir = Path(args.reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out_path = reports_dir / f"labels-{args.emit}-{ts}.yaml"
        out_path.write_text(yaml_text)
        print(f"wrote {out_path}. Fill in score/passed, then:")
        print(f"  python3 harness.py calibrate --labels {out_path}")
        return
    if not args.labels:
        raise SystemExit("calibrate needs --labels <file> (or --emit <group> to write a skeleton)")
    labels = load_labels(args.labels)
    result = collect_pairs(RunStore(args.runs_dir), labels)
    metrics = agreement_metrics(result["pairs"])
    report_path = persist_calibration(
        args.reports_dir, labels_path=args.labels,
        pairs=result["pairs"], metrics=metrics)
    out = {"coverage": result["coverage"], "metrics": metrics,
           "report": str(report_path)}
    if args.json:
        print(json.dumps(out, indent=2))
        return
    cov = result["coverage"]
    print(f"Labeled: {cov['labeled']} | matched to runs: {cov['matched']} | "
          f"unmatched: {len(cov['unmatched'])} | unjudged: {len(cov['unjudged'])}")
    if cov["unmatched"]:
        print(f"  unmatched run_ids: {', '.join(cov['unmatched'][:10])}")
    if cov["unjudged"]:
        print(f"  matched but never judged: {', '.join(cov['unjudged'][:10])}")
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
    for axis, title in (("by_judge", "By judge"), ("by_task", "By task")):
        slices = {k: v for k, v in (metrics.get(axis) or {}).items()
                  if v.get("verdict") or v.get("score")}
        if len(slices) > 1:
            print(f"\n{title}:")
            for name, block in sorted(slices.items()):
                v = block.get("verdict")
                line = f"  {name}: "
                if v:
                    line += f"n={v['n']} accuracy {v['accuracy']:.1%} "
                    line += f"kappa {v['kappa']:.2f}" if v["kappa"] is not None else "kappa n/a"
                else:
                    s = block["score"]
                    line += f"score-only n={s['n']} mae {s['mae']:.3f}"
                print(line)
    if not score and not verdict:
        print("\nNo overlapping pairs. Label runs that have judge scores/verdicts.")
        print("Labels file format:")
        print("  labels:\n    - run_id: <prefix>\n      score: 0.8\n      passed: true")
        print("\nOr emit a skeleton:  python3 harness.py calibrate --emit <group>")
    print(f"\nReport: {report_path}")
    for judge_slug in sorted({p.get("judge_model") for p in result["pairs"] if p.get("judge_model")}):
        status = calibration_status(args.reports_dir, judge_slug)
        state = "calibrated" if status["calibrated"] else "uncalibrated"
        detail = f"kappa {status['kappa']:.2f} over {status['verdict_pairs']} pairs" \
            if status["kappa"] is not None else f"{status['verdict_pairs']} pairs"
        print(f"Judge {judge_slug}: {state} ({detail})")


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
    print(f"reviewed {result['reviewed']} runs ({result['skipped']} already reviewed), ${result['cost_usd']:.4f}")
    print(f"quality: {result['corpus']['stats']['run_quality']}")
    for iss in syn.get("systemic_issues") or []:
        print(f"  [{iss.get('severity')}] {iss.get('issue')} ({iss.get('run_count')} runs): {iss.get('fix')}")
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


def cmd_revalidate(args: argparse.Namespace) -> None:
    """Replay mechanical validators on stored artifacts — repairs the score axis."""
    from orchestral.revalidate import revalidate_runs

    result = revalidate_runs(
        RunStore(args.runs_dir),
        run_group=args.group, task_id=args.task,
        orchestrator=args.orchestrator, worker=args.worker,
        limit=args.limit, dry_run=args.dry_run,
        tasks_dir=Path(args.tasks_dir),
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    verb = "would repair" if args.dry_run else "repaired"
    print(f"revalidated {result['runs']} runs: {verb} {result['repaired']}, "
          f"unchanged {result['unchanged']}, skipped {result['skipped']}")
    for r in result["results"]:
        if r.get("unchanged") or r.get("skipped"):
            continue
        print(f"  {r['run_id'][:12]} {r.get('task_id','?')}: "
              f"score {r.get('score_was')} -> {r.get('score')}, "
              f"passes {r.get('passes_was')} -> {r.get('passes')}")
    for r in result["results"]:
        if r.get("skipped"):
            print(f"  skipped {r['run_id'][:12]}: {r['skipped']}")


def cmd_specaudit(args: argparse.Namespace) -> None:
    """jev-style audit of the task suite itself — does the benchmark low-ball?"""
    from orchestral.judge import audit_specs

    judge = _judge_from_arg(args)
    if judge is None:
        print("error: --judge is required (a decisions-model slug, e.g. '~typesafe/jev-latest')")
        raise SystemExit(1)
    specs = [load_task(f) for f in sorted(Path(args.tasks_dir).rglob("*.yaml"))]
    client = None if args.dry_run else provider_for(judge)
    try:
        rows = audit_specs(specs=specs, judge=judge, client=client,
                           dry_run=args.dry_run, workers=args.jobs)
    finally:
        if client is not None:
            client.close()
    out_path = Path(args.reports_dir) / "spec-audit.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "judge": judge.slug, "suite": __import__("orchestral").SUITE_VERSION,
        "audited_at": datetime.now(UTC).isoformat(), "specs": rows,
    }, indent=2, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return
    print(f"spec audit: {len(rows)} specs · judge {judge.slug} · wrote {out_path}")
    print(f"{'task':<30} {'type':<12} {'lowball':>8} {'sound':>6} {'diff':>5} {'adv':>5}")
    for r in rows:
        if "error" in r or "skipped" in r:
            print(f"{r['task_id']:<30} {r['type']:<12} {NULL_GLYPH:>8} {NULL_GLYPH:>6} {NULL_GLYPH:>5} {NULL_GLYPH:>5}  {r.get('error', 'dry-run')[:40]}")
            continue
        low = r["lowballs"]
        flag = " !" if isinstance(low, float) and low >= 0.5 else ""
        print(f"{r['task_id']:<30} {r['type']:<12} {low:>8.2f} {r['sound']:>6.2f} "
              f"{r['difficulty']:>5.1f} {r['adversarial']:>5.1f}{flag}")


def _group_evidence(store: RunStore, groups: list[str]) -> dict[str, Any]:
    """Live stats for named run groups — evidence attached to claims must be
    computed from the index, never hand-typed numbers that can drift."""
    out: dict[str, Any] = {}
    for g in groups:
        metas = store.list_runs(run_group=g)
        finished = [m for m in metas if m.status == "finished"]
        failed = [m for m in metas if m.status == "failed"]
        passes = sum(1 for m in finished if m.passes)
        scores = [m.score for m in finished if m.score is not None]
        out[g] = {
            "runs_total": len(metas),
            "finished": len(finished),
            "failed": len(failed),
            "passes": passes,
            "pass_rate_of_finished": round(passes / len(finished), 3) if finished else None,
            "pass_rate_of_all": round(passes / len(metas), 3) if metas else None,
            "score_mean": round(sum(scores) / len(scores), 3) if scores else None,
            "cost_usd": round(sum(m.total_cost_usd for m in metas), 3),
        }
    return out


def cmd_claimsaudit(args: argparse.Namespace) -> None:
    """Decisions-engine audit of our own claims — scorch the ideas."""
    import yaml

    from orchestral.judge import audit_claims

    judge = _judge_from_arg(args)
    if judge is None:
        print("error: --judge is required (a decisions-model slug, e.g. '~typesafe/jev-latest')")
        raise SystemExit(1)
    path = Path(args.claims)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    claims = data.get("claims") or []
    store = RunStore(args.runs_dir)
    for c in claims:
        groups = c.pop("groups", []) or []
        ev = c.setdefault("evidence", {})
        ev["live_group_stats"] = _group_evidence(store, groups)
    client = None if args.dry_run else provider_for(judge)
    try:
        rows = audit_claims(claims=claims, judge=judge, client=client,
                            dry_run=args.dry_run, workers=args.jobs)
    finally:
        if client is not None:
            client.close()
    out_path = Path(args.reports_dir) / "claims-audit.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "judge": judge.slug, "claims_file": str(path),
        "audited_at": datetime.now(UTC).isoformat(), "claims": rows,
    }, indent=2, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return
    print(f"claims audit: {len(rows)} claims · judge {judge.slug} · wrote {out_path}")
    print(f"{'claim':<28} {'supported':>10} {'fatal?':>7} {'severity':>9} {'strength':>9}")
    for r in rows:
        if "error" in r or "skipped" in r:
            print(f"{r.get('claim_id','?'):<28} {NULL_GLYPH:>10} {NULL_GLYPH:>7} {NULL_GLYPH:>9} {NULL_GLYPH:>9}  {r.get('error','dry-run')[:40]}")
            continue
        sev = r.get("severity")
        verdict = " !!" if isinstance(sev, (int, float)) and sev >= 3.5 else ""
        print(f"{r['claim_id']:<28} {r['supported']:>10.2f} {r['fatal_flaw']:>7.2f} "
              f"{sev:>9.1f} {r['strength']:>9.1f}{verdict}")


def cmd_scrub(args: argparse.Namespace) -> int:
    """Scrub every run for publication and fail if anything was withheld.

    `scrub_all` withholds files it cannot redact and records each one in the
    manifest. A withheld file is a hole in the published record, so a caller
    that only reads the exit status — a CI publish step, a shell pipeline —
    must be able to tell a complete publication from an incomplete one. It
    cannot from the exit status alone, so it exits non-zero when the manifest
    records any blocked file.
    """
    source = Path(args.runs_dir)
    # Validate before scrubbing: scrub_all clears the output directory first, so
    # a wrong --runs-dir used to destroy the previous runs-pub/ and repopulate it
    # from a directory the caller never named. Fail before any write.
    if not source.is_dir():
        print(
            f"error: runs directory not found: {source}. Nothing was written. "
            "Set --runs-dir (before or after `scrub`) to the tree holding run.json files.",
            file=sys.stderr,
        )
        return 1
    if not any(source.rglob("run.json")):
        print(
            f"error: no runs found under {source} (no run.json). Nothing was written. "
            "Check --runs-dir: an empty source must not be published as a success.",
            file=sys.stderr,
        )
        return 1
    copied = scrub_all(source, Path(args.scrub_dir))
    print(f"Scrubbed {len(copied)} runs to {args.scrub_dir}")
    for c in copied:
        print(f"  {c}")

    blocked: list[str] = []
    manifest_path = Path(args.scrub_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = []
    for entry in manifest:
        for item in entry.get("scrub_blocked") or []:
            blocked.append(f"{entry.get('run', '?')}/{item.get('file', '?')}")

    if blocked:
        print(
            f"error: {len(blocked)} file(s) were withheld from publication and are "
            "listed under `scrub_blocked` in the manifest. This run's output is "
            "incomplete until each is reviewed:",
            file=sys.stderr,
        )
        for name in blocked:
            print(f"  {name}", file=sys.stderr)
        return 1
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Push the observatory's rendered payloads + scrubbed artifacts to the
    hosted mirror. Default is a dry run: report the dirty set without
    credentials or network."""
    from orchestral import cf
    from orchestral.storage import RunStore

    store = RunStore(args.runs_dir)
    tasks_dir = Path(args.tasks_dir)
    groups_file = tasks_dir.parent / "groups.yaml"

    if args.verify:
        try:
            diff = cf.verify(store)
        except Exception as exc:
            print(f"verify failed: {exc}", file=sys.stderr)
            return 1
        print(f"local runs:  {diff['local_runs']}  total ${diff['local_total_cost']:.4f}")
        print(f"remote runs: {diff['remote_runs']}  total ${diff['remote_total_cost']:.4f}")
        if diff["missing_remote"]:
            print(f"missing remote ({len(diff['missing_remote'])}): "
                  f"{', '.join(diff['missing_remote'][:10])}"
                  + (" ..." if len(diff["missing_remote"]) > 10 else ""))
        if diff["remote_only"]:
            print(f"remote only ({len(diff['remote_only'])}): "
                  f"{', '.join(diff['remote_only'][:10])}")
        return 1 if diff["missing_remote"] else 0

    result = cf.sync(
        store, tasks_dir, Path(args.models_dir), groups_file,
        push=args.push, all_runs=args.all,
    )
    verb = "pushed" if args.push else "would push"
    print(f"{verb}: {len(result.pushed)} run(s)"
          + (f", {len(result.skipped_holdout)} holdout withheld"
             if result.skipped_holdout else "")
          + (f", {len(result.skipped_missing)} missing dirs"
             if result.skipped_missing else ""))
    for err in result.errors:
        print(f"  error: {err}", file=sys.stderr)
    if not args.push and result.pushed:
        print("dry run: pass --push to upload")
    return 1 if result.errors else 0


def cmd_selfcheck(args: argparse.Namespace) -> None:
    """Replay each spec's own reference material through its validators."""
    from orchestral.selfcheck import run_selfcheck

    findings, exit_code = run_selfcheck(
        args.tasks_dir,
        execute=args.execute,
        runs_dir=args.runs_dir if args.runs else None,
        task_id=args.task,
    )
    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        by_rule: dict[str, list] = {}
        for f in findings:
            by_rule.setdefault(f.rule, []).append(f)
        print(f"orchestral selfcheck: {len(findings)} finding(s)")
        for rule in sorted(by_rule):
            group = by_rule[rule]
            print(f"\n{rule} ({group[0].severity}): {len(group)}")
            for f in group[:20]:
                ident = f.task_id or "suite"
                print(f"  {ident}: {f.detail[:180]}")
            if len(group) > 20:
                print(f"  … and {len(group) - 20} more")
        if not findings:
            print("all references pass their own grading")
    if exit_code:
        sys.exit(exit_code)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Preflight the isolated code runtime: env contract, SDK surface, DNS/TLS
    to api.<domain>, and a live create/exec/destroy probe through the adapter."""
    from orchestral.doctor import run_doctor

    return run_doctor(probe=not args.no_probe)


def cmd_audit(args: argparse.Namespace) -> None:
    """Static task-spec audit: can every declared check fire, and can a model pass without working?"""
    report = audit_tree(
        args.tasks_dir,
        min_family=args.min_family,
        similarity=args.similarity,
        # A generated arm is the normal shape: holdout task text must not be
        # committed, so its absence from tasks/ is the point, not the problem.
        holdout_probe=None if args.no_holdout_arm else default_holdout_probe,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_text())
    if args.strict and not report.ok:
        sys.exit(1)


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
        allow_agent_exec=getattr(args, "allow_agent_exec", False),
    )


def cmd_serve(args: argparse.Namespace) -> None:
    from orchestral.web import run_server

    run_server(
        runs_dir=args.runs_dir,
        tasks_dir=args.tasks_dir,
        models_dir=args.models_dir,
        port=args.port,
        open_browser=args.open,
        allow_agent_exec=getattr(args, "allow_agent_exec", False),
    )


def cmd_cards(args: argparse.Namespace) -> None:
    """Batch-export X-ready PNGs: overview, leaderboard, and every
    group/pairing card — the SPA's own markup, rendered headless."""
    import hashlib
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.parse import quote

    from orchestral.shots import (
        ScreenshotUnavailable,
        browser_session,
        capture_page,
        shot_name,
    )
    from orchestral.web import state as wstate
    from orchestral.web.server import Observatory, make_handler

    store = RunStore(args.runs_dir)
    obs = Observatory(Path(args.runs_dir), Path(args.tasks_dir), Path(args.models_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(obs))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    out_dir = Path(args.reports_dir) / "cards"
    out_dir.mkdir(parents=True, exist_ok=True)

    targets: list[tuple[str, str | None]] = [("/", None), ("/leaderboard", None)]
    groups = [g["group"] for g in wstate.groups_payload(store)]
    if args.group:
        groups = [g for g in groups if g == args.group]
    for g in groups:
        targets.append((f"/card?kind=group&target={quote(g, safe='')}", ".xcard"))
    if args.group:
        # leaderboard_rows carries no group field — take the pairings the
        # group card itself aggregates, and scope each pairing card to it.
        seen: set[str] = set()
        for g in groups:
            d = wstate.card_payload(store, "group", g) or {}
            for pr in d.get("pairing_rows") or []:
                t = f"{pr['orchestrator']}|{pr['worker']}"
                if t in seen:
                    continue
                seen.add(t)
                targets.append((
                    f"/card?kind=pairing&target={quote(t, safe='')}&group={quote(g, safe='')}",
                    ".xcard"))
    else:
        for r in wstate.leaderboard_rows(store):
            t = f"{r['orchestrator']}|{r['worker']}"
            targets.append((f"/card?kind=pairing&target={quote(t, safe='')}", ".xcard"))

    written = failed = 0
    used: set[str] = set()
    try:
        with browser_session() as browser:
            for route, element in targets:
                try:
                    png = capture_page(f"{base}/#{route}", element=element, browser=browser)
                except ScreenshotUnavailable as exc:
                    failed += 1
                    if failed == 1:
                        print(f"capture failed: {exc}")
                    continue
                name = shot_name(route)
                stem, stamp = name.rsplit("-", 1)
                if name in used:
                    # distinct routes that slug-collide must not overwrite —
                    # resolve the digest name BEFORE cleanup so a colliding
                    # route can't delete the file its rival just wrote
                    digest = hashlib.sha1(route.encode()).hexdigest()[:6]
                    stem = f"{stem}-{digest}"
                    name = f"{stem}-{stamp}"
                used.add(name)
                # a re-export replaces the same view's earlier-stamped file;
                # the digit pattern scopes cleanup to this stem only, so a
                # colliding route's digest-stem files are never swept
                for stale in out_dir.glob(stem + "-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].png"):
                    stale.unlink()
                (out_dir / name).write_bytes(png)
                written += 1
    except ScreenshotUnavailable as exc:
        print(f"Screenshot unavailable: {exc}")
        return
    finally:
        httpd.shutdown()
        httpd.server_close()
    print(f"cards: wrote {written} PNG(s) to {out_dir}" + (f", {failed} failed" if failed else ""))


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


# Global directory flags are declared on the top-level parser. A subparser that
# also declares one must use SUPPRESS: argparse copies subparser defaults onto
# the shared namespace *after* the top-level value is parsed, so a plain
# default silently overwrote `harness.py --runs-dir X scrub` with "runs".
GLOBAL_DIR_FLAGS = {"--runs-dir": "runs", "--tasks-dir": "tasks", "--models-dir": "models"}


def _add_global_dir_flag(sp: argparse.ArgumentParser, flag: str, help_text: str) -> None:
    """Re-declare a top-level directory flag on a subparser without shadowing it.

    SUPPRESS leaves the top-level value in place when the flag is absent, and
    overrides it when given: so both spellings work and the subcommand-local
    one wins.
    """
    if flag not in GLOBAL_DIR_FLAGS:
        raise ValueError(f"{flag} is not a global directory flag: {sorted(GLOBAL_DIR_FLAGS)}")
    sp.add_argument(flag, default=argparse.SUPPRESS, help=help_text)


def build_parser() -> argparse.ArgumentParser:
    """Build the full CLI parser. Split out of main() so tests can inspect the
    flag wiring without executing a subcommand."""
    p = argparse.ArgumentParser(description="orchestral eval harness")
    p.add_argument("--runs-dir", default="runs", help="Root directory for run data")
    p.add_argument("--tasks-dir", default="tasks", help="Task spec directory")
    p.add_argument("--models-dir", default="models", help="Model config directory")
    sub = p.add_subparsers(dest="cmd")

    init = sub.add_parser("init", help="Create the runs directory and SQLite index")
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="Parse all task specs and model configs; check per-type metadata contracts")
    _add_global_dir_flag(validate, "--tasks-dir", "Task spec directory")
    _add_global_dir_flag(validate, "--models-dir", "Model config directory")
    validate.set_defaults(func=cmd_validate)

    def _add_run_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--planner", default="raw", choices=["raw", "ce-plan"], help="Orchestrator planning strategy: raw or ce-plan")
        sp.add_argument("--judge", default=None, help=f"Judge model slug (default {DEFAULT_JUDGE}, the decisions engine; vision-capable slugs for image tasks)")
        sp.add_argument("--no-judge", action="store_true", help="Skip the judge pass entirely: mechanical verdict only")
        sp.add_argument("--no-judge-cache", action="store_true", help="Bypass judge result cache reads (still writes)")
        sp.add_argument("--jev-assist", action="store_true",
                        help="Consult the decisions-engine judge inside the run loop: plan audit before delegation, output audit before assembly (one replan / one rework max). No-op without a decisions-model judge.")
        sp.add_argument("--retry-limit", type=int, default=None, help="Override the worker's retry_limit for this invocation")
        sp.add_argument("--prompt-variant", default=None, help="Orchestrator prompt variant from prompts/orchestrator-<name>.md")
        sp.add_argument("--dry-run", action="store_true", help="Do not call OpenRouter; generate sample data for storage testing")
        sp.add_argument("--json", action="store_true", help="Output the summary as JSON")
        sp.add_argument("--verbose", "-v", action="store_true", help="Echo events and debug records to stderr while running")
        sp.add_argument("--group", default=None, help="Label this run with a group name for replicate/variance analysis")
        sp.add_argument("--replicate", type=int, default=None, help="Replicate index within --group")
        sp.add_argument("--replicates", type=int, default=1, help="Run each cell N times under one --group for variance analysis")
        sp.add_argument(
            "--seed",
            type=int,
            default=None,
            help=(
                "Seed for this run. Recorded on the run config and manifest, and "
                "forwarded to the provider for video generation. A holdout spec "
                "carries the seed its task data was generated from, so a run over "
                "the arm records that seed when this is not given."
            ),
        )
        sp.add_argument("--daily-cap", type=float, default=_env_float("ORCHESTRAL_DAILY_CAP", 5.0),
                        help="Abort launches once today's recorded spend reaches this USD (0=off, env ORCHESTRAL_DAILY_CAP, default 5)")
        sp.add_argument("--max-cost", type=float, default=_env_float("ORCHESTRAL_MAX_GRID_COST", 10.0),
                        help="Abort when launch count × historical mean run cost exceeds this USD (0=off, env ORCHESTRAL_MAX_GRID_COST, default 10)")
        sp.add_argument("--allow-agent-exec", action="store_true",
                        default=_env_flag("ORCHESTRAL_ALLOW_AGENT_EXEC"),
                        help="Opt in to executor workers (agent CLIs run with your OS privileges; env ORCHESTRAL_ALLOW_AGENT_EXEC)")

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

    experiment = sub.add_parser(
        "experiment",
        help="Batched A/B experiment: paired baseline/jev-assist arms per matrix cell, cost-scaled reps, spend and evidence gates",
    )
    experiment.add_argument("--matrix", required=True, help="Experiment matrix YAML (orchestrators × workers × tasks)")
    experiment.add_argument("--budget", type=float, default=_env_float("ORCHESTRAL_EXPERIMENT_BUDGET", 0.0),
                            help="Total experiment spend bound in USD: sizes per-cell rep targets and aborts on the live calls meter (0=unbounded rep sizing; the daily cap still brakes)")
    experiment.add_argument("--batch-size", type=int, default=5, help="Replicate indexes per batch between gate checks")
    experiment.add_argument("--diff-eps", type=float, default=0.15, help="Early-stop threshold: difference-CI half-width at which a cell counts as resolved")
    experiment.add_argument("--jobs", type=int, default=1, help="Cells in parallel (reps stay serial inside a cell)")
    experiment.add_argument("--planner", default="raw", choices=["raw", "ce-plan"], help="Orchestrator planning strategy")
    experiment.add_argument("--prompt-variant", default=None, help="Orchestrator prompt variant from prompts/orchestrator-<name>.md")
    experiment.add_argument("--retry-limit", type=int, default=None, help="Override workers' retry_limit")
    experiment.add_argument("--seed", type=int, default=None, help="Base seed recorded on run configs (replicate i records seed+i-1; bookkeeping only; chat providers take no seed)")
    experiment.add_argument("--no-judge-cache", action="store_true", help="Bypass judge result cache reads (still writes)")
    experiment.add_argument("--dry-run", action="store_true", help="Print the priced plan table; write nothing, launch nothing")
    experiment.add_argument("--json", action="store_true", help="Output the summary as JSON")
    experiment.add_argument("--verbose", "-v", action="store_true", help="Echo events while running")
    experiment.add_argument("--daily-cap", type=float, default=_env_float("ORCHESTRAL_DAILY_CAP", 5.0),
                            help="Abort once today's recorded spend reaches this USD (0=off, env ORCHESTRAL_DAILY_CAP, default 5)")
    experiment.add_argument("--allow-agent-exec", action="store_true",
                            default=_env_flag("ORCHESTRAL_ALLOW_AGENT_EXEC"),
                            help="Opt in to executor workers (env ORCHESTRAL_ALLOW_AGENT_EXEC)")
    experiment.set_defaults(func=cmd_experiment)

    ablate = sub.add_parser("ablate", help="Sweep one knob (retry_limit, prompt_variant) for a pairing")
    ablate.add_argument("--task", required=True, help="Task id or path")
    ablate.add_argument("--orchestrator", required=True, help="OpenRouter model slug for the orchestrator")
    ablate.add_argument("--worker", required=True, help="OpenRouter model slug for the worker")
    ablate.add_argument("--sweep", required=True, help="knob=v1,v2,... (knobs: retry_limit, prompt_variant)")
    ablate.add_argument("--jobs", type=int, default=1, help="Run sweep points in parallel with N workers")
    _add_run_flags(ablate)
    ablate.set_defaults(func=cmd_ablate)

    coverage = sub.add_parser("coverage", help="Experiment coverage ledger: matrix cells vs stored runs: done/pending/aborted/posted")
    coverage.add_argument("--matrix", required=True, help="Experiment matrix YAML the ledger is keyed to")
    coverage.add_argument("--budget", type=float, default=0.0, help="Same semantics as experiment --budget: needed to reproduce the rep targets the driver used")
    coverage.add_argument("--diff-eps", type=float, default=0.15, help="Difference-CI half-width at which a cell counts as resolved")
    coverage.add_argument("--json", action="store_true", help="Emit JSON")
    coverage.set_defaults(func=cmd_coverage)

    publish = sub.add_parser("publish-mark", help="Mark a cell or run as published: the 'did we post this' check-off")
    publish.add_argument("--target", required=True, help="Cell key (task:orchestrator:worker) or run id")
    publish.add_argument("--url", default=None, help="Where it was published (post/thread/issue URL)")
    publish.add_argument("--note", default="", help="Free-text note")
    publish.add_argument("--clear", action="store_true", help="Remove the posted mark")
    publish.set_defaults(func=cmd_publish_mark)

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
    report.add_argument("--desc", action=argparse.BooleanOptionalAction, default=True, help="Sort descending (use --no-desc for ascending)")
    report.add_argument("--pairings", action="store_true", help="Aggregate by orchestrator × worker, sorted by quality per dollar")
    report.add_argument("--leaderboard", action="store_true", help="Pairing leaderboard: pass rate, medians, cost per pass, failure rate")
    report.add_argument("--contamination", action="store_true", help="Mean score on published vs holdout specs per task type, and the gap")
    report.add_argument("--min-samples", type=int, default=MIN_LEADERBOARD_SAMPLES, help="Leaderboard sample-size floor for the low-evidence flag")
    report.add_argument("--groups", action="store_true", help="Aggregate by run_group × task × pairing with variance stats")
    report.add_argument("--group", default=None, help="Only include runs from this run_group")
    report.add_argument("--compare", default=None, metavar="A,B", help="Compare two run groups cell-by-cell (pass-rate delta per task × pairing)")
    report.add_argument("--experiment", default=None, metavar="MATRIX", help="Experiment arm table for a matrix spec (baseline vs jev-assist per cell)")
    report.add_argument("--budget", type=float, default=0.0, help="With --experiment: the budget the driver ran under (sizes rep targets)")
    report.add_argument("--diff-eps", type=float, default=0.15, help="With --experiment: difference-CI half-width resolution threshold")
    report.add_argument("--limit", type=int, default=None, help="Limit number of rows")
    report.add_argument("--json", action="store_true", help="Output as JSON")
    report.set_defaults(func=cmd_report)

    prices = sub.add_parser("prices", help="Pricing drift check: provider-reported cost vs configured rate card")
    prices.add_argument("--threshold", type=float, default=DEFAULT_DRIFT_THRESHOLD, help="Drift fraction that flags a model (default 0.15)")
    prices.add_argument("--json", action="store_true", help="Emit JSON")
    prices.set_defaults(func=cmd_prices)

    export = sub.add_parser("export", help="Export runs as CSV, or a single run as Markdown/JSONL")
    _add_global_dir_flag(export, "--runs-dir", "Root directory for run data")
    export.add_argument("--format", choices=["csv", "md", "jsonl"], default="csv", help="Export format")
    export.add_argument("--run", default=None, help="Export a single run id (md audit or jsonl events)")
    export.add_argument("--leaderboard", action="store_true", help="Export pairing leaderboard as CSV")
    export.add_argument("--min-samples", type=int, default=MIN_LEADERBOARD_SAMPLES, help="Leaderboard low-evidence floor")
    export.add_argument("--out", default=None, help="Write to this file instead of stdout")
    export.set_defaults(func=cmd_export)

    dataset = sub.add_parser("dataset", help="Export an RL-ready JSONL dataset: one record per LLM call joined to run outcome and judge rewards")
    _add_global_dir_flag(dataset, "--runs-dir", "Root directory for run data")
    dataset.add_argument("--out", default=None, help="Steps JSONL path (default reports/dataset-steps-<ts>.jsonl)")
    dataset.add_argument("--group", default=None, help="Limit to a run group")
    dataset.add_argument("--task", default=None, help="Limit to a task id")
    dataset.add_argument("--orchestrator", default=None, help="Limit to an orchestrator slug")
    dataset.add_argument("--worker", default=None, help="Limit to a worker slug")
    dataset.add_argument("--reward", choices=["judge", "mechanical", "best"], default="best",
                         help="Which signal lands in outcome.reward (judge=primary judge score, mechanical=test score, best=judge else mechanical)")
    dataset.add_argument("--episodes", action="store_true", help="Also write one record per run (bandit view)")
    dataset.add_argument("--episodes-out", default=None, help="Episodes JSONL path (implies --episodes)")
    dataset.add_argument("--no-payloads", action="store_true", help="Metadata-only steps: omit prompt/completion text")
    dataset.add_argument("--include-dry", action="store_true", help="Include dry-run rows (default excludes: synthetic artifacts)")
    dataset.add_argument("--backfill", action="store_true", help="Rebuild calls payloads from events.jsonl for all stored runs first")
    dataset.set_defaults(func=cmd_dataset)

    scrub = sub.add_parser("scrub", help="Redact sensitive data from all runs for sharing")
    _add_global_dir_flag(scrub, "--runs-dir", "Source runs directory")
    scrub.add_argument("--scrub-dir", default="runs-pub", help="Where to write scrubbed runs")
    scrub.set_defaults(func=cmd_scrub)

    holdout = sub.add_parser(
        "holdout",
        help="Generate a seeded holdout arm into a run-scoped directory (never into git)",
    )
    holdout.add_argument("--out", default="runs-holdout", help="Directory to write the generated specs into")
    holdout.add_argument("--count", type=int, default=DEFAULT_ARM_SIZE, help="How many specs to generate")
    holdout.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Seed the generated task data")
    holdout.add_argument("--json", action="store_true", help="Also emit the generated spec index as JSON")
    holdout.set_defaults(func=cmd_holdout)

    dashboard = sub.add_parser("dashboard", help="Generate a unified stats dashboard")
    _add_global_dir_flag(dashboard, "--runs-dir", "Root directory for run data")
    dashboard.add_argument("--reports-dir", default="reports", help="Output directory for HTML reports")
    dashboard.set_defaults(func=cmd_dashboard)

    tui = sub.add_parser("tui", help="Interactive experiment observatory (needs the [tui] extra)")
    _add_global_dir_flag(tui, "--runs-dir", "Root directory for run data")
    tui.add_argument("--reports-dir", default="reports", help="Output directory for exports")
    tui.add_argument("--refresh", action="store_true", help="Auto-refresh every 5s")
    tui.add_argument("--allow-agent-exec", action="store_true",
                     default=_env_flag("ORCHESTRAL_ALLOW_AGENT_EXEC"),
                     help="Opt in to executor workers (agent CLIs run with your OS privileges; env ORCHESTRAL_ALLOW_AGENT_EXEC)")
    tui.set_defaults(func=cmd_tui)

    shots = sub.add_parser("shots", help="Screenshot HTML artifacts in stored runs (requires playwright extra)")
    _add_global_dir_flag(shots, "--runs-dir", "Root directory for run data")
    shots.add_argument("--task", default=None, help="Only capture runs for this task id")
    shots.add_argument("--all", action="store_true", help="Re-capture even when screenshots are current")

    cards = sub.add_parser("cards", help="Batch-export X-ready PNGs (leaderboard + every group/pairing card) via playwright")
    cards.add_argument("--reports-dir", default="reports", help="Output root: writes <reports>/cards/")
    cards.add_argument("--group", default=None, help="Only export cards scoped to this run_group")
    cards.set_defaults(func=cmd_cards)
    shots.set_defaults(func=cmd_shots)

    calibrate = sub.add_parser("calibrate", help="Judge-vs-human agreement metrics from a labels file")
    calibrate.add_argument("--labels", help="YAML labels file (labels: [{run_id, score, passed}])")
    calibrate.add_argument("--emit", metavar="GROUP",
                           help="Emit a labels skeleton for a run group's finished runs "
                                "(or 'all') instead of computing metrics")
    _add_global_dir_flag(calibrate, "--runs-dir", "Root directory for run data")
    calibrate.add_argument("--reports-dir", default="reports",
                           help="Where calibration reports and label skeletons persist")
    calibrate.add_argument("--json", action="store_true", help="Machine-readable output")
    calibrate.set_defaults(func=cmd_calibrate)

    review = sub.add_parser("review", help="Frontier-model audit of archived run evidence (writes review.json per run + reports/review-*.md)")
    review.add_argument("--model", default="x-ai/grok-4.3", help="Reviewer model slug (default: x-ai/grok-4.3, reasoning tier)")
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

    reval = sub.add_parser("revalidate", help="Replay mechanical validators on stored artifacts: repairs score/passes on report.json + index (no model calls)")
    reval.add_argument("--group", default=None, help="Only revalidate runs in this run_group")
    reval.add_argument("--task", default=None, help="Only revalidate runs for this task")
    reval.add_argument("--orchestrator", default=None)
    reval.add_argument("--worker", default=None)
    reval.add_argument("--limit", type=int, default=None, help="Cap the number of runs revalidated")
    reval.add_argument("--dry-run", action="store_true", help="Report divergences without writing")
    reval.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    reval.set_defaults(func=cmd_revalidate)

    specaudit = sub.add_parser("specaudit", help="Decisions-engine audit of the task suite: lowball/sound/difficulty/adversarial per spec")
    specaudit.add_argument("--judge", required=True, help="Decisions-model slug (e.g. '~typesafe/jev-latest'; quote it)")
    specaudit.add_argument("--jobs", type=int, default=8, help="Parallel audit calls")
    specaudit.add_argument("--reports-dir", default="reports", help="Output directory for spec-audit.json")
    specaudit.add_argument("--dry-run", action="store_true")
    specaudit.add_argument("--json", action="store_true")
    specaudit.set_defaults(func=cmd_specaudit)

    claimsaudit = sub.add_parser("claimsaudit", help="Decisions-engine audit of claims in audit/claims.yaml: scorch our own ideas")
    claimsaudit.add_argument("--judge", required=True, help="Decisions-model slug (e.g. '~typesafe/jev-latest'; quote it)")
    claimsaudit.add_argument("--claims", default="audit/claims.yaml", help="Claims battery file")
    claimsaudit.add_argument("--jobs", type=int, default=4, help="Parallel audit calls")
    claimsaudit.add_argument("--reports-dir", default="reports", help="Output directory for claims-audit.json")
    claimsaudit.add_argument("--dry-run", action="store_true")
    claimsaudit.add_argument("--json", action="store_true")
    claimsaudit.set_defaults(func=cmd_claimsaudit)

    audit = sub.add_parser(
        "audit",
        help="Static task-spec audit: fail-open checks, structural-only graders, contamination risk",
    )
    audit.add_argument("--tasks-dir", default=argparse.SUPPRESS, help="Task spec directory")
    audit.add_argument("--min-family", type=int, default=5, help="Specs sharing one prompt before it is a family")
    audit.add_argument("--similarity", type=float, default=0.8, help="Prompt token Jaccard threshold for a family")
    audit.add_argument("--json", action="store_true", help="Machine-readable output")
    audit.add_argument("--strict", action="store_true", help="Exit non-zero when any error-severity finding exists")
    audit.add_argument(
        "--no-holdout-arm",
        action="store_true",
        help="Do not count a generatable holdout arm: reports no_holdout_arm whenever no spec sets metadata.holdout",
    )
    audit.set_defaults(func=cmd_audit)

    fixtures = sub.add_parser(
        "fixtures",
        help="Repo fixtures for v3 real-repo tasks: fetch/check the gitignored tarballs pinned in fixtures/registry.yaml",
    )
    fixtures.add_argument("--fixtures-dir", default="fixtures", help="Fixture directory")
    fsub = fixtures.add_subparsers(dest="fixtures_cmd", required=True)
    fsub.add_parser("list", help="Show registry state (registered vs fetched)")
    ff = fsub.add_parser("fetch", help="Download + pack fixture tarballs from the registry")
    ff.add_argument("ids", nargs="*", help="Fixture ids (default: all registered)")
    fsub.add_parser("check", help="Verify fetched tarballs match registry pins + locks")
    fixtures.set_defaults(func=cmd_fixtures)

    models = sub.add_parser(
        "models",
        help="Model catalog: sync the provider's full model list for the observatory catalog view",
    )
    msub = models.add_subparsers(dest="models_cmd", required=True)
    msync = msub.add_parser(
        "sync",
        help="Fetch the provider model list (OpenRouter GET /models, no key) "
             "into models/provider-catalog.json",
    )
    msync.add_argument("--url", default=None,
                       help="Catalog endpoint (default: OpenRouter /api/v1/models)")
    # leaf needs its own copy: argparse hands post-`sync` args to msync,
    # so `models sync --models-dir X` would otherwise be unrecognized
    _add_global_dir_flag(msync, "--models-dir", "Model config directory")
    _add_global_dir_flag(models, "--models-dir", "Model config directory")
    models.set_defaults(func=cmd_models)

    selfcheck = sub.add_parser(
        "selfcheck",
        help="Spec self-verification: replay each spec's reference through its own grading (no model calls)",
    )
    selfcheck.add_argument("--tasks-dir", default=argparse.SUPPRESS, help="Task spec directory")
    selfcheck.add_argument("--runs-dir", default=argparse.SUPPRESS, help="Root directory for run data")
    selfcheck.add_argument("--task", default=None, help="Limit to one task id")
    selfcheck.add_argument(
        "--execute",
        action="store_true",
        help="Also run metadata.tests against spec references in a host subprocess "
             "(repo-authored content only: never run artifacts)",
    )
    selfcheck.add_argument(
        "--runs",
        action="store_true",
        help="Advisory: flag wall/ceiling/rubber-stamp checks across stored live runs",
    )
    selfcheck.add_argument("--json", action="store_true", help="Machine-readable output")
    selfcheck.set_defaults(func=cmd_selfcheck)

    serve = sub.add_parser("serve", help="Local web observatory: browse, launch, and cancel runs in a browser (localhost only)")
    serve.add_argument("--port", type=int, default=8787, help="Port to bind on 127.0.0.1 (default 8787)")
    serve.add_argument("--open", action="store_true", help="Open the observatory in a browser")
    serve.add_argument("--allow-agent-exec", action="store_true",
                       default=_env_flag("ORCHESTRAL_ALLOW_AGENT_EXEC"),
                       help="Opt in to executor workers at server start: never a per-request field (env ORCHESTRAL_ALLOW_AGENT_EXEC)")
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser(
        "doctor",
        help="Preflight the isolated code runtime: env contract, SDK surface, "
             "api.<domain> DNS/TLS, and a real sandbox create/exec/destroy probe",
    )
    doctor.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the live sandbox probe (env/SDK/DNS/TLS checks only)",
    )
    doctor.set_defaults(func=cmd_doctor)

    sync = sub.add_parser(
        "sync",
        help="Push observatory payloads + scrubbed run artifacts to the hosted mirror (obs.shippedit.dev)",
    )
    sync.add_argument("--push", action="store_true",
                      help="Actually upload (default: dry run listing the dirty set)")
    sync.add_argument("--verify", action="store_true",
                      help="Diff local run count/cost against the hosted snapshot")
    sync.add_argument("--all", action="store_true",
                      help="Sync every run, not just the dirty set (initial backfill)")
    sync.set_defaults(func=cmd_sync)
    return p


def main() -> None:
    p = build_parser()
    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        return
    try:
        rc = args.func(args)
    except ConfigError as exc:
        sys.exit(f"error: {exc}")
    except FileNotFoundError as exc:
        sys.exit(f"error: {exc}")
    # A command that reports its own failure status returns it. Every other
    # command returns None, which leaves the exit status at 0 as before.
    if isinstance(rc, int) and rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()
