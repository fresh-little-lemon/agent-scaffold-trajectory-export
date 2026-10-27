#!/usr/bin/env bash
# Restart a run_tasks.py batch on the server without depending on the ssh session: stop the running runner
# (SIGUSR1: stop dispatching, docker stop its containers), wait until it exits, then start it again. Unfinished
# items are continued in place (resume_unfinished). Usage: restart_batch.sh CONFIG LOG
cd "$(dirname "$0")/../.." || exit 1
CONFIG=$1; LOG=$2
P=$(pgrep -f "run_tasks.py --config $CONFIG" | head -1)
if [ -n "$P" ]; then
  kill -USR1 "$P"
  for _ in $(seq 300); do kill -0 "$P" 2>/dev/null || break; sleep 2; done
fi
echo "$(date -u) old runner ${P:-none} stopped, containers left: $(docker ps --format '{{.Names}}' | grep -c '^rt_')"
exec python3 e2e/run_tasks.py --config "$CONFIG" > "$LOG" 2>&1 < /dev/null
