#!/usr/bin/env python3
"""Generate orchestral task specs for the fleet-models router eval.

Input:  a states JSONL file: {"id", "utterance", "focused_app", "options", "expected"}
Output: tasks/router-eval/<id>.yaml  — one extract-type task per decision state,
        deterministic expected answer, so the harness grades pass/fail without a judge.

The same task set grades EVERY contestant (Jev via OpenRouter, local MLX router,
any frontier model) — identical states, identical grading. That's the gate.

Usage:
  python3 scripts/gen_router_eval.py data/eval/states.jsonl
"""
import json
import sys
from pathlib import Path

TEMPLATE = """id: router-eval-{eid}
type: extract
prompt: |
  You are a desktop assistant decision router. Given the state, decide which
  app to open. Return ONLY a JSON object: {{"choice": "<one of: {opts}>"}}.

  State:
  utterance: "{utterance}"
  focused_app: "{focused_app}"
validation: []
assets: []
metadata:
  fields:
    choice: {{type: str, required: true, enum: [{enum_opts}]}}
  expected:
    choice: "{expected}"
"""


def gen(states_path: str) -> int:
    out_dir = Path("tasks/router-eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for line in Path(states_path).read_text().splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        opts = s["options"]
        yaml = TEMPLATE.format(
            eid=s["id"],
            utterance=s["utterance"].replace('"', "'"),
            focused_app=s.get("focused_app", "none"),
            opts=", ".join(opts),
            enum_opts=", ".join(f'"{o}"' for o in opts),
            expected=s["expected"],
        )
        (out_dir / f"{s['id']}.yaml").write_text(yaml)
        n += 1
    print(f"wrote {n} task specs -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(gen(sys.argv[1] if len(sys.argv) > 1 else "data/eval/states.jsonl"))
