#!/usr/bin/env bash
# Stop the media2threejs runner at a given UTC time (before it starts retry rounds), then run the final
# post-processing. Usage: finish_run.sh HHMM
cd "$(dirname "$0")/../.." || exit 1
until [ "$(date -u +%H%M)" = "$1" ]; do sleep 30; done
P=$(pgrep -f "e2e/run_tasks.py --config e2e/tasks/media2threejs_scale.json" | head -1)
[ -n "$P" ] && kill -USR1 "$P"
sleep 150
python3 e2e/tasks/postprocess_run.py --model-dir e2e/3d-code-gen/claude_code/deepseek-v4.1-flash \
  --vllm-container dsv41_flash > logs/postprocess_final.log 2>&1
