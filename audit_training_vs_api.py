#!/usr/bin/env python3
"""Compare transcript-derived Swift rows with reconstructed API exchanges.

The API trace is read-only audit evidence. It is never used to build or repair
the training rows. Reports contain IDs, counts, and digests, not prompt text.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import difflib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


def digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def json_arg(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces = []
        for block in value:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                pieces.append(str(block.get("text", "")))
            elif kind in {"image", "input_image"}:
                pieces.append("<image>")
        return "".join(pieces)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def system_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces = []
        for block in value:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                pieces.append(str(block.get("text", "")))
        return "\n".join(piece for piece in pieces if piece)
    return content_text(value)


def canonical_tool_content(value: Any) -> Any:
    if isinstance(value, list):
        result = []
        for block in value:
            if isinstance(block, dict) and block.get("type") in {"image", "input_image"}:
                result.append({"type": block.get("type")})
            else:
                result.append(canonical_tool_content(block))
        return result
    if isinstance(value, dict):
        if value.get("type") in {"image", "input_image"}:
            return {"type": value.get("type")}
        return {key: canonical_tool_content(item) for key, item in value.items()}
    return value if value is not None else ""


def image_hashes(value: Any) -> list[str]:
    found = []
    if isinstance(value, dict):
        if value.get("type") in {"image", "input_image"}:
            source = value.get("source")
            data = source.get("data") if isinstance(source, dict) else None
            if not data and isinstance(value.get("file"), dict):
                data = value["file"].get("base64")
            if isinstance(data, str):
                try:
                    found.append(hashlib.sha256(base64.b64decode(data, validate=False)).hexdigest())
                except (ValueError, TypeError):
                    pass
            path = value.get("path")
            if isinstance(path, str):
                candidate = Path(path).stem
                if re.fullmatch(r"[0-9a-f]{64}", candidate):
                    found.append(candidate)
        for child in value.values():
            found.extend(image_hashes(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(image_hashes(child))
    return found


def image_asset_paths(value: Any) -> list[str]:
    found = []
    if isinstance(value, dict):
        if value.get("type") in {"image", "input_image"} and isinstance(value.get("path"), str):
            found.append(value["path"])
        for child in value.values():
            found.extend(image_asset_paths(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(image_asset_paths(child))
    return found


def verify_image_assets(value: Any, asset_root: Path) -> dict[str, Any]:
    paths = image_asset_paths(value)
    hashes = []
    missing = []
    for relative in paths:
        path = asset_root / relative
        try:
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            missing.append(relative)
    return {"paths": paths, "sha256": hashes, "missing": missing}


def tool_result_event(tool_id: Any, content: Any, is_error: bool = False) -> dict[str, Any]:
    canonical = canonical_tool_content(content)
    return {"kind": "tool_result", "id": str(tool_id or ""),
            "content_sha256": digest(canonical),
            "content_size": len(json.dumps(canonical, ensure_ascii=False, sort_keys=True)),
            "image_sha256": image_hashes(content),
            "is_error": bool(is_error)}


def request_events(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Anthropic Messages or OpenAI chat history into semantic events."""
    result: list[dict[str, Any]] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        content = message.get("content")
        if role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "text" and block.get("text"):
                        result.append({"kind": "assistant_text", "text": str(block["text"])})
                    elif kind == "thinking" and block.get("thinking"):
                        result.append({"kind": "assistant_thinking", "text": str(block["thinking"])})
                    elif kind == "tool_use":
                        result.append({"kind": "tool_call", "id": str(block.get("id", "")),
                                       "name": block.get("name"), "arguments": block.get("input", {})})
                    elif kind in {"redacted_thinking", "server_tool_use", "web_search_tool_result"}:
                        result.append({"kind": "nontrainable_block", "type": kind,
                                       "digest": digest(block)})
            elif content:
                result.append({"kind": "assistant_text", "text": content_text(content)})
            reasoning = message.get("reasoning_content")
            if reasoning:
                result.insert(max(0, len(result) - len(message.get("tool_calls") or [])),
                              {"kind": "assistant_thinking", "text": str(reasoning)})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                result.append({"kind": "tool_call", "id": str(call.get("id", "")),
                               "name": function.get("name"),
                               "arguments": json_arg(function.get("arguments", {}))})
        elif role == "tool":
            result.append(tool_result_event(message.get("tool_call_id"), content,
                                            message.get("is_error", False)))
        elif role == "user":
            if isinstance(content, list):
                text = content_text(content)
            else:
                text = content_text(content)
            if text:
                result.append({"kind": "user_text", "text": text})
            for block in message.get("content_blocks") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result.append(tool_result_event(block.get("tool_use_id"),
                                                    block.get("content"),
                                                    block.get("is_error", False)))
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result.append(tool_result_event(block.get("tool_use_id"),
                                                        block.get("content"),
                                                        block.get("is_error", False)))
        elif role in {"system", "developer"}:
            text = content_text(content)
            result.append({"kind": f"{role}_text", "text": text})
    return result


