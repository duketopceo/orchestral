"""Tests for the provider seam: provider_for resolution and per-role routing."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from orchestral.config import ModelConfig, TaskSpec
from orchestral.openrouter import OpenRouterClient, OpenRouterError
from orchestral.providers import provider_for, provider_key
from orchestral.runner import Runner
from orchestral.storage import RunStore


def _model(slug: str, role: str = "worker", metadata: dict | None = None) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.1, output_price_per_mtok=0.4,
        metadata=metadata or {},
    )


class TestProviderFor(unittest.TestCase):
    def test_default_model_resolves_openrouter(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            client = provider_for(_model("org/m"))
            self.assertIsInstance(client, OpenRouterClient)
            self.assertEqual(str(client.client.base_url).rstrip("/"), "https://openrouter.ai/api/v1")
            client.close()

    def test_openai_compatible_resolves_env_and_base_url(self):
        model = _model("org/m", metadata={
            "provider": "openai-compatible",
            "base_url": "https://api.together.xyz/v1",
            "api_key_env": "TOGETHER_API_KEY",
        })
        with patch.dict("os.environ", {"TOGETHER_API_KEY": "k2"}):
            client = provider_for(model)
            self.assertEqual(str(client.client.base_url).rstrip("/"), "https://api.together.xyz/v1")
            self.assertEqual(client.provider, "openai-compatible")
            client.close()

    def test_missing_env_var_names_the_var(self):
        model = _model("org/m", metadata={
            "provider": "openai-compatible",
            "base_url": "https://api.together.xyz/v1",
            "api_key_env": "MISSING_TEST_KEY",
        })
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                provider_for(model)
            self.assertIn("MISSING_TEST_KEY", str(ctx.exception))

    def test_unknown_provider_fails_fast(self):
        model = _model("org/m", metadata={"provider": "anthropic-sdk"})
        with self.assertRaises(ValueError) as ctx:
            provider_for(model)
        self.assertIn("anthropic-sdk", str(ctx.exception))

    def test_missing_base_url_fails_fast(self):
        model = _model("org/m", metadata={"provider": "openai-compatible"})
        with self.assertRaises(ValueError) as ctx:
            provider_for(model)
        self.assertIn("base_url", str(ctx.exception))

    def test_images_rejected_on_non_openrouter_provider(self):
        with patch.dict("os.environ", {"K": "x"}):
            client = OpenRouterClient(base_url="https://api.together.xyz/v1",
                                      api_key_env="K", provider="openai-compatible")
            with self.assertRaises(OpenRouterError) as ctx:
                client.images(model="m", prompt="p")
            self.assertIn("chat-only", str(ctx.exception))

    def test_http_warning_only_for_public_hosts(self):
        model = _model("m", metadata={"provider": "openai-compatible",
                                      "base_url": "http://llm.example.com/v1",
                                      "api_key_env": "K"})
        with patch.dict("os.environ", {"K": "x"}), patch("sys.stderr") as err:
            provider_for(model)
            self.assertTrue(err.write.called)
        model2 = _model("m", metadata={"provider": "openai-compatible",
                                       "base_url": "http://localhost:11434/v1",
                                       "api_key_env": "K"})
        with patch.dict("os.environ", {"K": "x"}), patch("sys.stderr") as err2:
            provider_for(model2).close()
            self.assertFalse(err2.write.called)


class TestRunnerProviderRouting(unittest.TestCase):
    def test_injected_clients_route_per_role(self):
        """Mock clients per role prove orchestrator/worker calls hit the right client."""
        orch_client, worker_client = MagicMock(), MagicMock()
        plan = {"subtasks": [{"id": 0, "description": "write it"}]}
        orch_client.chat.return_value = {
            "content": json.dumps(plan),
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            "latency_ms": 1, "id": "p",
        }
        worker_client.chat.return_value = {
            "content": "<html><title>x</title><body>hi</body></html>",
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            "latency_ms": 1, "id": "w",
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(
                runs_dir=tmp, store=RunStore(tmp),
                clients={"orchestrator": orch_client, "worker": worker_client},
            )
            task = TaskSpec(id="t1", type="html", prompt="build a page")
            meta = runner.run(task, _model("o/m", "orchestrator"), _model("w/m"))
            self.assertEqual(meta.status, "finished")
            orch_client.chat.assert_called()   # plan + assemble went to orch client
            worker_client.chat.assert_called()  # delegation went to worker client
            worker_client.images.assert_not_called()

    def test_same_provider_shares_one_client(self):
        model_a = _model("a/m", "orchestrator")
        model_b = _model("b/m", "worker")
        runner = Runner(dry_run=False, runs_dir=tempfile.mkdtemp(),
                        store=RunStore(tempfile.mkdtemp()))
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            clients = runner._resolve_clients(model_a, model_b, None)
        self.assertIs(clients["orchestrator"], clients["worker"])
        self.assertEqual(len(runner._owned_clients), 1)
        for c in runner._owned_clients:
            c.close()

    def test_distinct_providers_get_distinct_clients(self):
        orch = _model("o/m", "orchestrator")
        worker = _model("w/m", metadata={
            "provider": "openai-compatible",
            "base_url": "https://api.together.xyz/v1",
            "api_key_env": "K2",
        })
        runner = Runner(dry_run=False, runs_dir=tempfile.mkdtemp(),
                        store=RunStore(tempfile.mkdtemp()))
        env = {"OPENROUTER_API_KEY": "k", "K2": "k2"}
        with patch.dict("os.environ", env):
            clients = runner._resolve_clients(orch, worker, None)
        self.assertIsNot(clients["orchestrator"], clients["worker"])
        self.assertEqual(clients["worker"].provider, "openai-compatible")
        self.assertEqual(provider_key(worker)[2], "K2")
        for c in runner._owned_clients:
            c.close()


if __name__ == "__main__":
    unittest.main()
