#!/usr/bin/env bash
# Probe (2.1.310): does a transcript-external official channel expose the raw compact response incl. <analysis>?
# Runs inside agent-scaffold-claude-code:2.1.310 against the local DSv4.1 vLLM (port 18005).
set -u
H=/home/node
mkdir -p $H/.claude/api-bodies $H/probe
cat > $H/.claude/settings.json <<'JSON'
{"hooks":{"PostCompact":[{"matcher":"","hooks":[{"type":"command","command":"cat >> /home/node/probe/postcompact.jsonl; echo >> /home/node/probe/postcompact.jsonl"}]}],
          "PreCompact":[{"matcher":"","hooks":[{"type":"command","command":"cat >> /home/node/probe/precompact.jsonl; echo >> /home/node/probe/precompact.jsonl"}]}]}}
JSON
cd $H
claude --print --dangerously-skip-permissions --output-format json \
  "Read /etc/os-release with the Read tool, then write a 300-word explanation of what each field means." > probe/run1.json 2> probe/run1.err
SID=$(python3 -c "import json;print(json.load(open('probe/run1.json'))['session_id'])")
echo "session $SID"
claude --print --dangerously-skip-permissions --output-format json --resume "$SID" "/compact" > probe/run2.json 2> probe/run2.err
echo "rc=$?"
ls -la $H/.claude/api-bodies | head; ls $H/probe
