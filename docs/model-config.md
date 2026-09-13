# Model config schema

Model configs live in `models/*.yaml`. Each file holds a `models:` list;
a leading `~` on a slug disables the entry without deleting it.

```yaml
models:
  - slug: "deepseek/deepseek-v4-flash-0731"   # required; the wire model name
    name: "DeepSeek V4 Flash 0731"           # required; display name
    role: worker                            # required; orchestrator | worker | reference
    input_price_per_mtok: 0.03              # required; USD per 1M input tokens
    output_price_per_mtok: 0.10             # required; USD per 1M output tokens
    context: 1048576                        # optional; context window (default 128000)
    max_tokens: 131072                      # optional; generation cap (default 8192)
    retry_limit: 2                          # optional; worker retries (default 2)
    price_per_image: 0.0                    # optional; USD per image for image tasks
    price_per_video_second: 0.0             # optional; USD per second for video tasks
    metadata:                               # optional free-form map
      provider: openrouter                  # openrouter (default) | openai-compatible
      base_url: https://api.together.xyz/v1 # required for openai-compatible
      api_key_env: TOGETHER_API_KEY         # env var holding the key
      modalities: [text, image]             # which task types the model can produce
      vision: true                          # can judge image artifacts
```

## Fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `slug` | str | required | Model identifier sent to the provider API; prefix `~` to disable |
| `name` | str | required | Human-readable name for reports |
| `role` | str | required | `orchestrator`, `worker`, or `reference` |
| `input_price_per_mtok` | float | required | USD per million input tokens |
| `output_price_per_mtok` | float | required | USD per million output tokens |
| `context` | int | 128000 | Context window size |
| `max_tokens` | int | 8192 | Max output tokens per call |
| `retry_limit` | int | 2 | Worker retry attempts; overridable per run with `--retry-limit` |
| `price_per_image` | float | 0.0 | Fallback price when image usage reports no cost |
| `price_per_video_second` | float | 0.0 | Fallback price per generated second; the video job's `usage.cost` wins when reported |
| `metadata` | map | `{}` | Provider + capability keys below |

## `metadata` keys

| Key | Type | Notes |
|---|---|---|
| `provider` | str | `openrouter` (default) or `openai-compatible`; see [providers.md](providers.md) |
| `base_url` | str | API base for `openai-compatible` providers |
| `api_key_env` | str | Env var holding the API key (`OPENROUTER_API_KEY` / `OPENAI_API_KEY` defaults) |
| `modalities` | list[str] | `text`, `image`, `video` — `grid` filters workers by task type |
| `vision` | bool | Marks a judge as able to score image artifacts |

## Notes

- **Cost accounting**: prices are per-million tokens; the runner derives
  per-token cost and falls back to `price_per_image` for image calls without
  token usage. Video calls prefer the completed job's `usage.cost` and fall
  back to `duration × price_per_video_second`.
- **Unknown slugs** passed on the CLI get ad-hoc configs with cheap default
  pricing — put real prices in `models/*.yaml` for accurate cost reports.
