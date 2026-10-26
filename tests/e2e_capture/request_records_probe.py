"""Inspect Claude Code's api-request / api-request-blob records to learn how they encode each request.

    python3 request_records_probe.py <session.jsonl>
"""
import json
import sys


def short(value, n=160):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= n else text[:n] + f"...({len(text)} chars)"


def main(path):
    records = [json.loads(line) for line in open(path, encoding="utf-8")]
    blobs = {r["hash"]: r for r in records if r.get("type") == "api-request-blob"}
    print("blob keys:", sorted({k for r in blobs.values() for k in r}))
    calls = [(i, r) for i, r in enumerate(records) if r.get("type") == "api-request"]
    for i, r in calls[:6] + calls[-3:]:
        print(f"--- line {i} id={r['id'][:8]} base={str(r.get('base'))[:8]} keep={r.get('keep')} "
              f"tail={len(r.get('tail') or [])} digest={str(r.get('requestDigest'))[:16]} src={r.get('querySource')}")
        print("   params:", short(r.get("params"), 400))
        print("   cacheControl:", short(r.get("cacheControl"), 300))
        for h in (r.get("tail") or [])[:3]:
            b = blobs.get(h)
            print("   tail blob", h[:8], "->", short(b.get("message") if b else "MISSING", 200))
    ids = {r["id"] for _, r in calls}
    print("base values that are api-request ids:", sum(1 for _, r in calls if r.get("base") in ids),
          "/", sum(1 for _, r in calls if r.get("base")))
    print("missing tail blobs:", sum(1 for _, r in calls for h in (r.get("tail") or []) if h not in blobs))


if __name__ == "__main__":
    main(sys.argv[1])
