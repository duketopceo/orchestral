# Laya — open typed-decision model (Jev alternative)

[Laya](https://github.com/NandhaKishorM/laya) (Convai Innovations, Apache-2.0) is a
**non-autoregressive typed-decision model**: instead of generating text token by token, it
scores a fixed set of choices in a single forward pass and returns calibrated
probabilities. Checkpoints live on Hugging Face (`convaiinnovations/laya`, 421M params).

## Why it matters to orchestral

- **Open Jev alternative.** On typed decisions (routing, triage, guardrails, extraction
  routing) the fine-tuned checkpoint scores **0.766** vs Jev's published **0.727** — and it
  is an Apache-2.0 download, not a hosted endpoint. No ToS landmine, no per-call cost, no
  rate limits.
- **Fine-tunable.** Ships an RLCD fine-tuning notebook (Kaggle 2×T4), so the fleet can
  specialize it on orchestral's own decision tasks.
- **Fast.** One parallel forward pass per request on CPU; no autoregressive decoding.

## Integration

Laya is a Python library, not a server, so orchestral talks to it through a thin adapter
that wraps `laya.Agent.system_one` as an OpenAI-compatible `/v1/chat/completions`
endpoint:

1. Start the adapter (first call downloads ~1–2 GB from HF):

   ```sh
   uv run --python 3.11 --with torch --with transformers --with safetensors \
       --with huggingface_hub --with numpy \
       python scripts/serve_laya.py --port 8090
   # --subfolder multilingual | typed-decisions to pick a different checkpoint
   ```

2. Point orchestral at it — `models/laya.yaml` already defines the worker:

   ```sh
   uv run --python 3.11 python harness.py batch --batch-dir tasks/router-eval \
       --orchestrator <orchestrator-slug> --worker laya/local-decision
   ```

Options are taken from a JSON body field `"options"` (list) or parsed from the prompt's
`one of: a, b, c` hint — the exact shape `scripts/gen_router_eval.py` emits — so
router-eval tasks work unmodified. The response content is `{"choice": ..., "probabilities":
..., "confidence": ...}`, preserving Laya's calibration signal in the run logs.

## Caveats

- **>50 options:** the checkpoint's `head_max_len` (256 tokens) truncates long option
  lists — accuracy on a 77-option task drops to **0.425**. Raise `head_max_len` in the
  checkpoint config for wide choice sets.
- **Score questions are weak** (SST-5 ordinal scoring 0.372): use Laya for `choice`
  decisions, not graded scores. The adapter only exposes the `choice` type.
- **English vs multilingual routing:** the default English checkpoint **collapses** off
  English (0.100 on Hindi MASSIVE intent, with high reported confidence). Route by script:
  non-English states should hit the `multilingual` subfolder (`--subfolder
  multilingual`), or start a second adapter instance and split workers by language.
- **Decision model only:** it never generates code or prose — use it for routing /
  extract-style tasks, not code/site/image workers.

## Router-eval regeneration

The task set is shared by every contestant (identical states, identical grading):

```sh
uv run --python 3.11 python scripts/gen_router_eval.py data/eval/states.jsonl
```

If the adapter endpoint is up, run the live comparison:

```sh
uv run --python 3.11 python harness.py batch --batch-dir tasks/router-eval \
    --orchestrator <orchestrator-slug> --worker laya/local-decision
```

The live run was **not** executed during integration: it requires downloading the
checkpoint from Hugging Face (~1–2 GB) plus torch/transformers. The commands above are the
documented path.
