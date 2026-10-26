#!/usr/bin/env bash
# Does a graceful stop let Claude Code flush its transcript? Compare `docker kill` (SIGKILL) with
# `docker stop` (SIGTERM, then SIGKILL after a grace period) on a real Claude Code 2.1.310 run against vLLM.
#
#   cc_stop_exp.sh <kill|stop|stop-init> <scratch-dir> [api_proxy.py]
#     kill       docker kill, as run_batch.py does today
#     stop       docker stop -t 60, container shaped like run_batch.py (bash -c as PID 1, claude its child)
#     stop-init  docker stop -t 60 with --init + TINI_KILL_PROCESS_GROUP=1 (SIGTERM reaches the whole group)
# The stop is sent while a model call is streaming (journal shows a started exchange with a response head).
set -u
MODE=$1; D=$2; PROXY=${3:-$(dirname "$0")/../../e2e/api_proxy.py}
IMAGE=${IMAGE:-agent-scaffold-claude-code:2.1.310}
UPSTREAM=${UPSTREAM:-http://host.docker.internal:18005}
MODEL=${MODEL:-deepseek-v4.1-flash}
mkdir -p "$D/home" "$D/out"; chmod -R 777 "$D"
NAME=ccstop_$(basename "$D")_$$
INIT=(); [ "$MODE" = stop-init ] && INIT=(--init -e TINI_KILL_PROCESS_GROUP=1)
docker run -d --rm --name "$NAME" "${INIT[@]}" --add-host host.docker.internal:host-gateway \
  -v "$D/home:/home/node" -v "$D/out:/app/outputs" -v "$(readlink -f "$PROXY"):/opt/api_proxy.py:ro" \
  -e HOME=/home/node -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= -e all_proxy= -e ALL_PROXY= \
  -e ANTHROPIC_API_KEY=EMPTY -e ANTHROPIC_BASE_URL=http://127.0.0.1:9999 \
  -e ANTHROPIC_MODEL=$MODEL -e ANTHROPIC_SMALL_FAST_MODEL=$MODEL -e ANTHROPIC_DEFAULT_SONNET_MODEL=$MODEL \
  -e ANTHROPIC_DEFAULT_OPUS_MODEL=$MODEL -e ANTHROPIC_DEFAULT_HAIKU_MODEL=$MODEL \
  --entrypoint bash "$IMAGE" -c "umask 0000; \
    UPSTREAM=$UPSTREAM TRACE_FILE=/app/outputs/trace.jsonl python3 /opt/api_proxy.py 9999 > /dev/null 2> /app/outputs/proxy.err & sleep 1; \
    cd /home/node && claude --print --dangerously-skip-permissions --output-format stream-json --verbose \
    'Create a python package in /home/node/proj with 4 modules (geometry, stats, text, io), each with 3 functions and docstrings, plus pytest tests for every function. Write each file with the Write tool one at a time, then run the tests with Bash and fix failures.' \
    > /app/outputs/claude.stream.jsonl 2>&1; echo \"claude exit=\$?\" > /app/outputs/after.txt" > /dev/null

T=$D/out/trace.jsonl; sleep 30
for _ in $(seq 900); do
  s=$(grep -c exchange_start "$T.journal.jsonl" 2>/dev/null || echo 0); h=$(grep -c response_head "$T.journal.jsonl" 2>/dev/null || echo 0)
  f=$(wc -l < "$T" 2>/dev/null || echo 0); [ "$s" -gt "$f" ] && [ "$h" -gt "$f" ] && break; sleep 0.2
done
sleep 2
t0=$(date +%s.%N)
if [ "$MODE" = kill ]; then docker kill "$NAME" > /dev/null; else docker stop -t 60 "$NAME" > /dev/null; fi
echo "mode=$MODE stopped in $(echo "$(date +%s.%N) - $t0" | bc | cut -c1-5)s"
echo "after.txt: $(cat "$D/out/after.txt" 2>/dev/null || echo '<none: shell never ran past claude>')"
echo "stream-json tail event: $(tail -1 "$D/out/claude.stream.jsonl" | head -c 160)"
docker run --rm -v "$D:/x" --entrypoint python3 "$IMAGE" -c "
import json, glob
L = [json.loads(l) for f in glob.glob('/x/home/.claude/projects/*/*.jsonl') for l in open(f)]
print('transcript records:', len(L), '| last 3 types:', [r.get('type') + (':' + r['subtype'] if r.get('subtype') else '') for r in L[-3:]])
ids = [r['message']['id'] for r in L if r.get('type') == 'assistant']
print('assistant message ids:', len(set(ids)), '| api-request records:', sum(r.get('type') == 'api-request' for r in L))
for f in glob.glob('/x/home/.claude/sessions/*.json'):
    print('sessions/' + f.split('/')[-1], json.load(open(f)).get('status'))
"
echo "trace finished: $(wc -l < "$T")  journal starts: $(grep -c exchange_start "$T.journal.jsonl")"
