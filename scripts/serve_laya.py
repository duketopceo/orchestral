#!/usr/bin/env python3
"""Serve Laya as an OpenAI-compatible chat endpoint on 127.0.0.1.

Laya (github.com/NandhaKishorM/laya, Apache-2.0) is a non-autoregressive typed-decision
model with a Python API only — no server. This thin adapter wraps `laya.Agent.system_one`
so orchestral (or any OpenAI client) can use it as a `worker` model.

It speaks POST /v1/chat/completions (and GET /v1/models). Laya does not generate text,
so the adapter maps a chat request onto a single `choice` question:

  - options come from a JSON body field "options" (list of option keys, optionally
    {"options": [...], "criteria": {...}}) or, if absent, are parsed from the last
    user message's "Return ONLY ... one of: a, b, c" / JSON-enum hint (the same
    shape orchestral's router-eval tasks emit);
  - the response "content" is the chosen option's key, plus a metadata block with
    Laya's calibrated probabilities and confidence so the harness can log them.

Usage:
    uv run --python 3.11 --with torch --with transformers --with safetensors \
        --with huggingface_hub --with numpy \
        python scripts/serve_laya.py --port 8090 \
        [--model convaiinnovations/laya] [--subfolder multilingual|typed-decisions]

    # CPU is fine (421M params, single parallel forward pass per request).
    # First call downloads the checkpoint from HF (roughly 1-2 GB; set HF_TOKEN if gated).

Then point orchestral at it (see models/laya.yaml):
    uv run --python 3.11 python harness.py batch --batch-dir tasks/router-eval \
        --orchestrator <orchestrator-slug> --worker laya/local-decision

Note: the endpoint answers any question with Laya's top choice — it is a decision
router, not a code generator. Use it for routing/extract/choice tasks only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# transformers probes for TensorFlow at import time; Laya is torch-only.
# (Same guard laya's own tests set — avoids a macOS/TF abseil deadlock.)
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL_ID = "convaiinnovations/laya"
SUBFOLDER = None
AGENT = None  # lazy: loaded on first request so --help and imports stay cheap

_ENUM_RE = re.compile(r"one of[:\s]+([^.]+)", re.IGNORECASE)


def get_agent():
    global AGENT
    if AGENT is None:
        import laya

        print(f"loading laya checkpoint {MODEL_ID} (subfolder={SUBFOLDER}) ...", flush=True)
        AGENT = laya.load(MODEL_ID, subfolder=SUBFOLDER)
        print("ready.", flush=True)
    return AGENT


def parse_options(body: dict) -> tuple[list[str], dict]:
    """Options come from request body 'options', else from the prompt's enum hint."""
    explicit = body.get("options")
    if isinstance(explicit, list) and explicit:
        crit = body.get("criteria") if isinstance(body.get("criteria"), dict) else None
        return [str(o) for o in explicit], crit or {}
    messages = body.get("messages", [])
    text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
    m = _ENUM_RE.search(text)
    if m:
        opts = [o.strip().strip('"\'').rstrip(",") for o in m.group(1).split(",")]
        return [o for o in opts if o], {}
    raise ValueError(
        "no options found: pass a JSON body field 'options' (list) or phrase the "
        "prompt as 'Return ONLY ... one of: a, b, c'"
    )


def parse_state(body: dict) -> str:
    """Everything after the option-enum hint in the last user message is the state."""
    messages = body.get("messages", [])
    parts = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
    return parts[-1] if parts else ""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # stdlib BaseHTTPRequestHandler override — name is not ours to change
        if self.path.rstrip("/") in ("/v1/models", ""):
            self._send(200, {"object": "list", "data": [{"id": "laya/local-decision", "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # stdlib BaseHTTPRequestHandler override — name is not ours to change
        if not self.path.rstrip("/").endswith("chat/completions"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            options, criteria = parse_options(body)
            state = parse_state(body)
            agent = get_agent()
            question = {
                "route": {
                    "type": "choice",
                    "instructions": "Choose the best option for the given state.",
                    "criteria": criteria or {o: o for o in options},
                }
            }
            result = agent.system_one(state, question)
            ans = result["answers"]["route"]
            chosen = str(ans["choice"])
            content = json.dumps(
                {"choice": chosen, "probabilities": ans["probabilities"], "confidence": ans["confidence"]}
            )
            self._send(
                200,
                {
                    "id": "chatcmpl-laya",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "laya/local-decision",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": result["usage"]["input_tokens"],
                        "completion_tokens": 0,
                        "total_tokens": result["usage"]["input_tokens"],
                    },
                },
            )
        except Exception as exc:  # one endpoint, surface anything
            self._send(500, {"error": {"message": str(exc), "type": "laya_adapter_error"}})

    def log_message(self, format: str, *args) -> None:  # stdlib signature — args shadow builtins on purpose
        pass


def main() -> int:
    global MODEL_ID, SUBFOLDER
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--model", default=MODEL_ID, help="HF repo id or local path")
    p.add_argument("--subfolder", default=None, choices=["multilingual", "typed-decisions"])
    args = p.parse_args()
    MODEL_ID, SUBFOLDER = args.model, args.subfolder
    get_agent()  # fail fast on a bad model id / missing weights
    print(f"serving OpenAI-compatible endpoint on http://{args.host}:{args.port}/v1", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
