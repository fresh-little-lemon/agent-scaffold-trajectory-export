"""Find the hash Claude Code uses for api-request tail/blob message hashes, using the proxy trace as ground truth.

    python3 message_hash_probe.py <session.jsonl> <rebuilt-trace.jsonl>
"""
import hashlib
import json
import sys


def canon(value, sort_keys=False):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys)


def strip_cache(value):
    if isinstance(value, dict):
        return {k: strip_cache(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache(v) for v in value]
    return value


CANDIDATES = {
    "compact": lambda m: canon(m),
    "compact_sorted": lambda m: canon(m, True),
    "compact_nocache": lambda m: canon(strip_cache(m)),
    "compact_nocache_sorted": lambda m: canon(strip_cache(m), True),
    "default_json": lambda m: json.dumps(m, ensure_ascii=False),
    "default_json_nocache": lambda m: json.dumps(strip_cache(m), ensure_ascii=False),
}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def main(session_path, trace_path):
    records = [json.loads(line) for line in open(session_path, encoding="utf-8")]
    blobs = {r["hash"]: r["message"] for r in records if r.get("type") == "api-request-blob"}
    for name, fn in CANDIDATES.items():
        ok = sum(sha(fn(m)) == h for h, m in blobs.items())
        print(f"blob self-hash {name}: {ok}/{len(blobs)}")
    wanted = {h for r in records if r.get("type") == "api-request" for h in (r.get("tail") or [])}
    wire_msgs = []
    for line in open(trace_path, encoding="utf-8"):
        body = json.loads(line)["request"].get("body")
        if isinstance(body, dict) and isinstance(body.get("messages"), list):
            wire_msgs.extend(body["messages"])
    print(f"tail hashes: {len(wanted)}, wire messages: {len(wire_msgs)}")
    for name, fn in CANDIDATES.items():
        found = {sha(fn(m)) for m in wire_msgs} & wanted
        print(f"wire messages hashing into tail set with {name}: {len(found)}/{len(wanted)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
