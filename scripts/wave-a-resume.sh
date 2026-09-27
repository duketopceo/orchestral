#!/usr/bin/env bash
# Wave-A resume: re-run every (worker, task) cell lacking a finished run
# for the given orchestrator. Bug-artifact failures were deleted first.
set -u
cd "$(dirname "$0")/.."
export OPENROUTER_API_KEY="$(python3 -c "import json;print(json.load(open('/home/lukekimball/.config/openrouter/keys.json'))['api_key'])")"
ORCH="$1"
MISSING=$(.venv/bin/python - "$ORCH" <<'PY'
import sqlite3, sys
orch = sys.argv[1]
workers = ["z-ai/glm-5.3-flash","xiaomi/mimo-v2.6-flash","minimax/minimax-m3",
           "openai/gpt-5.6-luna","deepseek/deepseek-v4.1-flash","tencent/hy3"]
tasks = ["v2-crossfile-service","v2-fanout-records","v2-injection-log"]
db = sqlite3.connect("runs/index.db")
done = {(w,t) for (w,t) in db.execute(
    "SELECT worker, task_id FROM runs WHERE run_group='v2-probe' AND dry_run=0 "
    "AND status='finished' AND orchestrator=?", (orch,))}
for w in workers:
    for t in tasks:
        if (w,t) not in done:
            print(f"{w} {t}")
PY
)
echo "$MISSING" | while read -r W T; do
  [ -z "$W" ] && continue
  echo "=== $ORCH -> $W -> $T"
  .venv/bin/python harness.py run --task "$T" --orchestrator "$ORCH" \
    --worker "$W" --group v2-probe --judge "~typesafe/jev-latest" \
    --daily-cap 8 2>&1 | tail -3
done
echo "=== DONE $ORCH"
