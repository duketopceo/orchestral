"""Tests for the model-role catalog (web/catalog.py::models_catalog_payload)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestral.storage import RunMeta, RunStore
from orchestral.web.catalog import models_catalog_payload


def _meta(**kw) -> RunMeta:
    base = {
        "run_id": "r", "orchestrator": "o/m", "task_id": "t", "worker": "w/m",
        "status": "finished", "started_at": "2026-01-01T00:00:00",
        "total_cost_usd": 0.01, "latency_ms": 100.0, "passes": True,
        "run_group": "g", "config": {}, "run_dir": "",
    }
    base.update(kw)
    return RunMeta(**base)


MODELS_YAML = """\
models:
  - slug: "acme/brain"
    name: "Brain"
    role: orchestrator
    input_price_per_mtok: 1.0
    output_price_per_mtok: 2.0
    metadata:
      vision: true
      modalities: [text, image]
  - slug: "acme/hands"
    name: "Hands"
    role: worker
    input_price_per_mtok: 0.1
    output_price_per_mtok: 0.2
"""


class TestModelsCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RunStore(self.root / "runs")
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()
        (self.models_dir / "default.yaml").write_text(MODELS_YAML, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _by_slug(self, payload, slug):
        return next(m for m in payload["models"] if m["slug"] == slug)

    def test_catalog_lists_configured_models_with_qualified_roles(self):
        d = models_catalog_payload(self.store, self.models_dir)
        brain = self._by_slug(d, "acme/brain")
        self.assertEqual(brain["declared_role"], "orchestrator")
        # declared role + judge (open to every model)
        self.assertEqual(set(brain["qualified"]), {"orchestrator", "judge"})
        self.assertTrue(brain["vision"])
        self.assertEqual(brain["modalities"], ["image", "text"])
        self.assertEqual(brain["source"], "configured")

    def test_zero_usage_shows_not_run(self):
        d = models_catalog_payload(self.store, self.models_dir)
        hands = self._by_slug(d, "acme/hands")
        # usage is sparse — an untouched role is simply absent
        for role in ("orchestrator", "worker", "judge", "reference"):
            self.assertEqual(hands["usage"].get(role, {}).get("runs", 0), 0)

    def test_usage_counts_per_role(self):
        self.store.index_meta(_meta(run_id="r1", orchestrator="acme/brain",
                                    worker="acme/hands"))
        self.store.index_meta(_meta(run_id="r2", orchestrator="acme/brain",
                                    worker="acme/hands"))
        self.store.record_call(run_id="r1", phase="plan", step=0,
                               role="orchestrator", model="acme/brain",
                               cost_usd=0.01)
        self.store.record_call(run_id="r1", phase="work", step=1,
                               role="worker", model="acme/hands",
                               cost_usd=0.005)
        self.store.record_call(run_id="r1", phase="judge", step=2,
                               role="judge", model="acme/hands",
                               cost_usd=0.002)
        d = models_catalog_payload(self.store, self.models_dir)
        brain = self._by_slug(d, "acme/brain")
        self.assertEqual(brain["usage"]["orchestrator"]["runs"], 2)
        self.assertEqual(brain["usage"]["orchestrator"]["calls"], 1)
        self.assertAlmostEqual(
            brain["usage"]["orchestrator"]["cost_usd"], 0.01)
        hands = self._by_slug(d, "acme/hands")
        self.assertEqual(hands["usage"]["worker"]["runs"], 2)
        self.assertEqual(hands["usage"]["judge"]["runs"], 1)
        # demonstrated judge usage widens qualified roles
        self.assertEqual(set(hands["qualified"]), {"worker", "judge"})

    def test_run_without_calls_still_counts(self):
        # a run that died before its first call has no ledger rows — the
        # runs-table orchestrator/worker columns still cover it
        self.store.index_meta(_meta(run_id="r9", orchestrator="acme/brain",
                                    worker="acme/hands"))
        d = models_catalog_payload(self.store, self.models_dir)
        brain = self._by_slug(d, "acme/brain")
        self.assertEqual(brain["usage"]["orchestrator"]["runs"], 1)
        self.assertEqual(brain["usage"]["orchestrator"]["calls"], 0)

    def test_dry_run_calls_do_not_count(self):
        self.store.index_meta(_meta(run_id="rd", orchestrator="acme/brain",
                                    worker="acme/hands", dry_run=1))
        self.store.record_call(run_id="rd", phase="work", step=0,
                               role="worker", model="acme/hands",
                               cost_usd=0.0, dry_run=True)
        d = models_catalog_payload(self.store, self.models_dir)
        hands = self._by_slug(d, "acme/hands")
        self.assertEqual(hands["usage"].get("worker", {}).get("runs", 0), 0)
        self.assertEqual(hands["usage"].get("worker", {}).get("calls", 0), 0)

    def test_uncatalogued_slug_from_ledger_gets_row(self):
        self.store.record_call(run_id="rx", phase="work", step=0,
                               role="worker", model="retired/old-model",
                               cost_usd=0.001)
        d = models_catalog_payload(self.store, self.models_dir)
        old = self._by_slug(d, "retired/old-model")
        self.assertEqual(old["source"], "ledger")
        self.assertIsNone(old["declared_role"])
        self.assertEqual(old["usage"]["worker"]["calls"], 1)

    def test_default_judge_row_present_without_yaml_entry(self):
        from orchestral.judge import DEFAULT_JUDGE
        d = models_catalog_payload(self.store, self.models_dir)
        default = [m for m in d["models"] if m["default"]]
        self.assertEqual(len(default), 1)
        self.assertEqual(default[0]["slug"], DEFAULT_JUDGE)
        self.assertEqual(default[0]["source"], "decisions")

    def test_tilde_yaml_entries_surface_as_decisions_engines(self):
        (self.models_dir / "engines.yaml").write_text(
            "models:\n  - slug: \"~typesafe/jev-pro\"\n    role: judge\n",
            encoding="utf-8")
        d = models_catalog_payload(self.store, self.models_dir)
        pro = self._by_slug(d, "~typesafe/jev-pro")
        self.assertEqual(pro["source"], "decisions")
        self.assertEqual(pro["declared_role"], "judge")
        self.assertIn("judge", pro["qualified"])

    def test_erroring_calls_surface_per_role(self):
        self.store.index_meta(_meta(run_id="re", orchestrator="acme/brain",
                                    worker="acme/hands"))
        self.store.record_call(run_id="re", phase="work", step=0,
                               role="worker", model="acme/hands",
                               error="provider blocked")
        self.store.record_call(run_id="re", phase="work", step=1,
                               role="worker", model="acme/hands",
                               cost_usd=0.005)
        d = models_catalog_payload(self.store, self.models_dir)
        hands = self._by_slug(d, "acme/hands")
        self.assertEqual(hands["usage"]["worker"]["errors"], 1)
        self.assertEqual(hands["usage"]["worker"]["calls"], 2)

    def test_provider_rows_merge_without_duplicates(self):
        from orchestral.remotecatalog import write_catalog
        write_catalog(self.models_dir, {
            "source": "test", "fetched_at": "2026-01-01T00:00:00",
            "models": [
                {"slug": "acme/hands", "name": "Hands", "context": 100},
                {"slug": "newco/free-thing", "name": "FreeThing",
                 "input_modalities": ["text", "image"],
                 "output_modalities": ["text"],
                 "structured": True, "free": True, "expires": "2026-12-01"},
            ],
        })
        d = models_catalog_payload(self.store, self.models_dir)
        slugs = [m["slug"] for m in d["models"]]
        # configured model is not duplicated by its provider listing
        self.assertEqual(slugs.count("acme/hands"), 1)
        free = self._by_slug(d, "newco/free-thing")
        self.assertEqual(free["source"], "provider")
        self.assertTrue(free["free"])
        # text out + structured → worker, judge, orchestrator qualified
        self.assertEqual(set(free["qualified"]),
                         {"orchestrator", "worker", "judge"})
        self.assertEqual(d["provider_synced_at"], "2026-01-01T00:00:00")

    def test_empty_models_dir_lists_ledger_only(self):
        for f in self.models_dir.glob("*.yaml"):
            f.unlink()
        self.store.record_call(run_id="rz", phase="plan", step=0,
                               role="orchestrator", model="x/y")
        d = models_catalog_payload(self.store, self.models_dir)
        self.assertTrue(any(m["slug"] == "x/y" for m in d["models"]))

    def test_malformed_yaml_entries_do_not_crash_catalog(self):
        # the view degrades on bad yaml — one bad file cannot 500 the
        # catalog (load_models already swallows to an empty roster; the
        # ~-entry reader must be equally tolerant of non-mapping items)
        (self.models_dir / "broken.yaml").write_text(
            'models:\n  - "just-a-string"\n  - slug: "~acme/parked"\n    role: worker\n',
            encoding="utf-8")
        (self.models_dir / "list.yaml").write_text(
            "- not\n- a\n- mapping\n", encoding="utf-8")
        d = models_catalog_payload(self.store, self.models_dir)
        self.assertIn("models", d)
        # ~ entries still surface even inside a file load_models rejects
        self.assertTrue(self._by_slug(d, "~acme/parked"))

    def test_tilde_declared_role_counts_as_qualified(self):
        (self.models_dir / "watch.yaml").write_text(
            'models:\n  - slug: "~acme/big-orchestrator"\n    role: orchestrator\n',
            encoding="utf-8")
        d = models_catalog_payload(self.store, self.models_dir)
        row = self._by_slug(d, "~acme/big-orchestrator")
        self.assertEqual(row["declared_role"], "orchestrator")
        self.assertIn("orchestrator", row["qualified"])

    def test_noncanonical_roles_count_spend_but_not_qualification(self):
        # planner/harness call roles (ce-work, harness, NULL) carry real
        # spend but are not catalog roles — usage keeps them, qualified
        # must not
        self.store.index_meta(_meta(run_id="rc", orchestrator="acme/brain",
                                    worker="acme/hands"))
        self.store.record_call(run_id="rc", phase="plan", step=0,
                               role="ce-work", model="acme/brain",
                               cost_usd=0.02)
        d = models_catalog_payload(self.store, self.models_dir)
        brain = self._by_slug(d, "acme/brain")
        self.assertNotIn("ce-work", brain["qualified"])
        self.assertAlmostEqual(brain["usage"]["ce-work"]["cost_usd"], 0.02)


if __name__ == "__main__":
    unittest.main()
