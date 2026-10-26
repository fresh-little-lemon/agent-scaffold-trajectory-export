"""Debug helper: for rebuilt calls that match no wire request, find the closest wire request and show the first differing message.

    python3 calls_first_mismatch.py calls.jsonl rebuilt-trace.jsonl [N]
"""
import json
import sys

sys.path.insert(0, ".")
from transcript_to_calls import message_hash, strip_cache  # noqa: E402


def main(calls_path, trace_path, limit=4):
    calls = [json.loads(line) for line in open(calls_path, encoding="utf-8")]
    wire = []
    for line in open(trace_path, encoding="utf-8"):
        body = json.loads(line)["request"].get("body")
        if isinstance(body, dict) and isinstance(body.get("messages"), list):
            wire.append([message_hash(m) for m in body["messages"]] + [None])
            wire[-1][-1] = body["messages"]
    shown = 0
    for call in calls:
        mine = [message_hash(m) for m in call["request"]["messages"]]
        best, best_len, best_msgs = None, -1, None
        for entry in wire:
            hashes, msgs = entry[:-1], entry[-1]
            common = next((i for i, (a, b) in enumerate(zip(mine, hashes)) if a != b), min(len(mine), len(hashes)))
            if common > best_len:
                best, best_len, best_msgs = hashes, common, msgs
        if best == mine:
            continue
        print(f"call {call['call_id'][:8]} line {call['line']} keep-chain len {len(mine)}; closest wire len {len(best)}, "
              f"common prefix {best_len}")
        if best_len < len(mine) and best_len < len(best):
            print("   rebuilt:", json.dumps(call["request"]["messages"][best_len], ensure_ascii=False)[:400])
            print("   wire   :", json.dumps(strip_cache(best_msgs[best_len]), ensure_ascii=False)[:400])
        shown += 1
        if shown >= limit:
            break


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 4)
