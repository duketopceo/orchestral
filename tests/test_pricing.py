"""Pricing drift: store aggregate query + ratio computation + flags."""

from __future__ import annotations

import tempfile
import unittest

from orchestral.config import ModelConfig
from orchestral.logger import EventLogger
from orchestral.pricing import pricing_drift
from orchestral.storage import RunStore


def _model(slug: str, in_mtok: float = 0.10, out_mtok: float = 0.40) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role="worker",
        input_price_per_mtok=in_mtok, output_price_per_mtok=out_mtok,
    )


def _seed_calls(tmp: str, *, dry_run: bool = False) -> RunStore:
    """Two api_reported calls on m/x plus one configured-only call on m/y."""
    store = RunStore(tmp)
    logger = EventLogger(tmp, store=store, run_id="r1", dry_run=dry_run)
    for i in range(2):
        logger.log_llm_call(
            phase="delegate", step=i, model="m/x", role="worker",
            messages=[], completion={}, input_tokens=1000, output_tokens=500,
            cost_usd=0.0010, latency_ms=10.0,
            pricing_source="api_reported", api_cost_usd=0.0010,
        )
    logger.log_llm_call(
        phase="delegate", step=3, model="m/y", role="worker",
        messages=[], completion={}, input_tokens=100, output_tokens=50,
        cost_usd=0.0001, latency_ms=10.0, pricing_source="configured",
    )
    logger.close()
    return store


class TestPricingSummary(unittest.TestCase):
    def test_groups_by_model_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _seed_calls(tmp)
            rows = store.calls_pricing_summary()
            by_key = {(r["model"], r["pricing_source"]): r for r in rows}
            self.assertIn(("m/x", "api_reported"), by_key)
            self.assertIn(("m/y", "configured"), by_key)
            mx = by_key[("m/x", "api_reported")]
            self.assertEqual(mx["calls"], 2)
            self.assertEqual(mx["input_tokens"], 2000)
            self.assertEqual(mx["output_tokens"], 1000)
            self.assertAlmostEqual(mx["api_cost_usd"], 0.002)

    def test_dry_run_rows_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _seed_calls(tmp, dry_run=True)
            self.assertEqual(store.calls_pricing_summary(), [])


class TestPricingDrift(unittest.TestCase):
    def _rows(self):
        return [
            {"model": "m/x", "pricing_source": "api_reported", "calls": 2,
             "input_tokens": 2000, "output_tokens": 1000,
             "cost_usd": 0.0020, "api_cost_usd": 0.0020},
            {"model": "m/x", "pricing_source": "configured", "calls": 1,
             "input_tokens": 100, "output_tokens": 50,
             "cost_usd": 0.0001, "api_cost_usd": None},
            {"model": "m/y", "pricing_source": "configured", "calls": 1,
             "input_tokens": 100, "output_tokens": 50,
             "cost_usd": 0.0001, "api_cost_usd": None},
            {"model": "m/z", "pricing_source": "api_reported", "calls": 1,
             "input_tokens": 0, "output_tokens": 0,
             "cost_usd": 0.36, "api_cost_usd": 0.36},
        ]

    def test_in_tolerance(self):
        # 2000 in @ 0.50/mtok + 1000 out @ 1.00/mtok = 0.0020 configured,
        # matching the 0.0020 api total → ratio 1.0, no drift
        models = {"m/x": _model("m/x", in_mtok=0.50, out_mtok=1.00)}
        rows = pricing_drift(self._rows(), models)
        mx = next(r for r in rows if r.model == "m/x")
        # configured ≈ 0.0020 → ratio ≈ 1.0 → not drifted
        self.assertFalse(mx.drifted)
        self.assertAlmostEqual(mx.ratio or 0, 1.0, places=2)

    def test_drifted_when_api_cost_diverges(self):
        models = {"m/x": _model("m/x")}  # cheap rates → ratio ~3.3
        rows = pricing_drift(self._rows(), models)
        mx = next(r for r in rows if r.model == "m/x")
        self.assertTrue(mx.drifted)
        self.assertGreater(mx.ratio or 0, 3.0)
        self.assertEqual(mx.calls, 3)  # configured-source calls count too
        self.assertEqual(mx.api_calls, 2)

    def test_no_api_costs_note(self):
        models = {"m/y": _model("m/y")}
        rows = pricing_drift(self._rows(), models)
        my = next(r for r in rows if r.model == "m/y")
        self.assertFalse(my.drifted)
        self.assertIsNone(my.ratio)
        self.assertIn("no provider-reported", my.note)

    def test_non_token_pricing_not_comparable(self):
        rows = pricing_drift(self._rows(), {"m/z": _model("m/z")})
        mz = next(r for r in rows if r.model == "m/z")
        self.assertFalse(mz.drifted)
        self.assertIn("non-token", mz.note)

    def test_unknown_model_flagged(self):
        rows = pricing_drift(self._rows(), {})  # nothing in models/
        mx = next(r for r in rows if r.model == "m/x")
        self.assertFalse(mx.drifted)
        self.assertIn("not in models/", mx.note)


class TestPricesCli(unittest.TestCase):
    def test_cmd_prices_json(self):
        import argparse
        import contextlib
        import io
        import json as _json

        from harness import cmd_prices

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            logger = EventLogger(tmp, store=store, run_id="r1", dry_run=False)
            # z-ai/glm-5.3-flash: 0.075/0.25 per mtok → configured ≈ 0.00021
            # for 2x(1000in+500out); api reports 10x that → drifted
            for i in range(2):
                logger.log_llm_call(
                    phase="delegate", step=i, model="z-ai/glm-5.3-flash", role="worker",
                    messages=[], completion={}, input_tokens=1000, output_tokens=500,
                    cost_usd=0.002, latency_ms=10.0,
                    pricing_source="api_reported", api_cost_usd=0.002,
                )
            logger.close()
            args = argparse.Namespace(
                runs_dir=tmp, models_dir="models", threshold=0.15, json=True,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_prices(args)
            rows = _json.loads(buf.getvalue())
            glm = next(r for r in rows if r["model"] == "z-ai/glm-5.3-flash")
            self.assertTrue(glm["drifted"])
            self.assertGreater(glm["ratio"], 2.0)


if __name__ == "__main__":
    unittest.main()
