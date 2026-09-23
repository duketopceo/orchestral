"""Append-only JSONL event logger for agent actions.

Two channels per run directory:

- `events.jsonl` — semantic events (run_start, llm_call, worker_error, ...).
  This is the analysis stream; keep it clean.
- `debug.jsonl` — operational diagnostics (http retries, video polls,
  provider decisions). Verbose, separate file, omitted from scrubbed output.

When a RunStore + run_id are provided, every `llm_call`/`worker_error` event
is also indexed as a row in the `calls` table so SQL queries never have to
re-parse JSONL.
"""

from __future__ import annotations

import contextlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

EVENT_SCHEMA_VERSION = "2"

# Event types worth indexing per-call in the `calls` table.
_CALL_EVENT_TYPES = ("llm_call", "worker_error")

# Lifecycle vocabulary (see docs/tui-observability-design.md). Detail events
# like llm_call/worker_error coexist — these mark phase transitions so a live
# view can render progress without understanding call payloads.
LIFECYCLE_EVENTS = frozenset({
    "run.created", "task.loaded", "run.started",
    "orchestrator.started", "orchestrator.completed",
    "delegation.created", "orchestrator.self_executed",
    "worker.started", "worker.progress", "worker.completed", "worker.failed",
    "synthesis.started", "synthesis.completed",
    "evaluation.started", "evaluation.completed",
    "usage.recorded", "artifact.saved",
    "run.completed", "run.failed", "run.cancelled",
})


class EventLogger:
    """Write timestamped, structured events to runs/{...}/events.jsonl."""

    def __init__(
        self,
        run_dir: Path | str,
        store: Any = None,
        run_id: str | None = None,
        dry_run: bool = False,
        verbose: bool = False,
    ):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self._fh = open(self.events_path, "a", encoding="utf-8")
        self.debug_path = self.run_dir / "debug.jsonl"
        self._debug_fh = open(self.debug_path, "a", encoding="utf-8")
        self._debug_seq = 0
        self._store = store
        self._run_id = run_id
        self._dry_run = dry_run
        self._verbose = verbose
        self._seq = 0

    def log(
        self,
        *,
        phase: str,
        step: int,
        event_type: str,
        model: str,
        role: str,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        reasoning: str = "",
        cost: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
        error: str | None = None,
        worker_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a single structured event and return it."""
        self._seq += 1
        event: dict[str, Any] = {
            "event_id": uuid.uuid4().hex[:16],
            "schema_version": EVENT_SCHEMA_VERSION,
            "run_id": self._run_id,
            "sequence": self._seq,
            "timestamp": datetime.now(UTC).isoformat(),
            "phase": phase,
            "step": step,
            "type": event_type,
            "role": role,
            "worker_id": worker_id,
            "model": model,
            "input": input_data,
            "output": output_data,
            "reasoning": reasoning,
            "cost": cost or {},
            "latency_ms": latency_ms,
            "error": error,
            "metadata": metadata or {},
        }
        self._fh.write(json.dumps(event, default=str) + "\n")
        self._fh.flush()
        if self._verbose:
            self._echo_event(event)
        if self._store is not None and self._run_id and event_type in _CALL_EVENT_TYPES:
            self._index_call(event)
        return event

    def lifecycle(
        self,
        event_type: str,
        *,
        phase: str = "",
        role: str = "harness",
        worker_id: str | None = None,
        step: int = -1,
        **payload: Any,
    ) -> dict[str, Any]:
        """Emit a phase-transition event from the lifecycle vocabulary.

        Payload keys land in `output` so consumers read one place. These are
        coarse markers for live views — call detail still goes through
        `log`/`log_llm_call`.
        """
        if event_type not in LIFECYCLE_EVENTS:
            raise ValueError(f"Not a lifecycle event type: {event_type}")
        return self.log(
            phase=phase or "lifecycle",
            step=step,
            event_type=event_type,
            model="",
            role=role,
            worker_id=worker_id,
            input_data={},
            output_data=payload,
        )

    def _index_call(self, event: dict[str, Any]) -> None:
        """Mirror a call-level event into the `calls` table.

        Indexing must never kill a run — any store failure is swallowed into
        the debug stream.
        """
        cost = event.get("cost") or {}
        try:
            self._store.record_call(
                run_id=self._run_id,
                phase=event.get("phase"),
                step=event.get("step"),
                role=event.get("role"),
                model=event.get("model"),
                input_tokens=cost.get("input_tokens") or 0,
                output_tokens=cost.get("output_tokens") or 0,
                cost_usd=cost.get("usd") or 0.0,
                api_cost_usd=cost.get("api_cost_usd"),
                pricing_source=cost.get("pricing_source"),
                latency_ms=event.get("latency_ms") or 0.0,
                attempt=(event.get("output") or {}).get("attempt"),
                error_category=(event.get("metadata") or {}).get("error_category"),
                error=event.get("error"),
                dry_run=self._dry_run,
                worker_id=event.get("worker_id"),
                sequence=event.get("sequence"),
            )
        except Exception as exc:  # pragma: no cover - defensive
            with contextlib.suppress(Exception):
                self.log_debug("logger", "calls-table insert failed", error=str(exc))

    def _echo_event(self, event: dict[str, Any]) -> None:
        cost = event.get("cost") or {}
        bits = [f"[{event['phase']}/{event['type']}]", event.get("model") or event.get("role") or ""]
        if event.get("latency_ms"):
            bits.append(f"{event['latency_ms']:.0f}ms")
        if cost.get("usd"):
            bits.append(f"${cost['usd']:.6f}")
        if event.get("error"):
            bits.append(f"ERROR: {event['error'][:160]}")
        print(" ".join(b for b in bits if b), file=sys.stderr)

    def log_debug(self, component: str, message: str, **fields: Any) -> dict[str, Any]:
        """Append an operational record to debug.jsonl.

        For diagnostics only: never pass file contents, media bytes,
        credentials, or provider-returned URLs in `fields`.
        """
        self._debug_seq += 1
        record = {
            "seq": self._debug_seq,
            "timestamp": datetime.now(UTC).isoformat(),
            "component": component,
            "message": message,
            "fields": fields,
        }
        self._debug_fh.write(json.dumps(record, default=str) + "\n")
        self._debug_fh.flush()
        if self._verbose:
            detail = " ".join(f"{k}={v}" for k, v in fields.items())
            print(f"  dbg[{component}] {message} {detail}".rstrip(), file=sys.stderr)
        return record

    def log_llm_call(
        self,
        *,
        phase: str,
        step: int,
        model: str,
        role: str,
        messages: list[dict[str, Any]],
        completion: dict[str, Any],
        reasoning: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        error: str | None = None,
        pricing_source: str | None = None,
        api_cost_usd: float | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """Convenience wrapper for an OpenRouter LLM call."""
        cost: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usd": cost_usd,
            "pricing_source": pricing_source,
        }
        if api_cost_usd is not None:
            cost["api_cost_usd"] = api_cost_usd
        out: dict[str, Any] = {**completion, "attempt": attempt} if attempt is not None else completion
        return self.log(
            phase=phase,
            step=step,
            event_type="llm_call",
            model=model,
            role=role,
            input_data={"messages": messages},
            output_data=out,
            reasoning=reasoning,
            cost=cost,
            latency_ms=latency_ms,
            error=error,
        )

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        if not self._debug_fh.closed:
            self._debug_fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
