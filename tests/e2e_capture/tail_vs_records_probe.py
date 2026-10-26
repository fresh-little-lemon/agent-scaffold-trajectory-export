"""For tail messages with no api-request-blob, compare the real wire message with the transcript records it came from.

    python3 tail_vs_records_probe.py <session.jsonl> <rebuilt-trace.jsonl>
"""
import hashlib
import json
import sys
from collections import Counter, defaultdict


def strip_cache(value):
    if isinstance(value, dict):
        return {k: strip_cache(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache(v) for v in value]
    return value


def mhash(message):
    return hashlib.sha256(json.dumps(strip_cache(message), ensure_ascii=False, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


def blocks(message):
    content = message.get("content")
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


def main(session_path, trace_path):
    records = [json.loads(line) for line in open(session_path, encoding="utf-8")]
    blobs = {r["hash"] for r in records if r.get("type") == "api-request-blob"}
    tails = [h for r in records if r.get("type") == "api-request" for h in (r.get("tail") or [])]
    missing = [h for h in dict.fromkeys(tails) if h not in blobs]
    wire = {}
    for line in open(trace_path, encoding="utf-8"):
        body = json.loads(line)["request"].get("body")
        if isinstance(body, dict):
            for m in body.get("messages") or []:
                wire.setdefault(mhash(m), m)
    # Transcript candidates: assistant records grouped by message.id; user records one by one.
    by_msg_id = defaultdict(list)
    users = []
    for r in records:
        if r.get("type") == "assistant":
            by_msg_id[r["message"]["id"]].append(r)
        elif r.get("type") == "user":
            users.append(r)
    stats = Counter()
    shown = Counter()
    for h in missing:
        w = wire.get(h)
        if w is None:
            stats["no wire copy (lost request)"] += 1
            continue
        role = w["role"]
        if role == "assistant":
            tool_ids = {b.get("id") for b in blocks(w) if b.get("type") == "tool_use"}
            group = next((g for g in by_msg_id.values()
                          if tool_ids & {b.get("id") for r in g for b in blocks(r["message"])}), None)
            if group is None:
                group = next((g for g in by_msg_id.values() if any(
                    b.get("text") and b.get("text") == wb.get("text") for r in g for b in blocks(r["message"])
                    for wb in blocks(w))), None)
            if group is None:
                stats["assistant: no transcript group"] += 1
                continue
            cand = {"role": "assistant", "content": [b for r in group for b in blocks(r["message"])]}
            ok = mhash(cand) == h
            stats[f"assistant: grouped-by-id exact={ok}"] += 1
            if not ok and shown["a"] < 2:
                shown["a"] += 1
                print("ASSISTANT DIFF wire:", json.dumps(strip_cache(w), ensure_ascii=False)[:700])
                print("               cand:", json.dumps(cand, ensure_ascii=False)[:700])
        else:
            ids = {b.get("tool_use_id") for b in blocks(w) if b.get("type") == "tool_result"}
            rs = [u for u in users if ids & {b.get("tool_use_id") for b in blocks(u["message"])}] if ids else []
            if not rs:
                texts = [b.get("text") for b in blocks(w) if b.get("type") == "text"]
                rs = [u for u in users if any(t and t in json.dumps(u["message"], ensure_ascii=False) for t in texts)]
            kinds = Counter(b.get("type") for b in blocks(w))
            if not rs:
                stats[f"user: no transcript record (blocks {dict(kinds)})"] += 1
                if shown["u0"] < 2:
                    shown["u0"] += 1
                    print("USER w/o record:", json.dumps(strip_cache(w), ensure_ascii=False)[:500])
                continue
            single = {"role": "user", "content": blocks(rs[0]["message"])}
            merged = {"role": "user", "content": [b for u in rs for b in blocks(u["message"])]}
            ok = mhash(single) == h or mhash(merged) == h
            stats[f"user: {len(rs)} record(s) exact={ok}"] += 1
            if not ok and shown["u"] < 3:
                shown["u"] += 1
                print("USER DIFF wire blocks:", [b.get("type") for b in blocks(w)],
                      "| records blocks:", [[b.get("type") for b in blocks(u["message"])] for u in rs])
                print("   wire:", json.dumps(strip_cache(w), ensure_ascii=False)[:900])
                print("   rec0:", json.dumps(rs[0]["message"], ensure_ascii=False)[:600])
    print(f"missing-blob tail messages: {len(missing)}")
    for k, v in stats.most_common():
        print(f"  {v:4d}  {k}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
