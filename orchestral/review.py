"""Frontier-model review of archived runs.

Runs a strong reviewer model over the durable evidence in each run dir —
meta, plan, report, cost ledger, event summary, worker outputs — and writes
a structured `review.json` back into the run dir. A corpus pass aggregates
all per-run findings into `reports/review-<ts>.md` for methodology audit.

Review output is hypotheses, not verdicts: every finding must cite the
evidence field it was derived from so claims can be verified mechanically.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.config import ModelConfig, TaskSpec, find_task, load_task
from orchestral.costs import compute_cost, token_usage_from_raw
from orchestral.planners import _extract_json
from orchestral.providers import Provider
from orchestral.storage import RunMeta, RunStore

REVIEW_PROMPT = """You are a senior evaluation-methodology reviewer auditing an LLM eval harness run.

You are reviewing the *evidence*, not the models. Your job: decide whether this run
measured what the task intended, whether the failure label (if any) is honest, and
whether the artifact satisfied the spirit of the checks or merely their letter.

Run evidence digest:
```json
{digest}
```

Return only a JSON object with this exact shape:

{{
  "run_quality": "clean" | "suspect" | "invalid",
  "verdict": "<one sentence: did this run measure what it claimed to>",
  "findings": [
    {{
      "kind": "contract" | "validator" | "task_spec" | "model" | "harness" | "prompt",
      "severity": "low" | "medium" | "high",
      "claim": "<what is wrong or notable>",
      "evidence": "<the specific digest field or value you base this on>"
    }}
  ],
  "suggested_check": "<a mechanical check that would confirm or refute the top finding>"
}}

Rules:
- Every finding must cite concrete evidence from the digest. No speculation.
- If the run is clean, return an empty findings list — do not invent issues.
- "suspect" = the run's score/pass may not reflect true capability (e.g. contract
  guessing, validator too weak, task spec ambiguous). "invalid" = the run measured
  the wrong thing entirely (e.g. harness bug, wrong task shape).
"""

SYNTH_PROMPT = """You are a senior evaluation-methodology reviewer synthesizing an audit of an LLM eval harness.

Below are the per-run findings from a batch of reviews, plus aggregate stats.
Identify the top systemic issues, ranked by how much evidence they corrupt.

Aggregate stats:
```json
{stats}
```

Findings (one per line):
{findings}

Return only a JSON object:
{{
  "systemic_issues": [
    {{"issue": "<pattern>", "run_count": <int>, "severity": "low|medium|high", "fix": "<concrete fix>"}}
  ],
  "verdict": "<paragraph: is this eval trustworthy, and what is the single most important thing to fix>"
}}
"""

FINDING_KINDS = ("contract", "validator", "task_spec", "model", "harness", "prompt")
_BOUNDS = {"plan": 4000, "report": 4000, "worker": 1500, "artifact": 2000, "error_events": 3000}


def build_run_digest(run_dir: Path, meta: RunMeta, tasks_dir: Path = Path("tasks")) -> dict[str, Any]:
    """Compact, bounded evidence digest for one run — the reviewer's input."""
    digest: dict[str, Any] = {
        "run": {
            "run_id": meta.run_id,
            "task_id": meta.task_id,
            "orchestrator": meta.orchestrator,
            "worker": meta.worker,
            "status": meta.status,
            "score": meta.score,
            "passes": meta.passes,
            "failure_reason": meta.failure_reason,
            "cost_usd": meta.total_cost_usd,
            "tokens": f"{meta.total_input_tokens}in/{meta.total_output_tokens}out",
            "latency_ms": meta.latency_ms,
            "prompt_variant": (meta.config or {}).get("prompt_variant"),
            "dry_run": (meta.config or {}).get("dry_run"),
            "run_group": meta.run_group,
            "replicate": meta.replicate,
        }
    }
    spec_path = tasks_dir / f"{meta.task_id}.yaml"
    if not spec_path.exists():
        spec_path = find_task(meta.task_id, tasks_dir) or spec_path
    if spec_path.exists():
        try:
            spec: TaskSpec = load_task(spec_path)
            digest["task"] = {
                "type": spec.type,
                "prompt": spec.prompt[:2000],
                "validation": spec.validation,
                "metadata_keys": sorted((spec.metadata or {}).keys()),
            }
        except Exception as exc:
            digest["task"] = {"load_error": str(exc)}
    for name, bound in (("plan.json", _BOUNDS["plan"]), ("report.json", _BOUNDS["report"])):
        path = run_dir / name
        if path.exists():
            try:
                digest[name.split(".")[0]] = json.loads(path.read_text()[:bound])
            except Exception:
                digest[name.split(".")[0]] = path.read_text()[:bound]
    cost_path = run_dir / "cost.json"
    if cost_path.exists():
        with contextlib.suppress(Exception):
            digest["calls"] = json.loads(cost_path.read_text())
    workers = sorted(run_dir.glob("worker-*.json"))
    if workers:
        digest["workers"] = []
        for w in workers[:8]:
            with contextlib.suppress(Exception):
                digest["workers"].append(json.loads(w.read_text()))
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        counts: dict[str, int] = {}
        errors: list[str] = []
        for line in events_path.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type") or "?"
            counts[t] = counts.get(t, 0) + 1
            if e.get("error") or t.endswith((".failed", ".error")):
                errors.append(json.dumps({"type": t, "error": e.get("error"), "output": e.get("output")})[:500])
        digest["events"] = {"counts": counts, "errors": errors[:6]}
    artifact = next((p for p in run_dir.glob("artifact.*") if p.is_file()), None)
    if artifact and artifact.stat().st_size < 2_000_000 and artifact.suffix in (".html", ".py", ".txt", ".md", ".json", ".sql"):
        digest["artifact_head"] = artifact.read_text(errors="replace")[: _BOUNDS["artifact"]]
    return digest


