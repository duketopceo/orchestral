"""Derive metrics.json from a run's events.jsonl.

The event stream is the source of truth (it carries latency, which the cost
ledger does not). metrics.json precomputes the aggregates reports and the
GUI slice on so consumers never re-parse JSONL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

METRICS_SCHEMA_VERSION = "1"


def _num(value: Any) -> float:
    return value if isinstance(value, (int, float)) else 0.0


def build_metrics(events_path: Path) -> dict[str, Any]:
    """Aggregate a run's events.jsonl into per-phase/role metrics.

    Returns a dict with per-(phase, role) call aggregates, error counts by
    category (worker_error events only — the run-level category lives on
    runs.failure_reason, so counting run_failed would double-count), retry
    counts, and the event total. Malformed lines and odd field types are
    skipped per-event so one bad record can't void the whole summary.
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    error_counts: dict[str, int] = {}
    pricing_sources: dict[str, int] = {}
    retries = 0
    n_events = 0

    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        n_events += 1
        etype = ev.get("type")
        meta = ev.get("metadata")
        cat = meta.get("error_category") if isinstance(meta, dict) else None
        if cat and etype == "worker_error":
            error_counts[cat] = error_counts.get(cat, 0) + 1
        if etype == "worker_retry":
            retries += 1
        if etype != "llm_call":
            continue
        key = (ev.get("phase") or "", ev.get("role") or "")
        s = by_key.setdefault(
            key,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latencies": []},
        )
        s["calls"] += 1
        cost = ev.get("cost")
        if isinstance(cost, dict):
            s["input_tokens"] += cost.get("input_tokens") or 0
            s["output_tokens"] += cost.get("output_tokens") or 0
            s["cost_usd"] += _num(cost.get("usd"))
            src = cost.get("pricing_source") or "unlabeled"
            pricing_sources[src] = pricing_sources.get(src, 0) + 1
        s["latencies"].append(_num(ev.get("latency_ms")))

    phases: dict[str, Any] = {}
    for (phase, role), s in sorted(by_key.items()):
        lats = s["latencies"]
        entry = phases.setdefault(phase, {})
        entry[role] = {
            "calls": s["calls"],
            "input_tokens": s["input_tokens"],
            "output_tokens": s["output_tokens"],
            "cost_usd": round(s["cost_usd"], 7),
            "latency_ms": {
                "mean": round(sum(lats) / len(lats), 1) if lats else 0.0,
                "max": round(max(lats), 1) if lats else 0.0,
            },
        }

    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "events": n_events,
        "phases": phases,
        "retries": retries,
        "error_counts": dict(sorted(error_counts.items())),
        "pricing_sources": dict(sorted(pricing_sources.items())),
    }
