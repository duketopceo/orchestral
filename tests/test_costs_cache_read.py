"""Cached-token cost decomposition.

Agents re-read their full context every turn, so ~97% of prompt tokens are
cache reads. A model that *looks* cheaper on headline input/output can be
dramatically more expensive for agent workloads once its cache-read rate is
counted. These tests pin the two behaviors that make that visible:

1. `token_usage_from_raw` extracts OpenRouter/Anthropic cache components
   (and the legacy flat `cached_tokens`) instead of lumping them into prompt.
2. `compute_cost` prices cache-read tokens at the model's cache-read rate
   when one is configured, while leaving every model WITHOUT one on the exact
   legacy bill (cached bytes at full input rate) — so no existing config
   changes.

The ledger also surfaces the decomposed token counts so run summaries expose
how much of a run's input was cache-read.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from orchestral.config import ModelConfig
from orchestral.costs import (
    CostLedger,
    compute_cost,
    token_usage_from_raw,
)

# Rates in dollars per million tokens (Mtok), like the model config YAML.
_AGENT = ModelConfig(
    slug="grok47",
    name="Grok 4.7",
    role="worker",
    input_price_per_mtok=3.0,
    output_price_per_mtok=8.0,
    cache_read_price_per_mtok=0.5,
)
# No cache-read rate configured -> must resolve to legacy behavior.
_LEGACY = ModelConfig(
    slug="grok47-legacy",
    name="Grok 4.7 (no rate card)",
    role="worker",
    input_price_per_mtok=3.0,
    output_price_per_mtok=8.0,
)


def _usd(v: Decimal) -> float:
    return float(v)


class TokenUsageFromRawTest(unittest.TestCase):
    def test_parses_openrouter_cache_components(self):
        usage = token_usage_from_raw(
            {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "cache_read_input_tokens": 97,
                "cache_creation_input_tokens": 2,
            }
        )
        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual(usage.cache_read_input_tokens, 97)
        self.assertEqual(usage.cache_creation_input_tokens, 2)
        self.assertEqual(
            usage.to_dict()["cache_read_input_tokens"], 97,
            "decomposed cache must survive the wire round-trip",
        )

    def test_legacy_flat_cached_tokens_falls_back_to_read(self):
        usage = token_usage_from_raw(
            {"prompt_tokens": 100, "cached_tokens": 60}
        )
        self.assertEqual(usage.cache_read_input_tokens, 60)
        self.assertEqual(usage.cache_creation_input_tokens, 0)


class ComputeCostCacheTest(unittest.TestCase):
    def test_cache_read_billed_at_read_rate(self):
        # prompt=100, cache_read=97, cache_creation=2 -> fresh input = 1.
        # input_cost = (1 + 2)*3e-6 + 97*0.5e-6 = 9e-6 + 48.5e-6 = 57.5e-6
        # output_cost = 1 * 8e-6 = 8e-6 -> total = 65.5e-6
        usage = token_usage_from_raw(
            {
                "prompt_tokens": 100,
                "completion_tokens": 1,
                "cache_read_input_tokens": 97,
                "cache_creation_input_tokens": 2,
            }
        )
        cost, _ = compute_cost(usage, _AGENT)
        expected = 65.5e-6
        self.assertAlmostEqual(cost, expected, places=8)
        # It must be far cheaper than billing every prompt token at input rate.
        naive = 100 * 3e-6 + 1 * 8e-6
        self.assertLess(cost, naive / 4, "cache pricing should be ~4x cheaper than input-rate")

    def test_unconfigured_model_keeps_legacy_bill(self):
        # No cache_read_price_per_mtok -> every prompt token at input rate,
        # cached bytes NOT subtracted, output at output rate.
        usage = token_usage_from_raw(
            {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "cache_read_input_tokens": 90,
            }
        )
        cost, _ = compute_cost(usage, _LEGACY)
        expected = 100 * 3e-6 + 5 * 8e-6
        self.assertAlmostEqual(cost, expected, places=8)

    def test_cache_read_price_property_falls_back_to_input(self):
        self.assertEqual(_LEGACY.cache_read_price, _LEGACY.input_price)
        self.assertAlmostEqual(_AGENT.cache_read_price, 0.5e-6)

    def test_agent_gap_reproduces_headline_ratio(self):
        # Same tokens, agent-heavy: Grok at input-rate vs cache-rate.
        usage = token_usage_from_raw(
            {
                "prompt_tokens": 100,
                "completion_tokens": 1,
                "cache_read_input_tokens": 97,
                "cache_creation_input_tokens": 2,
            }
        )
        legacy_cost, _ = compute_cost(usage, _LEGACY)
        cached_cost, _ = compute_cost(usage, _AGENT)
        ratio = legacy_cost / cached_cost
        self.assertGreater(
            ratio, 4.0,
            "billing cached tokens at input rate should overstate agent cost ~4x",
        )


class CostLedgerCacheTest(unittest.TestCase):
    def test_summary_carries_decomposed_cache_totals(self):
        ledger = CostLedger()
        ledger.add(
            {
                "phase": "run",
                "model": "grok47",
                "input_tokens": 100,
                "output_tokens": 1,
                "cost_usd": 0.0000655,
                "cache_read_input_tokens": 97,
                "cache_creation_input_tokens": 2,
            }
        )
        self.assertEqual(ledger.total_cache_read_tokens(), 97)
        self.assertEqual(ledger.total_cache_creation_tokens(), 2)
        summary = ledger.summary()
        self.assertEqual(summary["total_cache_read_input_tokens"], 97)
        self.assertEqual(summary["breakdown"][0]["cache_read_input_tokens"], 97)


if __name__ == "__main__":
    unittest.main()
