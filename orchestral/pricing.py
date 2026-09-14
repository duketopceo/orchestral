"""Pricing drift detection — configured rate card vs provider-reported cost.

When a call reports `api_cost_usd` (pricing_source="api_reported"), the same
token counts can be re-priced at the configured rate card. The ratio tells
you whether `models/*.yaml` prices have gone stale — provider price changes,
surcharges, or a model silently routed to a different tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestral.config import ModelConfig

DEFAULT_DRIFT_THRESHOLD = 0.15  # |api/configured - 1| beyond this is flagged


@dataclass
class PricingDrift:
    model: str
    calls: int                    # all indexed non-dry-run calls
    api_calls: int                # calls with provider-reported cost
    input_tokens: int
    output_tokens: int
    api_cost_usd: float           # observed total over api_reported calls
    configured_cost_usd: float | None  # same calls at configured rates
    ratio: float | None           # api / configured (None = not comparable)
    drifted: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls": self.calls,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "api_cost_usd": self.api_cost_usd,
            "configured_cost_usd": self.configured_cost_usd,
            "ratio": self.ratio,
            "drifted": self.drifted,
            "note": self.note,
        }


def pricing_drift(
    summary_rows: list[dict[str, Any]],
    models: dict[str, ModelConfig],
    *,
    threshold: float = DEFAULT_DRIFT_THRESHOLD,
) -> list[PricingDrift]:
    """Fold `calls_pricing_summary()` rows into per-model drift records.

    `summary_rows` is one row per (model, pricing_source). `models` maps
    slug -> ModelConfig; a slug absent from models/ can't be re-priced.
    """
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in summary_rows:
        by_model.setdefault(row["model"], []).append(row)

    out: list[PricingDrift] = []
    for model, rows in sorted(by_model.items()):
        calls = sum(int(r["calls"]) for r in rows)
        api = [r for r in rows if r["pricing_source"] == "api_reported" and r["api_cost_usd"] is not None]
        api_calls = sum(int(r["calls"]) for r in api)
        api_cost = float(sum(r["api_cost_usd"] for r in api))
        in_tok = int(sum(r["input_tokens"] or 0 for r in api))
        out_tok = int(sum(r["output_tokens"] or 0 for r in api))

        cfg = models.get(model)
        note = ""
        configured: float | None = None
        ratio: float | None = None
        if not api:
            note = "no provider-reported costs"
        elif cfg is None:
            note = "model not in models/ — can't re-price"
        elif not (cfg.input_price_per_mtok or cfg.output_price_per_mtok):
            note = "no token rates configured"
        elif in_tok + out_tok == 0:
            note = "non-token pricing (image/video) — not comparable"
        else:
            configured = (
                in_tok * cfg.input_price_per_mtok / 1e6
                + out_tok * cfg.output_price_per_mtok / 1e6
            )
            if configured > 0:
                ratio = api_cost / configured
            else:
                note = "configured rates are zero"
        drifted = ratio is not None and abs(ratio - 1) > threshold
        out.append(
            PricingDrift(
                model=model,
                calls=calls,
                api_calls=api_calls,
                input_tokens=in_tok,
                output_tokens=out_tok,
                api_cost_usd=api_cost,
                configured_cost_usd=configured,
                ratio=ratio,
                drifted=drifted,
                note=note,
            )
        )
    return out
