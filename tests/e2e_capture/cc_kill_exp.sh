#!/usr/bin/env bash
# Real scenario: Claude Code 2.1.310 in its container -> new proxy (inside container) -> DeepSeek vLLM; docker kill mid-run.
D=/tmp/proxyexp_gsy/cc; rm -rf $D; mkdir -p $D/home $D/out; chmod -R 777 $D
NAME=cckill_gsy_$$
docker run -d --rm --name $NAME --add-host host.docker.internal:host-gateway \
  -v $D/home:/home/node -v $D/out:/app/outputs -v /tmp/proxyexp_gsy/api_proxy_new.py:/opt/api_proxy.py:ro \
  -e HOME=/home/node -e http_proxy= -e https_proxy= -e HTTP_PROXY= -e HTTPS_PROXY= -e all_proxy= -e ALL_PROXY= \
  -e ANTHROPIC_API_KEY=EMPTY -e ANTHROPIC_BASE_URL=http://127.0.0.1:9999 \
  -e ANTHROPIC_MODEL=deepseek-v4.1-flash -e ANTHROPIC_SMALL_FAST_MODEL=deepseek-v4.1-flash \
  -e ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4.1-flash -e ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-v4.1-flash -e ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4.1-flash \
  --entrypoint bash agent-scaffold-claude-code:2.1.310 -c "umask 0000; \
    UPSTREAM=http://host.docker.internal:18005 TRACE_FILE=/app/outputs/trace.jsonl python3 /opt/api_proxy.py 9999 > /app/outputs/proxy.out 2> /app/outputs/proxy.err & sleep 1; \
    cd /home/node && claude --print --dangerously-skip-permissions --output-format stream-json --verbose \
    'Create a python package in /home/node/proj with 4 modules (geometry, stats, text, io), each with 3 functions and docstrings, plus pytest tests for every function. Write each file with the Write tool one at a time, then run the tests with Bash and fix failures.' > /app/outputs/claude.stream.jsonl 2>&1"
sleep 45; echo "killing at 45s"; docker kill $NAME > /dev/null; sleep 1
T=$D/out/trace.jsonl
echo "trace records: $(wc -l < $T)  journal starts: $(grep -c exchange_start $T.journal.jsonl)"
python3 /tmp/proxyexp_gsy/rebuild_wire_jsonl.py --trace $T --output $D/rebuilt.jsonl --output-mount $D/out > /dev/null
python3 - <<PY
import json, glob
recs = [json.loads(l) for l in open("$D/rebuilt.jsonl")]
for r in recs:
    c = r["capture"]; s = r["response"].get("stream") or {}
    print(r["sequence"], r["request"]["method"], r["request"]["target"], r["response"]["status"], c["capture_status"], r["response"]["body_format"], s.get("termination"))
tr = glob.glob("$D/home/.claude/projects/*/*.jsonl")
L = [json.loads(l) for l in open(tr[0])] if tr else []
ids = {d["message"]["id"] for d in L if d.get("type") == "assistant"}
gen = [r for r in recs if r["request"]["target"].startswith("/v1/messages") and "count_tokens" not in r["request"]["target"]]
print("transcript assistant message ids:", len(ids), "| /v1/messages exchanges:", len(gen))
for r in gen:
    b = r["response"]["body"] if isinstance(r["response"]["body"], dict) else {}
    print("  n", r["sequence"], r["capture"]["capture_status"], "id", b.get("id"), "in transcript:", b.get("id") in ids)
PY
