"""AI Gateway provider path: base-url rewrite, header contract, label
injection, direct decisions, and the redirect guard.

No live calls — httpx.MockTransport stands in for the wire.
"""

import json
import os
import unittest
import unittest.mock

import httpx

from orchestral import providers
from orchestral.config import ModelConfig
from orchestral.openrouter import OpenRouterClient, OpenRouterError
from orchestral.runner import LabeledProvider

ACCT = "1661907b2d7e4a20800306e6a57844c5"
GW = "orchestral-eval"
COMPAT = f"https://gateway.ai.cloudflare.com/v1/{ACCT}/{GW}/compat"


def _model(**meta) -> ModelConfig:
    return ModelConfig(
        slug="org/m", name="m", role="worker",
        input_price_per_mtok=0.5, output_price_per_mtok=2.0,
        metadata=meta or None,
    )


class TestGatewayUrl(unittest.TestCase):
    def test_unset_env_means_no_gateway(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCHESTRAL_AIG_GATEWAY", None)
            self.assertIsNone(providers._aig_compat_url())

    def test_compat_url_from_spec(self):
        with unittest.mock.patch.dict(
                os.environ, {"ORCHESTRAL_AIG_GATEWAY": f"{ACCT}/{GW}"}):
            self.assertEqual(providers._aig_compat_url(), COMPAT)

    def test_malformed_spec_ignored(self):
        for bad in ("noSlash", "/", f"{ACCT}/", f"/{GW}", ""):
            with unittest.mock.patch.dict(
                    os.environ, {"ORCHESTRAL_AIG_GATEWAY": bad}):
                self.assertIsNone(providers._aig_compat_url(), bad)


class TestProviderFor(unittest.TestCase):
    def _env(self, **extra):
        env = {"OPENROUTER_API_KEY": "k", "CLOUDFLARE_AIG_TOKEN": "t",
               "ORCHESTRAL_AIG_GATEWAY": f"{ACCT}/{GW}"}
        env.update(extra)
        return unittest.mock.patch.dict(os.environ, env, clear=False)

    def test_default_openrouter_rewritten_to_gateway(self):
        with self._env():
            client = providers.provider_for(_model())
        self.assertEqual(str(client.client.base_url).rstrip("/"), COMPAT)
        self.assertEqual(client.direct_base_url, "https://openrouter.ai/api/v1")
        self.assertTrue(client.aig_token)

    def test_custom_base_url_stays_direct(self):
        with self._env():
            client = providers.provider_for(
                _model(base_url="http://127.0.0.1:11434/v1"))
        self.assertEqual(str(client.client.base_url).rstrip("/"),
                         "http://127.0.0.1:11434/v1")
        self.assertFalse(client.aig_token)
        self.assertIsNone(client.direct_base_url)

    def test_gateway_requires_token(self):
        env = {"OPENROUTER_API_KEY": "k",
               "ORCHESTRAL_AIG_GATEWAY": f"{ACCT}/{GW}"}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLOUDFLARE_AIG_TOKEN", None)
            from orchestral.openrouter import ProviderConfigError
            with self.assertRaises(ProviderConfigError):
                providers.provider_for(_model())

    def test_non_openrouter_provider_not_rewritten(self):
        with self._env():
            client = providers.provider_for(_model(
                provider="openai-compatible",
                base_url="https://api.example.com/v1",
                api_key_env="OPENROUTER_API_KEY"))
        self.assertFalse(client.aig_token)


class TestGatewayHeaders(unittest.TestCase):
    def _client(self) -> OpenRouterClient:
        env = {"OPENROUTER_API_KEY": "k", "CLOUDFLARE_AIG_TOKEN": "tok"}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            return OpenRouterClient(
                base_url=COMPAT, aig_token_env="CLOUDFLARE_AIG_TOKEN",
                direct_base_url="https://openrouter.ai/api/v1")

    def test_gateway_headers_present(self):
        h = self._client()._headers()
        self.assertEqual(h["cf-aig-authorization"], "Bearer tok")
        self.assertEqual(h["cf-aig-collect-log-payload"], "false")
        self.assertNotIn("cf-aig-metadata", h)

    def test_labels_become_metadata(self):
        h = self._client()._headers(labels={
            "run_id": "r1", "task": "t", "role": "worker", "group": "g",
            "seed": 42, "sixth": "dropped"})
        meta = json.loads(h["cf-aig-metadata"])
        self.assertEqual(len(meta), 5)
        self.assertEqual(meta["seed"], "42")  # values stringified
        self.assertNotIn("sixth", meta)

    def test_no_gateway_no_aig_headers(self):
        env = {"OPENROUTER_API_KEY": "k"}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            client = OpenRouterClient()
        h = client._headers(labels={"run_id": "r1"})
        self.assertNotIn("cf-aig-authorization", h)
        self.assertNotIn("cf-aig-metadata", h)

    def test_decide_uses_direct_origin(self):
        client = self._client()
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"answers": {}, "usage": {}})

        client.client = httpx.Client(
            base_url=COMPAT, transport=httpx.MockTransport(handler))
        client.decide(model="m", state="s", questions={})
        self.assertEqual(seen["url"],
                         "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(seen["auth"], "Bearer k")

    def test_direct_paths_never_leak_aig_token(self):
        """decide()/images() go straight to OpenRouter — the Cloudflare AIG
        credential must not ride along to the provider origin."""
        client = self._client()
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            if "decisions" in str(request.url):
                return httpx.Response(200, json={"answers": {}, "usage": {}})
            return httpx.Response(200, json={
                "data": [{"b64_json": "aGk="}], "usage": {}})

        client.client = httpx.Client(
            base_url=COMPAT, transport=httpx.MockTransport(handler))
        client.decide(model="m", state="s", questions={})
        client.images(model="m", prompt="p")
        self.assertEqual(len(seen), 2)
        for h in seen:
            self.assertEqual(h.get("authorization"), "Bearer k")
            self.assertNotIn("cf-aig-authorization", h)
            self.assertNotIn("cf-aig-metadata", h)
            self.assertNotIn("cf-aig-collect-log-payload", h)

    def test_redirect_refused(self):
        client = self._client()
        client.client = httpx.Client(
            base_url=COMPAT,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    302, headers={"location": "https://evil.example/x"})))
        with self.assertRaises(OpenRouterError):
            client.chat(model="m", messages=[{"role": "u", "content": "hi"}])


