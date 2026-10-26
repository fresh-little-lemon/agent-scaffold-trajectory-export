#!/usr/bin/env bash
# Real-vLLM experiments for the write-ahead proxy. All output under $D.
D=/tmp/proxyexp_gsy/new; rm -rf $D; mkdir -p $D; cd /tmp/proxyexp_gsy
T=$D/trace.jsonl
start() { UPSTREAM=http://127.0.0.1:18005 TRACE_FILE=$T python3 api_proxy_new.py 19778 >> $D/proxy.out 2>> $D/proxy.err & PID=$!; sleep 1; }
long() { curl -s -N --noproxy '*' http://127.0.0.1:19778/v1/messages -H 'content-type: application/json' "$@" \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":3000,"stream":true,"messages":[{"role":"user","content":"Write a 2000-word story about a lighthouse."}]}'; }
short() { curl -s --noproxy '*' http://127.0.0.1:19778/v1/messages -H 'content-type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":20,"messages":[{"role":"user","content":"say hi"}]}' > /dev/null; }

echo "## E1 run A: complete call, then SIGKILL mid-stream"
start; short; long > $D/e1.client & C=$!; sleep 4; kill -9 $PID; wait $C
echo "client received $(wc -c < $D/e1.client) bytes"
echo "## E2 run B (same trace = restart): complete call, then SIGTERM mid-stream"
start; short; long > $D/e2.client & C=$!; sleep 4; kill -TERM $PID; wait $C
echo "client received $(wc -c < $D/e2.client) bytes"
echo "## E3 run C: client disconnects after 3s"
start; long --max-time 3 > $D/e3.client; sleep 1; echo "client received $(wc -c < $D/e3.client) bytes"; kill -TERM $PID; sleep 0.5

echo "## files"; ls $T.wire; for r in $T.wire/*; do echo "$(basename $r): $(ls $r | tr '\n' ' ')"; done
echo "## trace records"; python3 -c "
import json
for l in open('$T'): r=json.loads(l); print(r['proxy_run_id'][-8:], r['n'], r['response_status'], 'complete=',r['response_complete'], 'client_disc=',r['client_disconnected'], 'phase=',r['failure_phase'], 'bytes=',r['response_body_bytes'])"
echo "## journal events"; python3 -c "
import json,collections
print(collections.Counter(json.loads(l)['event'] for l in open('$T.journal.jsonl')))
for l in open('$T.journal.jsonl'):
  e=json.loads(l)
  if e['event']=='proxy_stop': print('proxy_stop', e['proxy_run_id'][-8:], 'signal', e['signal'], 'inflight', [(i['n'], i['target']) for i in e['inflight']])"
python3 rebuild_wire_jsonl.py --trace $T --output $D/rebuilt.jsonl
echo "## rebuilt"; python3 -c "
import json
for l in open('$D/rebuilt.jsonl'):
  r=json.loads(l); c=r['capture']; s=r['response'].get('stream') or {}; b=r['response']['body'] or {}
  txt=''.join(x.get('text','') for x in b.get('content',[])) if isinstance(b,dict) else ''
  print(r['proxy_run_id'][-8:], r['sequence'], c['capture_status'], r['response']['body_format'], s.get('termination'), 'text_chars=',len(txt), 'resp_bytes=',r['response']['body_integrity'].get('byte_length'), (c.get('interrupted') or {}).get('by',''))"
echo "## E1 on-disk partial vs what client got"; python3 - <<PY
import json,glob,os
recs=[json.loads(l) for l in open('$D/rebuilt.jsonl')]
for name,r in (('e1',recs[1]),('e2',recs[3])):
    disk=open(r['request']['url'] and [x for x in [json.loads(l) for l in open('$T.journal.jsonl')] if x.get('event')=='exchange_start' and x['proxy_run_id']==r['proxy_run_id'] and x['n']==r['sequence']][0]['response_body_file'],'rb').read()
    cli=open('$D/%s.client'%name,'rb').read()
    print(name, 'disk',len(disk),'client',len(cli),'client is prefix of disk:',disk.startswith(cli),'disk is prefix of client:',cli.startswith(disk))
PY