def _validate_review(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("review is not a JSON object")
    out: dict[str, Any] = {
        "run_quality": result.get("run_quality") if result.get("run_quality") in ("clean", "suspect", "invalid") else "suspect",
        "verdict": str(result.get("verdict", "")),
        "findings": [],
    }
    for f in result.get("findings") or []:
        if not isinstance(f, dict) or not f.get("claim") or not f.get("evidence"):
            continue
        out["findings"].append({
            "kind": f.get("kind") if f.get("kind") in FINDING_KINDS else "model",
            "severity": f.get("severity") if f.get("severity") in ("low", "medium", "high") else "low",
            "claim": str(f["claim"]),
            "evidence": str(f["evidence"]),
        })
    if result.get("suggested_check"):
        out["suggested_check"] = str(result["suggested_check"])
    return out


def review_run(
    digest: dict[str, Any],
    model: ModelConfig,
    client: Provider | None,
    dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One reviewer call over a run digest. Returns (review, cost record)."""
    prompt = REVIEW_PROMPT.format(digest=json.dumps(digest, indent=1, default=str)[:24000])
    if dry_run or client is None:
        return {
            "run_quality": "clean",
            "verdict": "Dry-run; no reviewer model was called.",
            "findings": [],
            "suggested_check": "n/a",
        }, {"phase": "review", "model": model.slug, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "pricing_source": "none"}
    completion = client.chat(model=model.slug, messages=[
        {"role": "system", "content": "You are a senior eval-methodology reviewer. Return only a JSON object."},
        {"role": "user", "content": prompt},
    ], max_tokens=4096, temperature=0.1)
    usage = token_usage_from_raw(completion["usage"])
    cost_usd, _ = compute_cost(usage, model)
    try:
        result = _validate_review(_extract_json(completion["content"]))
    except Exception as exc:
        result = {"run_quality": "suspect", "verdict": f"Reviewer output unparseable: {exc}", "findings": [], "parse_failed": True}
    return result, {
        "phase": "review", "model": model.slug,
        "input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
        "cost_usd": cost_usd, "pricing_source": "configured",
        "api_cost_usd": completion.get("api_cost_usd"),
    }


def run_review_batch(
    store: RunStore,
    model: ModelConfig,
    client: Provider | None,
    *,
    run_group: str | None = None,
    task_id: str | None = None,
    orchestrator: str | None = None,
    worker: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    reports_dir: Path = Path("reports"),
    tasks_dir: Path = Path("tasks"),
) -> dict[str, Any]:
    """Review every matching run; write review.json per run + a corpus report."""
    metas = store.list_runs(
        orchestrator=orchestrator, worker=worker, task_id=task_id,
        run_group=run_group, limit=limit,
    )
    reviews: list[dict[str, Any]] = []
    total_cost = 0.0
    skipped = 0
    for meta in metas:
        run_dir = Path(meta.run_dir)
        out_path = run_dir / "review.json"
        if out_path.exists() and not force:
            skipped += 1
            with contextlib.suppress(Exception):
                reviews.append({"run_id": meta.run_id, "task_id": meta.task_id, **json.loads(out_path.read_text())})
            continue
        digest = build_run_digest(run_dir, meta, tasks_dir)
        try:
            review, cost = review_run(digest, model, client, dry_run)
        except Exception as exc:
            review, cost = {
                "run_quality": "suspect",
                "verdict": "Review call failed — evidence unaudited.",
                "findings": [],
                "review_error": str(exc)[:300],
            }, {"cost_usd": 0.0}
        total_cost += cost["cost_usd"]
        record = {
            "reviewer": model.slug,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "run_id": meta.run_id,
            "task_id": meta.task_id,
            **review,
        }
        out_path.write_text(json.dumps(record, indent=2))
        reviews.append(record)
    corpus = _corpus(reviews, metas, model, client, dry_run)
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (reports_dir / f"review-{ts}.json").write_text(json.dumps({"reviews": reviews, "corpus": corpus}, indent=2))
    (reports_dir / f"review-{ts}.md").write_text(_render_markdown(corpus, reviews, model.slug))
    return {"reviews": reviews, "corpus": corpus, "reviewed": len(metas) - skipped, "skipped": skipped, "cost_usd": total_cost}


def _corpus(reviews: list[dict[str, Any]], metas: list[RunMeta], model: ModelConfig,
            client: Provider | None, dry_run: bool) -> dict[str, Any]:
    qualities: dict[str, int] = {}
    kinds: dict[str, int] = {}
    severities: dict[str, int] = {}
    lines: list[str] = []
    for r in reviews:
        q = r.get("run_quality", "?")
        qualities[q] = qualities.get(q, 0) + 1
        for f in r.get("findings") or []:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
            severities[f["severity"]] = severities.get(f["severity"], 0) + 1
            lines.append(f"[{f['severity']}/{f['kind']}] {r.get('task_id')}: {f['claim']}")
    stats = {
        "runs_reviewed": len(reviews),
        "run_quality": qualities,
        "finding_kinds": kinds,
        "severities": severities,
        "tasks": sorted({m.task_id for m in metas}),
        "pairings": sorted({f"{m.orchestrator} -> {m.worker}" for m in metas}),
    }
    synthesis: dict[str, Any] = {"systemic_issues": [], "verdict": "No synthesis (dry-run or no findings)."}
    if not dry_run and client is not None and reviews:
        prompt = SYNTH_PROMPT.format(stats=json.dumps(stats, indent=1), findings="\n".join(lines[:120])[:20000])
        try:
            completion = client.chat(model=model.slug, messages=[
                {"role": "system", "content": "You are a senior eval-methodology reviewer. Return only a JSON object."},
                {"role": "user", "content": prompt},
            ], max_tokens=4096, temperature=0.1)
            parsed = _extract_json(completion["content"])
            if isinstance(parsed, dict):
                synthesis = parsed
        except Exception as exc:
            synthesis = {"systemic_issues": [], "verdict": f"Synthesis call failed: {exc}"}
    return {"stats": stats, "synthesis": synthesis}


def _render_markdown(corpus: dict[str, Any], reviews: list[dict[str, Any]], reviewer: str) -> str:
    stats = corpus["stats"]
    syn = corpus["synthesis"]
    out = [
        f"# Run evidence review — {reviewer}",
        "",
        f"Runs reviewed: {stats['runs_reviewed']} | quality: {stats['run_quality']} | severities: {stats['severities']}",
        "",
        "## Reviewer verdict",
        "",
        syn.get("verdict", ""),
        "",
        "## Systemic issues",
        "",
    ]
    for i, iss in enumerate(syn.get("systemic_issues") or [], 1):
        out.append(f"{i}. **[{iss.get('severity')}]** {iss.get('issue')} ({iss.get('run_count')} runs) — fix: {iss.get('fix')}")
    out += ["", "## High-severity findings", ""]
    for r in reviews:
        for f in r.get("findings") or []:
            if f["severity"] == "high":
                out.append(f"- `{r.get('run_id')}` ({r.get('task_id')}): [{f['kind']}] {f['claim']} — evidence: {f['evidence']}")
    out += ["", "## Per-run quality", ""]
    for r in reviews:
        out.append(f"- `{r.get('run_id')}` {r.get('task_id')}: **{r.get('run_quality')}** — {r.get('verdict')}")
    return "\n".join(out) + "\n"
