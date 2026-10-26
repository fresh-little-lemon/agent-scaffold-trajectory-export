#!/usr/bin/env bash
# Kill experiment: start a proxy against real DeepSeek vLLM, stream a long generation, SIGKILL the proxy mid-stream.
# Usage: kill_exp.sh <proxy.py> <outdir>
P=$1; OUT=$2; rm -rf $OUT; mkdir -p $OUT
UPSTREAM=http://127.0.0.1:18005 TRACE_FILE=$OUT/trace.jsonl python3 $P 19777 > $OUT/proxy.out 2> $OUT/proxy.err &
PID=$!; sleep 1
# 1) a short complete exchange first
curl -s --noproxy '*' http://127.0.0.1:19777/v1/messages -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":20,"messages":[{"role":"user","content":"say hi"}]}' > $OUT/c1.out
# 2) a long streaming exchange, killed after ~4s
curl -s -N --noproxy '*' http://127.0.0.1:19777/v1/messages -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":3000,"stream":true,"messages":[{"role":"user","content":"Write a 2000-word story about a lighthouse."}]}' > $OUT/c2.out &
CPID=$!; sleep 4; kill -9 $PID; wait $CPID
echo "client got $(wc -c < $OUT/c2.out) bytes, $(grep -c content_block_delta $OUT/c2.out) deltas"
echo "trace lines: $(wc -l < $OUT/trace.jsonl 2>/dev/null || echo 0)"; find $OUT -name "*.bin" -o -name "*.journal*" | xargs -r ls -la | awk '{print $5, $9}'
