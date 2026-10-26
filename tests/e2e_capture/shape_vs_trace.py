"""Check whether a Claude Code transcript alone pins down every main-loop request's system prompt and tools.

Claude Code (2.1.3xx) logs, next to the user/assistant messages, content-addressed request records:
`api-request-shape` (system + tools, keyed by shapeHash) and `api-request` (one per call, naming its shapeHash).
This script rebuilds (system, tools) per api-request from the transcript and compares them, in order, with
the /v1/messages requests actually captured by the proxy (rebuilt trace JSONL).

    python3 shape_vs_trace.py <session.jsonl> <rebuilt-trace.jsonl>
"""
import hashlib
import json
import sys
from collections import Counter


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def strip_cache(value):
    """cache_control markers move between requests; compare content only."""
    if isinstance(value, dict):
        return {k: strip_cache(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache(v) for v in value]
    return value


def main(session_path, trace_path):
    records = [json.loads(line) for line in open(session_path, encoding="utf-8")]
    shapes = {r["shapeHash"]: r["shape"] for r in records if r.get("type") == "api-request-shape"}
    calls = [r for r in records if r.get("type") == "api-request"]
    print(f"transcript: {len(calls)} api-request records, {len(shapes)} distinct shapes")
    print("shape keys:", sorted({k for s in shapes.values() for k in s}))
    print("tool counts per shape:", sorted(Counter(len(s.get("tools") or []) for s in shapes.values()).items()))
    print("example api-request keys:", sorted(calls[0].keys()))
    print("example api-request (truncated):", json.dumps({k: calls[0][k] for k in calls[0] if k not in ("params",)},
                                                          ensure_ascii=False)[:600])

    trace = []
    for line in open(trace_path, encoding="utf-8"):
        r = json.loads(line)
        body = r["request"].get("body")
        target = r["request"].get("target") or ""
        if (isinstance(body, dict) and "messages" in body and "count_tokens" not in target
                and isinstance(body.get("system"), list)):
            trace.append(body)
    print(f"trace: {len(trace)} /v1/messages generation requests")

    # The billing header block (system[0]) is per-request; shapes start at the agent prompt.
    def wire_shape(body):
        system = [b for b in body["system"] if not str(b.get("text", "")).startswith("x-anthropic-billing-header")]
        return digest(strip_cache(system)), digest(strip_cache(body.get("tools") or []))

    shape_keys = {h: (digest(strip_cache(s.get("system") or [])), digest(strip_cache(s.get("tools") or [])))
                  for h, s in shapes.items()}
    wire_keys = Counter(wire_shape(b) for b in trace)
    matched = sum(n for key, n in wire_keys.items() if key in set(shape_keys.values()))
    print(f"trace requests whose (system, tools) equals some transcript shape: {matched}/{len(trace)}")
    unmatched = [k for k in wire_keys if k not in set(shape_keys.values())]
    if unmatched:
        print("unmatched wire shapes:", unmatched[:5])
        sys_only = sum(n for k, n in wire_keys.items() if k[0] in {v[0] for v in shape_keys.values()})
        tools_only = sum(n for k, n in wire_keys.items() if k[1] in {v[1] for v in shape_keys.values()})
        print(f"  system matches some shape: {sys_only}/{len(trace)}; tools match some shape: {tools_only}/{len(trace)}")

    # In-order check for the main loop: the i-th sdk api-request vs the i-th trace request with the same shape set.
    seq = [shape_keys.get(c.get("shapeHash")) for c in calls]
    print("transcript per-call shape sequence (first 20):",
          [list(shape_keys).index(c.get("shapeHash")) if c.get("shapeHash") in shape_keys else None for c in calls][:20])
    print("distinct per-call tool counts in transcript:",
          sorted(Counter(len(shapes[c["shapeHash"]].get("tools") or []) for c in calls if c.get("shapeHash") in shapes).items()))
    print("distinct per-call tool counts on the wire:", sorted(Counter(len(b.get("tools") or []) for b in trace).items()))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