class TestLabeledProvider(unittest.TestCase):
    class _Inner:
        supports_labels = True

        def __init__(self):
            self.kwargs = None

        def chat(self, **kw):
            self.kwargs = kw
            return {"content": "ok"}

    def test_labels_injected(self):
        inner = self._Inner()
        lp = LabeledProvider(inner, {"run_id": "r1", "role": "worker",
                                     "group": "", "seed": None})
        lp.chat(model="m", messages=[])
        self.assertEqual(inner.kwargs["labels"],
                         {"run_id": "r1", "role": "worker"})

    def test_unsupported_inner_gets_no_labels(self):
        class _Fake:
            def chat(self, **kw):
                return kw

        lp = LabeledProvider(_Fake(), {"run_id": "r1"})
        self.assertNotIn("labels", lp.chat(model="m", messages=[]))

    def test_role_wrappers_share_client_keep_roles(self):
        inner = self._Inner()
        a = LabeledProvider(inner, {"role": "orchestrator"})
        b = LabeledProvider(inner, {"role": "worker"})
        a.chat(model="m", messages=[])
        self.assertEqual(inner.kwargs["labels"]["role"], "orchestrator")
        b.chat(model="m", messages=[])
        self.assertEqual(inner.kwargs["labels"]["role"], "worker")


if __name__ == "__main__":
    unittest.main()
