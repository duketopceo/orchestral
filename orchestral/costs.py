"""Cost accounting for orchestral, ported from perplexityai/search_evals.

Tracks per-call token usage and cost in Decimal to avoid floating-point drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from orchestral.config import ModelConfig

USD_QUANT = Decimal("0.0000001")


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class CostRecord:
    phase: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    usage: dict[str, int] | None = None


class CostLedger:
    """Sums per-call costs exactly once."""

    def __init__(self) -> None:
        self.records: list[CostRecord] = []

    def add(self, record: CostRecord | dict[str, Any]) -> None:
        if isinstance(record, dict):
            record = CostRecord(
                phase=record["phase"],
                model=record["model"],
                input_tokens=record.get("input_tokens", 0),
                output_tokens=record.get("output_tokens", 0),
                cost_usd=record.get("cost_usd", 0.0),
                usage=record.get("usage"),
            )
        self.records.append(record)

    def add_many(self, records: list[CostRecord | dict[str, Any]]) -> None:
        for record in records:
            self.add(record)

    def total_cost_usd(self) -> float:
        return usd(sum((Decimal(str(r.cost_usd)) for r in self.records), Decimal("0")))

    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.records)

    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.records)

    def to_breakdown(self) -> list[dict[str, Any]]:
        return [
            {
                "phase": r.phase,
                "model": r.model,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": r.cost_usd,
                "usage": r.usage,
            }
            for r in self.records
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "currency": "USD",
            "calls": len(self.records),
            "total_cost_usd": self.total_cost_usd(),
            "total_input_tokens": self.total_input_tokens(),
            "total_output_tokens": self.total_output_tokens(),
            "breakdown": self.to_breakdown(),
        }


def _token_component(tokens: int, price_per_token: float) -> Decimal:
    return Decimal(tokens) * Decimal(str(price_per_token))


def token_usage_from_raw(usage: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cached_tokens=usage.get("cached_tokens", 0),
        reasoning_tokens=usage.get("reasoning_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
    )


def compute_cost(usage: TokenUsage, model_cfg: ModelConfig) -> tuple[float, TokenUsage]:
    """Return (cost_usd, normalized_token_usage) for a model call."""
    input_cost = _token_component(usage.prompt_tokens, model_cfg.input_price)
    output_cost = _token_component(usage.completion_tokens, model_cfg.output_price)
    cost = input_cost + output_cost
    return usd(cost), usage


def compute_image_cost(model_cfg: ModelConfig, usage: TokenUsage | None = None, n: int = 1) -> float:
    """Cost for an image generation call.

    Uses token usage when the API reports it, otherwise falls back to the
    configured per-image price on the model config.
    """
    if (
        usage is not None
        and (usage.prompt_tokens or usage.completion_tokens)
        and (model_cfg.input_price_per_mtok or model_cfg.output_price_per_mtok)
    ):
        cost, _ = compute_cost(usage, model_cfg)
        return cost
    return usd(_token_component(n, model_cfg.price_per_image))


def usd(value: Decimal) -> float:
    return float(value.quantize(USD_QUANT, rounding=ROUND_HALF_UP))