def response_events(body: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """Flatten one completed API response to expected transcript target events."""
    response_id = body.get("id")
    events: list[dict[str, Any]] = []
    if isinstance(body.get("content"), list):  # Anthropic Messages
        for block in body["content"]:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                events.append({"kind": "assistant_text", "text": str(block.get("text", ""))})
            elif kind == "thinking":
                events.append({"kind": "assistant_thinking", "text": str(block.get("thinking", ""))})
            elif kind == "tool_use":
                events.append({"kind": "tool_call", "id": str(block.get("id", "")),
                               "name": block.get("name"), "arguments": block.get("input", {})})
            else:
                events.append({"kind": "nontrainable_block", "type": kind,
                               "digest": digest(block)})
        return (str(response_id) if response_id else None), events

    choices = body.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        response_id = response_id or message.get("id")
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            events.append({"kind": "assistant_thinking", "text": str(reasoning)})
        if message.get("content"):
            events.append({"kind": "assistant_text", "text": content_text(message["content"])})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            events.append({"kind": "tool_call", "id": str(call.get("id", "")),
                           "name": function.get("name"),
                           "arguments": json_arg(function.get("arguments", {}))})
        return (str(response_id) if response_id else None), events

    # OpenAI Responses API semantic output.
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call", "custom_tool_call"}:
            events.append({"kind": "tool_call", "id": str(item.get("call_id", item.get("id", ""))),
                           "name": item.get("name"),
                           "arguments": json_arg(item.get("arguments", item.get("input", {})))})
        elif item.get("type") == "message":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    events.append({"kind": "assistant_text", "text": str(block.get("text", ""))})
                elif isinstance(block, dict) and block.get("type") == "reasoning":
                    events.append({"kind": "assistant_thinking", "text": content_text(block.get("summary", []))})
    return (str(response_id) if response_id else None), events


def train_events(row: dict[str, Any], descriptors: list[dict[str, Any]],
                 message_id: str | None = None, before_index: int | None = None) -> list[dict[str, Any]]:
    events = []
    for index, message in enumerate(row.get("messages") or []):
        descriptor = descriptors[index] if index < len(descriptors) else {}
        if before_index is not None and index >= before_index:
            break
        if message_id is not None and descriptor.get("message_id") != message_id:
            continue
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            events.append({"kind": "user_text", "text": str(content)})
        elif role == "assistant":
            text = str(content)
            if text.startswith("<think>") and text.endswith("</think>"):
                events.append({"kind": "assistant_thinking", "text": text[7:-8]})
            else:
                events.append({"kind": "assistant_text", "text": text})
        elif role == "tool_call":
            value = json_arg(content)
            if not isinstance(value, dict):
                value = {}
            events.append({"kind": "tool_call",
                           "id": str(descriptor.get("tool_use_id", "")),
                           "name": value.get("name", descriptor.get("tool_name")),
                           "arguments": value.get("arguments", {})})
        elif role == "tool_response":
            value = json_arg(content)
            if isinstance(value, dict):
                events.append(tool_result_event(value.get("tool_use_id"),
                                                value.get("content", ""),
                                                value.get("is_error", False)))
    return events


def normalized_tools(value: Any) -> list[dict[str, Any]] | None:
    value = json_arg(value)
    if not isinstance(value, list):
        return None
    normalized = []
    for tool in value:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            normalized.append({"name": fn.get("name"), "description": fn.get("description", ""),
                               "schema": fn.get("parameters", {})})
        elif tool.get("name"):
            normalized.append({"name": tool.get("name"), "description": tool.get("description", ""),
                               "schema": tool.get("input_schema", {})})
    return normalized


def first_difference(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, Any] | None:
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left != right:
            result = {"index": index, "expected_kind": left.get("kind"),
                      "actual_kind": right.get("kind"), "expected_sha256": digest(left),
                      "actual_sha256": digest(right),
                      "different_fields": sorted(key for key in set(left) | set(right)
                                                  if left.get(key) != right.get(key))}
            for key in ("text", "content"):
                left_text, right_text = left.get(key), right.get(key)
                if isinstance(left_text, str) and isinstance(right_text, str):
                    prefix = 0
                    for a, b in zip(left_text, right_text):
                        if a != b:
                            break
                        prefix += 1
                    result.update({"expected_chars": len(left_text), "actual_chars": len(right_text),
                                   "common_prefix_chars": prefix})
                    break
            return result
    if len(expected) != len(actual):
        left = expected[len(actual)] if len(expected) > len(actual) else None
        right = actual[len(expected)] if len(actual) > len(expected) else None
        return {"index": min(len(expected), len(actual)),
                "expected_kind": left.get("kind") if left else None,
                "actual_kind": right.get("kind") if right else None,
                "expected_sha256": digest(left) if left else None,
                "actual_sha256": digest(right) if right else None}
    return None


def ordered_coverage(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, Any]:
    """Count request events found in order inside the transcript prefix."""
    cursor = 0
    matched = 0
    missing = []
    for index, event in enumerate(expected):
        signature = digest(event)
        found = next((position for position in range(cursor, len(actual))
                      if digest(actual[position]) == signature), None)
        if found is None:
            missing.append({"index": index, "kind": event.get("kind"), "sha256": signature})
        else:
            matched += 1
            cursor = found + 1
    return {"matched_request_events_in_order": matched,
            "unmatched_request_events": missing,
            "extra_training_prefix_events": max(0, len(actual) - matched)}


def coalesced_coverage(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare split transcript turns after coalescing adjacent semantic fields."""
    result: dict[str, Any] = {}
    for kind in ("user_text", "assistant_text", "assistant_thinking"):
        left = "".join(str(item.get("text", "")) for item in expected if item.get("kind") == kind)
        right = "".join(str(item.get("text", "")) for item in actual if item.get("kind") == kind)
        prefix = 0
        for a, b in zip(left, right):
            if a != b:
                break
            prefix += 1
        result[kind] = {"expected_chars": len(left), "training_chars": len(right),
                        "exact": left == right, "training_contains_expected": bool(left and left in right),
                        "similarity_ratio": round(difflib.SequenceMatcher(a=left, b=right).ratio(), 6),
                        "expected_sha256": digest(left), "training_sha256": digest(right),
                        "common_prefix_chars": prefix}
    for kind in ("tool_call", "tool_result"):
        expected_ids = [str(item.get("id", "")) for item in expected if item.get("kind") == kind]
        actual_ids = [str(item.get("id", "")) for item in actual if item.get("kind") == kind]
        result[kind] = {"expected_count": len(expected_ids), "training_count": len(actual_ids),
                        "missing_ids": sorted(set(expected_ids) - set(actual_ids)),
                        "extra_ids": sorted(set(actual_ids) - set(expected_ids))}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--train", required=True, type=Path)
    parser.add_argument("--reconstructed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    manifest = list(read_jsonl(args.manifest))
    rows = list(read_jsonl(args.train))
    written = [item for item in manifest if item.get("training_row_written")]
    if len(written) != len(rows):
        parser.error(f"train/manifest row mismatch: {len(rows)} train rows, {len(written)} written manifest entries")

    row_info: dict[str, dict[str, Any]] = {}
    assistant_owner: dict[str, str] = {}
    train_tool_events: dict[str, dict[str, Any]] = {}
    for row, meta in zip(rows, written):
        descriptors = meta.get("messages") or []
        source = str(meta.get("source_transcript", ""))
        row_info[source] = {"row": row, "meta": meta, "descriptors": descriptors}
        for desc in descriptors:
            if desc.get("role") in {"assistant", "tool_call"} and desc.get("message_id"):
                assistant_owner.setdefault(str(desc["message_id"]), source)
            if desc.get("role") == "tool_call" and desc.get("tool_use_id"):
                tool_id = str(desc["tool_use_id"])
                index = int(desc.get("message_index", -1))
                if 0 <= index < len(row.get("messages", [])):
                    parsed = json_arg(row["messages"][index].get("content", "{}"))
                    train_tool_events.setdefault(tool_id, {
                        "name": parsed.get("name") if isinstance(parsed, dict) else None,
                        "arguments": parsed.get("arguments", {}) if isinstance(parsed, dict) else {},
                        "source": source,
                    })

    status_counts: Counter[str] = Counter()
    endpoint_calls = 0
    successful_response_ids = 0
    unmatched_response_records = []
    request_tool_results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    request_tool_result_conflicts = Counter()
    api_system_variants: dict[str, dict[str, Any]] = {}
    api_tool_variants: dict[str, dict[str, Any]] = {}
    capture_integrity = Counter()
    response_checks = []
    context_checks = []
    tool_call_checks = []
    system_checks = []
    schema_checks = []
    for record in read_jsonl(args.reconstructed):
        request_integrity = (record.get("request") or {}).get("body_integrity") or {}
        response_integrity = (record.get("response") or {}).get("body_integrity") or {}
        capture_integrity["request_bodies_checked"] += bool(request_integrity.get("available"))
        capture_integrity["response_bodies_checked"] += bool(response_integrity.get("available"))
        capture_integrity["request_body_hash_match"] += bool(request_integrity.get("matches_trace_metadata"))
        capture_integrity["response_body_hash_match"] += bool(response_integrity.get("matches_trace_metadata"))
        capture = record.get("capture") or {}
        capture_integrity["request_parse_errors"] += bool(capture.get("request_parse_error"))
        capture_integrity["response_parse_errors"] += bool(capture.get("response_parse_error"))
        capture_integrity["incomplete_responses"] += capture.get("response_complete") is False
        request = record.get("request") or {}
        response = record.get("response") or {}
        target = str(request.get("target", ""))
        path = urlsplit(target).path.lower()
        if not any(path.endswith(suffix) for suffix in ("/messages", "/chat/completions", "/responses")):
            continue
        endpoint_calls += 1
        status = str(response.get("status", "?"))
        status_counts[status] += 1
        req_body = request.get("body") or {}
        req_events = request_events(req_body) if isinstance(req_body, dict) else []
        if status.startswith("2") and isinstance(response.get("body"), dict):
            if isinstance(req_body, dict):
                if req_body.get("system") is not None:
                    system_value = system_text(req_body.get("system"))
                    system_key = digest(system_value)
                    api_system_variants.setdefault(system_key, {"chars": len(system_value), "calls": 0})
                    api_system_variants[system_key]["calls"] += 1
                tools_value = normalized_tools(req_body.get("tools"))
                if tools_value is not None:
                    tools_key = digest(tools_value)
                    api_tool_variants.setdefault(tools_key, {"count": len(tools_value),
                                                             "names": [item.get("name") for item in tools_value],
                                                             "calls": 0})
                    api_tool_variants[tools_key]["calls"] += 1
            for event in req_events:
                if event.get("kind") == "tool_result" and event.get("id"):
                    variants = request_tool_results[event["id"]]
                    variant = {"event": event, "sequence": record.get("sequence"),
                               "status": status}
                    if not any(existing["event"] == event for existing in variants):
                        if variants:
                            request_tool_result_conflicts[event["id"]] += 1
                        variants.append(variant)
            response_id, events = response_events(response["body"])
            if response_id:
                successful_response_ids += 1
            else:
                unmatched_response_records.append({"sequence": record.get("sequence"),
                                                   "reason": "successful_generation_response_without_id",
                                                   "event_count": len(events),
                                                   "event_kinds": [event.get("kind") for event in events]})
                continue
            source = assistant_owner.get(response_id)
            if source is None or source not in row_info:
                unmatched_response_records.append({"sequence": record.get("sequence"), "id": response_id,
                                               "reason": "response_id_absent_from_training_manifest",
                                               "event_count": len(events),
                                               "event_kinds": [event.get("kind") for event in events]})
                continue
            info = row_info[source]
            target_events = events
            actual_events = train_events(info["row"], info["descriptors"], message_id=response_id)
            mismatch = first_difference(target_events, actual_events)
            response_checks.append({"id": response_id, "sequence": record.get("sequence"),
                                    "source_transcript": source, "target_event_count": len(target_events),
                                    "training_event_count": len(actual_events),
                                    "exact": mismatch is None, "first_difference": mismatch})
            for event in target_events:
                if event.get("kind") != "tool_call" or not event.get("id"):
                    continue
                actual = train_tool_events.get(event["id"])
                if actual is None:
                    tool_call_checks.append({"id": event["id"], "exact": False, "reason": "missing_from_training"})
                else:
                    expected = {"name": event.get("name"), "arguments": event.get("arguments", {})}
                    observed = {"name": actual.get("name"), "arguments": actual.get("arguments", {})}
                    tool_call_checks.append({"id": event["id"], "exact": expected == observed,
                                             "expected_sha256": digest(expected),
                                             "training_sha256": digest(observed),
                                             "fields": [key for key in expected if expected[key] != observed[key]]})

            start = min((int(d.get("message_index", 10**12)) for d in info["descriptors"]
                         if d.get("message_id") == response_id), default=None)
            if start is not None:
                expected_history = req_events
                observed_history = train_events(info["row"], info["descriptors"], before_index=start)
                diff = first_difference(expected_history, observed_history)
                event_coverage = ordered_coverage(expected_history, observed_history)
                context_checks.append({"id": response_id, "sequence": record.get("sequence"),
                                       "request_event_count": len(expected_history),
                                       "training_prefix_event_count": len(observed_history),
                                       **event_coverage,
                                   "coalesced_content": coalesced_coverage(expected_history, observed_history),
                                       "request_event_kinds": [event.get("kind") for event in expected_history[:8]],
                                       "training_prefix_event_kinds": [event.get("kind") for event in observed_history[:8]],
                                       "exact": diff is None, "first_difference": diff})

            system = req_body.get("system") if isinstance(req_body, dict) else None
            if system is not None and info["row"].get("system") is not None:
                req_system = system_text(system)
                row_system = str(info["row"].get("system"))
                matcher = difflib.SequenceMatcher(a=req_system, b=row_system)
                matching_blocks = matcher.get_matching_blocks()
                system_checks.append({"id": response_id, "exact": req_system == row_system,
                                      "request_sha256": digest(req_system), "training_sha256": digest(row_system),
                                      "request_chars": len(req_system), "training_chars": len(row_system),
                                      "similarity_ratio": round(matcher.ratio(), 6),
                                      "common_prefix_chars": matching_blocks[0].size if matching_blocks else 0,
                                      "api_system_block_count": len(system) if isinstance(system, list) else 1,
                                      "prompt_snapshot_count": info["meta"].get("prompt_snapshot_count", 0)})
            api_tools = normalized_tools(req_body.get("tools")) if isinstance(req_body, dict) else None
            row_tools = normalized_tools(info["row"].get("tools"))
            if api_tools is not None and row_tools is not None:
                api_by_name = {item.get("name"): item for item in api_tools}
                row_by_name = {item.get("name"): item for item in row_tools}
                common_names = set(api_by_name) & set(row_by_name)
                schema_mismatch_names = sorted(name for name in common_names
                                               if api_by_name[name] != row_by_name[name])
                schema_checks.append({"id": response_id, "exact": api_tools == row_tools,
                                      "request_tool_count": len(api_tools), "training_tool_count": len(row_tools),
                                      "request_sha256": digest(api_tools), "training_sha256": digest(row_tools),
                                      "request_names": [tool["name"] for tool in api_tools],
                                      "training_names": [tool["name"] for tool in row_tools],
                                      "missing_from_training": sorted(set(api_by_name) - set(row_by_name)),
                                      "extra_in_training": sorted(set(row_by_name) - set(api_by_name)),
                                      "schema_mismatch_names": schema_mismatch_names})

    tool_result_checks = []
    image_assets_checked = 0
    image_asset_hash_mismatches = 0
    missing_image_assets = []
    for tool_id, variants in request_tool_results.items():
        info = next((item for item in row_info.values()
                     if any(d.get("role") == "tool_response" and str(d.get("tool_use_id")) == tool_id
                            for d in item["descriptors"])), None)
        if info is None:
            tool_result_checks.append({"id": tool_id, "exact": False, "reason": "missing_from_training"})
            continue
        descriptor = next(d for d in info["descriptors"]
                          if d.get("role") == "tool_response" and str(d.get("tool_use_id")) == tool_id)
        index = int(descriptor.get("message_index", -1))
        value = json_arg(info["row"]["messages"][index].get("content", "{}")) if index >= 0 else {}
        result_content = value.get("content", "") if isinstance(value, dict) else ""
        actual = tool_result_event(value.get("tool_use_id"), result_content,
                                   value.get("is_error", False)) if isinstance(value, dict) else {}
        assets = verify_image_assets(result_content, args.train.parent)
        expected_images = actual.get("image_sha256", [])
        if expected_images or assets["paths"]:
            image_assets_checked += len(assets["paths"])
            if assets["missing"] or assets["sha256"] != expected_images:
                image_asset_hash_mismatches += 1
            missing_image_assets.extend(assets["missing"])
        matched_variants = sum(item["event"] == actual for item in variants)
        tool_assets_exact = not assets["missing"] and assets["sha256"] == expected_images
        tool_result_checks.append({"id": tool_id, "exact": matched_variants == len(variants) and tool_assets_exact,
                                   "content_exact": matched_variants == len(variants),
                                   "image_assets_exact": tool_assets_exact,
                                   "image_asset_paths": assets["paths"],
                                   "image_asset_sha256": assets["sha256"],
                                   "api_variant_count": len(variants),
                                   "api_variants_matching_training": matched_variants,
                                   "api_variants": [{"sequence": item["sequence"], "status": item["status"],
                                                     "content_sha256": item["event"].get("content_sha256"),
                                                     "content_size": item["event"].get("content_size")}
                                                    for item in variants],
                                   "training_sha256": digest(actual),
                                   "training_content_sha256": actual.get("content_sha256"),
                                   "training_content_size": actual.get("content_size"),
                                   "training_error": actual.get("is_error")})

    api_request_events = 0
    transcript_files = set()
    training_system_variants: Counter[str] = Counter()
    training_tool_variants: Counter[str] = Counter()
    for meta in written:
        source = Path(str(meta.get("source_transcript", "")))
        info = row_info.get(str(meta.get("source_transcript", "")))
        if info:
            if info["row"].get("system") is not None:
                training_system_variants[digest(str(info["row"]["system"]))] += 1
            tools_value = normalized_tools(info["row"].get("tools"))
            if tools_value is not None:
                training_tool_variants[digest(tools_value)] += 1
        if source in transcript_files or not source.is_file():
            continue
        transcript_files.add(source)
        for item in read_jsonl(source):
            if item.get("type") == "api-request":
                api_request_events += 1

    def counts(checks: list[dict[str, Any]]) -> dict[str, int]:
        return {"checked": len(checks), "exact": sum(bool(item.get("exact")) for item in checks),
                "mismatch": sum(not bool(item.get("exact")) for item in checks)}

    result = {
        "schema": "training-vs-api-audit-v1",
        "inputs": {"manifest": str(args.manifest), "train": str(args.train),
                   "reconstructed": str(args.reconstructed)},
        "coverage": {"training_rows": len(rows), "transcript_files": len(transcript_files),
                     "api_request_events_in_transcripts": api_request_events,
                     "api_generation_endpoint_requests": endpoint_calls,
                     "http_status_counts": dict(status_counts),
                     "wire_capture_integrity": dict(capture_integrity),
                     "successful_response_ids": successful_response_ids,
                     "responses_joined_to_training": len(response_checks),
                     "responses_missing_from_training": len(unmatched_response_records),
                     "tool_results_seen_in_api_requests": len(request_tool_results),
                     "tool_results_with_conflicting_api_contents": len(request_tool_result_conflicts),
                     "image_assets_checked": image_assets_checked,
                     "image_asset_hash_mismatches": image_asset_hash_mismatches,
                     "missing_image_assets": missing_image_assets},
        "prompt_and_schema_variants": {
            "api_system_prompt_variants": len(api_system_variants),
            "training_system_prompt_variants": len(training_system_variants),
            "api_system_character_lengths": sorted({item["chars"] for item in api_system_variants.values()}),
            "api_tool_schema_variants": len(api_tool_variants),
            "training_tool_schema_variants": len(training_tool_variants),
            "api_tool_counts": sorted({item["count"] for item in api_tool_variants.values()}),
            "training_tool_counts": sorted({len(normalized_tools(info["row"].get("tools")) or [])
                                             for info in row_info.values()}),
            "api_tool_sets": sorted(api_tool_variants.values(), key=lambda item: (item["count"], item["names"])),
        },
        "assistant_response_content": {**counts(response_checks), "records": response_checks},
        "request_history_prefix": {**counts(context_checks), "records": context_checks},
        "tool_call_arguments": {**counts(tool_call_checks), "records": tool_call_checks},
        "tool_result_content": {**counts(tool_result_checks), "records": tool_result_checks,
                                "conflicting_api_ids": sorted(request_tool_result_conflicts)},
        "system_prompt": {**counts(system_checks), "records": system_checks,
                          "not_comparable_count": successful_response_ids - len(system_checks)},
        "tool_schemas": {**counts(schema_checks), "records": schema_checks,
                         "not_comparable_count": successful_response_ids - len(schema_checks)},
        "unmatched_successful_responses": unmatched_response_records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {key: value for key, value in result.items() if key not in {
        "assistant_response_content", "request_history_prefix", "tool_call_arguments",
        "tool_result_content", "system_prompt", "tool_schemas", "unmatched_successful_responses",
        "prompt_and_schema_variants"}}
    summary["unmatched_successful_response_count"] = len(unmatched_response_records)
    summary["checks"] = {"assistant_response_content": counts(response_checks),
                         "request_history_prefix": counts(context_checks),
                         "tool_call_arguments": counts(tool_call_checks),
                         "tool_result_content": counts(tool_result_checks),
                         "system_prompt": counts(system_checks),
                         "tool_schemas": counts(schema_checks)}
    summary["prompt_and_schema_variants"] = {
        key: value for key, value in result["prompt_and_schema_variants"].items()
        if key != "api_tool_sets"
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
