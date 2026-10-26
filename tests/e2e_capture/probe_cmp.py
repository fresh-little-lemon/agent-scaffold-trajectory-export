import json, glob, os
H = "/tmp/ccprobe_gsy/home"; D = H + "/.claude/api-bodies"
idx = [json.loads(l) for l in open(D + "/index.jsonl")]
for r in idx: print("INDEX", {k: r.get(k) for k in ("query_source", "message_id", "message_uuid", "request_file", "response_file")})
comp = [r for r in idx if r.get("query_source") == "compact"]
resp_txt = None
if comp:
    rs = json.load(open(D+"/"+os.path.basename(comp[0]["response_file"])))
    print("compact response keys", list(rs.keys()), "content types", [b["type"] for b in rs["content"]])
    resp_txt = "".join(b.get("text", "") for b in rs["content"] if b["type"] == "text")
    th = [b for b in rs["content"] if b["type"] == "thinking"]
    print("thinking blocks:", [(len(b.get("thinking", "")), b.get("thinking", "")[:80]) for b in th])
    print("has <analysis>:", "<analysis>" in resp_txt, "len", len(resp_txt)); print(resp_txt[:300])
    rq = json.load(open(D+"/"+os.path.basename(comp[0]["request_file"])))
    print("compact request keys", list(rq.keys()), "n msgs", len(rq["messages"]), "last block:", json.dumps(rq["messages"][-1]["content"][-1], ensure_ascii=False)[:150])
pc = [json.loads(l) for l in open(H + "/probe/postcompact.jsonl") if l.strip()]
cs = pc[0]["compact_summary"]; print("POSTCOMPACT has <analysis>:", "<analysis>" in cs, "len", len(cs), "== raw response text:", cs == resp_txt); print(cs[:200])
import sys; tr = glob.glob(H + "/.claude/projects/*/*.jsonl")[0]
L = [json.loads(l) for l in open(tr)]
print("transcript types", sorted({d["type"] for d in L}))
s = [d for d in L if d.get("isCompactSummary")]
print("transcript summary has <analysis>:", "<analysis>" in json.dumps(s, ensure_ascii=False))
reqs = [d for d in L if d["type"] == "api-request"]; print("transcript api-request querySources:", [d.get("querySource") for d in reqs])
# thinking redaction check on a main-loop response
for r in idx:
    if r.get("query_source") != "compact":
        x = json.load(open(D+"/"+os.path.basename(r["response_file"])))
        print("main resp blocks", [(b["type"], len(b.get("thinking", b.get("text", "")) or "")) for b in x["content"]])
        break
rq_files = sorted(glob.glob(D + "/*.request.json")); print("requests on disk", len(rq_files), "responses", len(glob.glob(D + "/*.response.json")))
for f in rq_files:
    q = json.load(open(f))
    for m in q["messages"]:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if b["type"] == "thinking": print("history thinking in request", os.path.basename(f), len(b.get("thinking", "")), repr(b.get("thinking", "")[:40]))
