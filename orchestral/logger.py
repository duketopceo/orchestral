"""Append-only JSONL event logger for agent actions.

Every significant action becomes one line in events.jsonl. The schema is
versioned and can be consumed by the CLI, the web UI, and post-run analysis.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


EVENT_SCHEMA_VERSION = "1"


class EventLogger:
    """Write timestamped, structured events to runs/{...}/events.jsonl."""

    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self._fh = open(self.events_path, "a", encoding="utf-8")

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
        cost: Optional[dict[str, Any]] = None,
        latency_ms: float = 0.0,
        error: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Append a single structured event and return it."""
        event: dict[str, Any] = {
            "event_id": uuid.uuid4().hex[:16],
            "schema_version": EVENT_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "step": step,
            "type": event_type,
            "role": role,
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
        return event

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
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        """Convenience wrapper for an OpenRouter LLM call."""
        return self.log(
            phase=phase,
            step=step,
            event_type="llm_call",
            model=model,
            role=role,
            input_data={"messages": messages},
            output_data=completion,
            reasoning=reasoning,
            cost={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usd": cost_usd,
            },
            latency_ms=latency_ms,
            error=error,
        )

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "EventLogger":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
