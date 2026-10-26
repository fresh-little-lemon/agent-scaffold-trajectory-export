#!/usr/bin/env python3
"""Check calls rebuilt from the transcript (transcript_to_calls.py) against the proxy trace (rebuild_wire_jsonl.py).

The trace is ground truth. Each rebuilt call is matched to the wire request with the same message list
(by message hashes) and compared field by field: system (minus the attribution header block, which the
transcript does not store), tools, sampling/context params, cache_control placement, and the response
(content blocks, stop_reason, message id). Wire requests with no rebuilt call are classified (compaction,
interrupted, failed, other) so every gap is accounted for.

    python3 audit_calls_vs_trace.py --calls calls.jsonl --trace traces/13.reconstructed.jsonl [--workspace W]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from transcript_to_calls import message_hash, strip_cache

BILLING_PREFIX = "x-anthropic-billing-header"
WIRE_ONLY_KEYS = {"metadata", "stream"}  # transport/user metadata, not model input
COMPACT_MARKER = "Your task is to create a detailed summary of the conversation so far"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def messages_key(messages: list[dict[str, Any]]) -> str:
    return digest([message_hash(m) for m in messages])


def without_billing(system: Any) -> Any:
    if isinstance(system, list):
        return [b for b in system if not str(b.get("text", "")).startswith(BILLING_PREFIX)]
    return system


def first_diff(a: Any, b: Any, path: str = "") -> str | None:
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}: only in {'rebuilt' if key in a else 'wire'}"
            found = first_diff(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            found = first_diff(x, y, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if a == b else f"{path}: {str(a)[:80]!r} != {str(b)[:80]!r}"


def classify_unmatched(exchange: dict[str, Any]) -> str:
    capture = exchange.get("capture") or {}
    status = (exchange.get("response") or {}).get("status")
    body = exchange["request"]["body"]
    tail = json.dumps((body.get("messages") or [])[-3:], ensure_ascii=False)
    response = (exchange.get("response") or {}).get("body")
    answer = json.dumps(response.get("content"), ensure_ascii=False)[:200] if isinstance(response, dict) else ""
    if COMPACT_MARKER in tail or '"<analysis>' in answer:
        return "compaction_call"
    if capture.get("capture_status") not in (None, "complete"):
        return f"capture_{capture.get('capture_status')}"
    if status and int(status) >= 400:
        return f"http_{status}"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calls", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True, help="rebuilt trace JSONL (rebuild_wire_jsonl.py)")
    parser.add_argument("--workspace", help="only audit calls from this workspace path (as recorded in calls)")
    parser.add_argument("--out", type=Path, help="write the full report JSON here")
    args = parser.parse_args()

    calls = [json.loads(line) for line in args.calls.open(encoding="utf-8")]
    if args.workspace:
        calls = [c for c in calls if c.get("workspace") == args.workspace]
    wire = []
    for line in args.trace.open(encoding="utf-8"):
        exchange = json.loads(line)
        body = exchange["request"].get("body")
        target = exchange["request"].get("target") or ""
        if isinstance(body, dict) and isinstance(body.get("messages"), list) and "count_tokens" not in target:
            wire.append(exchange)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for exchange in wire:
        by_key.setdefault(messages_key(exchange["request"]["body"]["messages"]), []).append(exchange)

    counts: Counter = Counter()
    problems: list[dict[str, Any]] = []
    used: set[int] = set()
    for call in calls:
        counts[f"calls_{call['status']}"] += 1
        if call["status"] == "unresolved_messages":
            problems.append({"call": call["call_id"], "issue": "unresolved", "detail": call["unresolved"][:3]})
            continue
        candidates = [e for e in by_key.get(messages_key(call["request"]["messages"]), []) if id(e) not in used]
        if not candidates:
            counts["call_without_wire_request"] += 1
            problems.append({"call": call["call_id"], "issue": "no wire request with these messages",
                             "status": call["status"]})
            continue
        # Prefer a successful exchange whose response id matches the transcript's.
        rid = (call.get("response") or {}).get("id")
        exchange = next((e for e in candidates if isinstance(e["response"].get("body"), dict)
                         and e["response"]["body"].get("id") == rid), candidates[0])
        used.add(id(exchange))
        counts["matched"] += 1
        body = exchange["request"]["body"]
        rebuilt = call["request"]
        checks = {
            "messages_exact_incl_cache_control": first_diff(rebuilt["messages"], body["messages"]),
            "system_exact": first_diff(strip_cache(rebuilt["system"]), strip_cache(without_billing(body.get("system")))),
            "system_cache_control": first_diff(rebuilt["system"], without_billing(body.get("system"))),
            "tools_exact": first_diff(rebuilt["tools"], body.get("tools")),
            "params_exact": first_diff({k: v for k, v in rebuilt.items() if k not in {"system", "tools", "messages"}},
                                       {k: v for k, v in body.items()
                                        if k not in {"system", "tools", "messages"} | WIRE_ONLY_KEYS}),
        }
        if any(b.get("text", "").startswith(BILLING_PREFIX) for b in body.get("system") or []):
            counts["wire_has_attribution_header_block"] += 1
        response = call.get("response")
        wire_response = exchange["response"].get("body")
        if response and isinstance(wire_response, dict):
            checks["response_content_exact"] = first_diff(strip_cache(response["content"]),
                                                          strip_cache(wire_response.get("content")))
            checks["response_stop_reason"] = first_diff(response.get("stop_reason"), wire_response.get("stop_reason"))
            checks["response_id"] = first_diff(response.get("id"), wire_response.get("id"))
        for name, diff in checks.items():
            counts[f"{name}:{'ok' if diff is None else 'DIFF'}"] += 1
            if diff is not None and sum(1 for p in problems if p.get("check") == name) < 3:
                problems.append({"call": call["call_id"], "check": name, "diff": diff})
    unmatched = Counter(classify_unmatched(e) for e in wire if id(e) not in used)
    report = {"calls": len(calls), "wire_generation_requests": len(wire), "counts": dict(sorted(counts.items())),
              "wire_requests_without_rebuilt_call": dict(unmatched), "examples": problems[:20]}
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
