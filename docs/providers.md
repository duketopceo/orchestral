# Providers

orchestral talks to model APIs through a small provider seam. The default is
**OpenRouter**; any **OpenAI-compatible chat endpoint** (Together, Groq, vLLM,
ollama, …) works by config alone — no new dependencies.

## Configuration

Providers are set per model in `models/*.yaml` `metadata`:

```yaml
- slug: "meta-llama/llama-4-scout"
  name: "Llama 4 Scout (Together)"
  role: worker
  input_price_per_mtok: 0.18
  output_price_per_mtok: 0.59
  metadata:
    provider: openai-compatible
    base_url: "https://api.together.xyz/v1"
    api_key_env: "TOGETHER_API_KEY"
```

| Key | Default | Notes |
|---|---|---|
| `metadata.provider` | `openrouter` | `openrouter` or `openai-compatible`; anything else fails fast |
| `metadata.base_url` | `https://openrouter.ai/api/v1` | Required for `openai-compatible` |
| `metadata.api_key_env` | `OPENROUTER_API_KEY` / `OPENAI_API_KEY` | Env var read for the key; the key itself never touches disk |

## Behavior

- **Per-role routing**: orchestrator, worker, and judge resolve their own
  clients, so a run can pair an OpenRouter orchestrator with a Together worker.
  Roles sharing identical provider config share one client.
- **Chat only**: `openai-compatible` providers serve text tasks. Image tasks
  use OpenRouter's Images API and fail fast on other providers.
- **Transparency**: each run logs `provider → base_url → env var name` in its
  `run_start` event (never the key value).
- **Plain HTTP** base URLs are allowed for private/loopback hosts (ollama etc.)
  and warn to stderr for anything else.

## Adding a real provider

Implement `orchestral.providers.Provider` (`chat`, `images`, `close`), map a
provider name in `provider_for`, and document it here. Vendor SDKs (Anthropic,
Gemini) are intentionally out of scope for v1.0 — pull requests welcome.
