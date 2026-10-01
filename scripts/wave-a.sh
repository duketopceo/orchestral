#!/usr/bin/env bash
# Wave-A probe: 4 orchestrators x 6 workers x 3 v2 probe tasks x 1 rep.
# Usage: wave-a.sh <orchestrator-slug>   (one process per orchestrator)
set -u
cd "$(dirname "$0")/.."
export OPENROUTER_API_KEY="$(python3 -c "import json;print(json.load(open('/home/lukekimball/.config/openrouter/keys.json'))['api_key'])")"
ORCH="$1"
TASKS="v2-crossfile-service v2-fanout-records v2-injection-log"
for W in z-ai/glm-5.3-flash xiaomi/mimo-v2.6-flash minimax/minimax-m3 openai/gpt-5.6-luna deepseek/deepseek-v4.1-flash tencent/hy3; do
  echo "=== $ORCH -> $W"
  .venv/bin/python harness.py batch \
    --batch-tasks $TASKS \
    --orchestrator "$ORCH" \
    --worker "$W" \
    --group v2-probe \
    --replicate 1 \
    --judge "~typesafe/jev-latest" \
    --daily-cap 8 \
    --jobs 1 2>&1 | tail -6
done
echo "=== DONE $ORCH"
