#!/usr/bin/env python3
"""Audit a Claude transcript against a semantic API trace.

This command is deliberately separate from ``claude_jsonl_to_swift.py``.  The
trace can confirm coverage and ordering, but it is never used to construct a
training row.  Both inputs are streamed line by line and only compact IDs,
counts, and hashes are retained.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield line_number, value


def content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    value = message.get("content")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    return []


def transcript_summary(paths: list[Path]) -> dict[str, Any]:
    assistant_ids: dict[str, dict[str, Any]] = {}
    tool_ids: dict[str, dict[str, Any]] = {}
    api_requests: list[dict[str, Any]] = []
    shape_map: dict[str, dict[str, Any]] = {}
    record_counts = Counter()
    thinking = Counter()
    invalid = 0
    for path in paths:
        for line_number, record in iter_jsonl(path):
            record_counts[str(record.get("type", "?"))] += 1
            if record.get("type") == "assistant":
                message = record.get("message") or {}
                message_id = message.get("id")
                if message_id:
                    assistant_ids.setdefault(str(message_id), {
                        "file": str(path), "line": line_number,
                        "model": message.get("model"),
                    })
                for block in content_blocks(message):
                    kind = block.get("type")
                    if kind == "thinking":
                        thinking["blocks"] += 1
                        if block.get("thinking"):
                            thinking["plaintext_blocks"] += 1
                        else:
                            thinking["opaque_blocks"] += 1
                    if kind in {"tool_use", "server_tool_use"} and block.get("id"):
                        tool_ids.setdefault(str(block["id"]), {
                            "file": str(path), "line": line_number,
                            "name": block.get("name"),
                            "input_digest": digest(block.get("input", {})),
                        })
            elif record.get("type") == "api-request":
                api_requests.append({
                    "file": str(path), "line": line_number,
                    "params": record.get("params") or {},
                    "request_digest": record.get("requestDigest"),
                    "shape_hash": record.get("shapeHash"),
                })
            elif record.get("type") == "api-request-shape":
                shape_map[str(record.get("shapeHash"))] = record.get("shape") or {}
    return {
        "files": [str(path) for path in paths],
        "record_counts": dict(record_counts),
        "assistant_ids": assistant_ids,
        "tool_ids": tool_ids,
        "api_requests": api_requests,
        "shape_map": shape_map,
        "thinking": dict(thinking),
    }


def tool_calls(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    choices = response.get("choices") or []
    for choice in choices:
        message = choice.get("message") or {}
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if call.get("id"):
                arguments = function.get("arguments", "")
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except json.JSONDecodeError:
                    pass
                result[str(call["id"])] = {
                    "name": function.get("name"),
                    "input_digest": digest(arguments),
                }
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                result[str(block["id"])] = {
                    "name": block.get("name"),
                    "input_digest": digest(block.get("input", {})),
                }
    return result


def compare_tool_payloads(transcript_tools: dict[str, dict[str, Any]],
                          trace_tools: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    mismatches = []
    for tool_id in sorted(set(transcript_tools) & set(trace_tools)):
        left, right = transcript_tools[tool_id], trace_tools[tool_id]
        fields = [key for key in ("name", "input_digest")
                  if left.get(key) is not None and right.get(key) is not None
                  and left.get(key) != right.get(key)]
        if fields:
            mismatches.append({"id": tool_id, "fields": fields,
                               "transcript": {key: left.get(key) for key in fields},
                               "trace": {key: right.get(key) for key in fields}})
    return mismatches


def trace_summary(path: Path) -> dict[str, Any]:
    first = next(iter_jsonl(path), None)
    if first and int(first[1].get("schema_version", 0) or 0) >= 2:
        return wire_trace_summary(path)
    response_ids: dict[str, dict[str, Any]] = {}
    trace_tools: dict[str, dict[str, Any]] = {}
    model_requests = 0
    generation_records = 0
    control_records = 0
    error_records = 0
    request_param_shapes = Counter()
    response_finish = Counter()
    request_shape_hashes = Counter()
    invalid = 0
    for line_number, record in iter_jsonl(path):
        request = record.get("request_original") or record.get("request") or {}
        response = record.get("response") or {}
        # Claude's control/context probes can contain a one-message body but do
        # not have model-generation parameters. Count only generation-shaped
        # requests as model calls.
        if isinstance(request, dict) and request.get("messages") and any(
                key in request for key in ("max_tokens", "thinking", "output_config", "stream")):
            model_requests += 1
            params = {key: request.get(key) for key in
                      ("model", "max_tokens", "thinking", "output_config") if key in request}
            request_param_shapes[digest(params)] += 1
            if record.get("request_original") is not None:
                request_shape_hashes[str(request.get("model"))] += 1
        choices = response.get("choices") or []
        if not choices:
            control_records += 1
            if response.get("error"):
                error_records += 1
            continue
        generation_records += 1
        response_id = response.get("id")
        if response_id:
            response_ids[str(response_id)] = {
                "line": line_number, "n": record.get("n"),
                "finish_reason": choices[0].get("finish_reason"),
            }
            response_finish[str(choices[0].get("finish_reason"))] += 1
        for tool_id, call in tool_calls(response).items():
            trace_tools[tool_id] = {"line": line_number, "n": record.get("n"), **call}
    return {
        "format": "semantic-legacy",
        "trace": str(path),
        "response_ids": response_ids,
        "tool_ids": trace_tools,
        "model_requests": model_requests,
        "generation_records": generation_records,
        "control_records": control_records,
        "error_records": error_records,
        "request_param_shapes": dict(request_param_shapes),
        "request_shape_model_counts": dict(request_shape_hashes),
        "response_finish_reasons": dict(response_finish),
    }


def wire_trace_summary(path: Path) -> dict[str, Any]:
    """Audit schema-v2 captures without loading whole request bodies in memory.

    Request bodies can contain million-token prompts. Their sidecars are hashed
    incrementally; only response JSON (or one SSE event at a time) is parsed.
    """
    records = 0
    complete = 0
    errors = 0
    missing_files = []
    integrity_mismatches = []
    status_counts = Counter()
    methods = Counter()
    request_bytes = 0
    response_bytes = 0
    model_requests = 0
    response_ids: dict[str, dict[str, Any]] = {}
    trace_tools: dict[str, dict[str, Any]] = {}
    incomplete_streams = []
    protocols = Counter()
    protocol_parse_errors = []
    for line_number, record in iter_jsonl(path):
        if int(record.get("schema_version", 0) or 0) < 2:
            continue
        records += 1
        methods[str(record.get("method", "?"))] += 1
        status_counts[str(record.get("response_status"))] += 1
        if record.get("response_complete"):
            complete += 1
        if record.get("error"):
            errors += 1
        request_bytes += int(record.get("request_body_bytes", 0) or 0)
        response_bytes += int(record.get("response_body_bytes", 0) or 0)
        for field in ("request_body_file", "request_transfer_body_file", "response_body_file"):
            value = record.get(field)
            if not value or not Path(value).is_file():
                missing_files.append({"line": line_number, "field": field, "path": value})
                continue
            byte_field = field.replace("_file", "_bytes")
            hash_field = field.replace("_file", "_sha256")
            actual_bytes, actual_hash = sidecar_integrity(Path(value))
            expected_bytes = record.get(byte_field)
            expected_hash = record.get(hash_field)
            mismatches = {}
            if expected_bytes is not None and actual_bytes != int(expected_bytes):
                mismatches["bytes"] = {"expected": int(expected_bytes), "actual": actual_bytes}
            if expected_hash and actual_hash != expected_hash:
                mismatches["sha256"] = {"expected": expected_hash, "actual": actual_hash}
            if mismatches:
                integrity_mismatches.append({"line": line_number, "field": field,
                                             "path": value, "mismatches": mismatches})
        request_target = str(record.get("request_target", ""))
        protocol = detect_protocol(request_target)
        if protocol:
            protocols[protocol] += 1
            # Requests to these generation endpoints are model requests. Do not
            # JSON-decode their possibly multi-gigabyte prompt bodies just to
            # count them; the raw sidecar and its digest are audited above.
            model_requests += 1
            response_file = record.get("response_body_file")
            if not response_file or not Path(response_file).is_file():
                continue
            try:
                streaming = is_sse_response(record.get("response_headers", []))
                if streaming:
                    parsed = parse_protocol_sse(protocol, iter_sse_file(response_file))
                else:
                    response_data = read_response_json(response_file)
                    if not isinstance(response_data, dict):
                        protocol_parse_errors.append({"line": line_number, "n": record.get("n"),
                                                      "protocol": protocol,
                                                      "error": "response_body_not_json_object"})
                        continue
                    parsed = parse_protocol_json(protocol, response_data)
            except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
                protocol_parse_errors.append({"line": line_number, "n": record.get("n"),
                                              "protocol": protocol,
                                              "error": f"{type(exc).__name__}: {exc}"})
                continue
            response_ids.update({key: {"line": line_number, "n": record.get("n"),
                                       "protocol": protocol, **value}
                                 for key, value in parsed["response_ids"].items()})
            trace_tools.update({key: {"line": line_number, "n": record.get("n"),
                                      "protocol": protocol, **value}
                                for key, value in parsed["tool_ids"].items()})
            if streaming and record.get("response_status") and int(record["response_status"]) < 400 and not parsed["terminal"]:
                incomplete_streams.append({"line": line_number, "n": record.get("n"),
                                           "protocol": protocol, "reason": "missing_terminal_event"})
    # Schema-3 proxies journal every exchange before it finishes; a start with no trace
    # record is an exchange cut off by the proxy dying (its partial bodies are on disk).
    interrupted = []
    journal = path.with_name(path.name + ".journal.jsonl")
    if journal.is_file():
        finished = {(str(r.get("proxy_run_id") or ""), r.get("n")) for _, r in iter_jsonl(path)}
        for _, event in iter_jsonl(journal):
            if (event.get("event") == "exchange_start"
                    and (str(event.get("proxy_run_id") or ""), event.get("n")) not in finished):
                interrupted.append({"proxy_run_id": event.get("proxy_run_id"), "n": event.get("n"),
                                    "target": event.get("request_target")})
    return {
        "format": "wire-v2",
        "trace": str(path),
        "wire_records": records,
        "complete_records": complete,
        "error_records": errors,
        "interrupted_exchanges": interrupted,
        "missing_body_files": missing_files,
        "body_integrity_mismatches": integrity_mismatches,
        "status_counts": dict(status_counts),
        "methods": dict(methods),
        "request_body_bytes": request_bytes,
        "response_body_bytes": response_bytes,
        "protocols": dict(protocols),
        "model_requests": model_requests,
        "generation_records": len(response_ids),
        "response_ids": response_ids,
        "tool_ids": trace_tools,
        "incomplete_streams": incomplete_streams,
        "protocol_parse_errors": protocol_parse_errors,
        "semantic_id_note": "IDs and stream completion are parsed for audit only; raw trace bodies never populate training data.",
    }


def detect_protocol(target: str) -> str | None:
    path = urlsplit(target).path.lower()
    if path.endswith("/messages"):
        return "messages"
    if path.endswith("/responses"):
        return "responses"
    if path.endswith("/chat/completions"):
        return "chat_completions"
    return None


def sidecar_integrity(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            byte_count += len(chunk)
            hasher.update(chunk)
    return byte_count, hasher.hexdigest()


def _open_payload(path: str | None):
    if not path:
        raise OSError("missing body sidecar path")
    with open(path, "rb") as stream:
        is_gzip = stream.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if is_gzip else open(path, "rb")


def is_sse_response(headers: list) -> bool:
    for item in headers:
        if isinstance(item, (list, tuple)) and len(item) == 2 and str(item[0]).lower() == "content-type":
            return "text/event-stream" in str(item[1]).lower()
    return False


def iter_sse_file(file_path: str | None):
    """Yield response lines incrementally to keep large streams bounded."""
    with _open_payload(file_path) as stream:
        yield from stream


def read_response_json(file_path: str | None) -> dict[str, Any] | None:
    try:
        with _open_payload(file_path) as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError):
        return None


def iter_sse_events(lines: Iterable[bytes]):
    event_name = ""
    data_lines: list[str] = []
    for line in lines:
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not text:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = "", []
        elif text.startswith(":"):
            continue
        elif text.startswith("event:"):
            event_name = text[6:].strip()
        elif text.startswith("data:"):
            data_lines.append(text[5:].lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def parse_protocol_sse(protocol: str, lines: Iterable[bytes]) -> dict[str, Any]:
    result = {"response_ids": {}, "tool_ids": {}, "terminal": False}
    chat_tools: dict[int, dict[str, Any]] = {}
    messages_blocks: dict[int, dict[str, Any]] = {}
    for event_name, data in iter_sse_events(lines):
        if data == "[DONE]":
            result["terminal"] = True
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if protocol == "messages":
            typ = obj.get("type") or event_name
            message = obj.get("message") or {}
            if typ == "message_start":
                add_response_id(result, message)
            elif typ == "content_block_start":
                block = obj.get("content_block") or {}
                index = obj.get("index", 0)
                messages_blocks[index] = block
                if block.get("type") in {"tool_use", "server_tool_use"} and block.get("id"):
                    result["tool_ids"][str(block["id"])] = {
                        "name": block.get("name"), "input_digest": digest(block.get("input", {}))
                    }
            elif typ == "content_block_delta":
                index = obj.get("index", 0)
                block = messages_blocks.setdefault(index, {})
                delta = obj.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    block["partial_json"] = block.get("partial_json", "") + delta.get("partial_json", "")
            elif typ == "content_block_stop":
                block = messages_blocks.get(obj.get("index", 0), {})
                if block.get("id"):
                    try:
                        arguments = json.loads(block.get("partial_json", "{}"))
                    except json.JSONDecodeError:
                        arguments = block.get("partial_json", "")
                    result["tool_ids"][str(block["id"])] = {
                        "name": block.get("name"), "input_digest": digest(arguments)
                    }
            elif typ == "message_stop":
                result["terminal"] = True
        elif protocol == "chat_completions":
            add_response_id(result, obj)
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    index = int(call.get("index", 0))
                    slot = chat_tools.setdefault(index, {"id": None, "name": "", "arguments": ""})
                    slot["id"] = call.get("id") or slot["id"]
                    function = call.get("function") or {}
                    slot["name"] += function.get("name", "")
                    slot["arguments"] += function.get("arguments", "")
                if choice.get("finish_reason") is not None:
                    result["terminal"] = True
        elif protocol == "responses":
            typ = obj.get("type") or event_name
            response = obj.get("response") if isinstance(obj.get("response"), dict) else obj
            add_response_id(result, response)
            if typ in {"response.completed", "response.failed", "response.incomplete"}:
                result["terminal"] = True
            item = obj.get("item") or obj.get("output_item") or {}
            if item:
                add_responses_item(result, item)
            if typ == "response.output_item.done":
                add_responses_item(result, item)
            if response.get("output"):
                merge_protocol_result(result, parse_protocol_json("responses", response))
    for slot in chat_tools.values():
        if slot.get("id"):
            try:
                arguments = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except json.JSONDecodeError:
                arguments = slot["arguments"]
            result["tool_ids"][str(slot["id"])] = {
                "name": slot["name"], "input_digest": digest(arguments)
            }
    return result


def parse_protocol_json(protocol: str, response: dict[str, Any]) -> dict[str, Any]:
    result = {"response_ids": {}, "tool_ids": {}, "terminal": True}
    if protocol == "messages":
        add_response_id(result, response)
        for block in response.get("content") or []:
            if isinstance(block, dict) and block.get("type") in {"tool_use", "server_tool_use"} and block.get("id"):
                result["tool_ids"][str(block["id"])] = {
                    "name": block.get("name"), "input_digest": digest(block.get("input", {}))
                }
    elif protocol == "chat_completions":
        add_response_id(result, response)
        result["tool_ids"].update(tool_calls(response))
    elif protocol == "responses":
        add_response_id(result, response)
        for item in response.get("output") or []:
            add_responses_item(result, item)
    return result


def add_response_id(result: dict[str, Any], value: dict[str, Any]) -> None:
    response_id = value.get("id")
    if response_id and ("usage" in value or "model" in value or "status" in value):
        result["response_ids"][str(response_id)] = {"model": value.get("model")}


def add_responses_item(result: dict[str, Any], item: dict[str, Any]) -> None:
    if item.get("type") not in {"function_call", "tool_search_call"}:
        return
    call_id = item.get("call_id") or item.get("id")
    if not call_id:
        return
    arguments = item.get("arguments", item.get("input", "{}"))
    try:
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        pass
    result["tool_ids"][str(call_id)] = {
        "name": item.get("name") or item.get("type"), "input_digest": digest(arguments)
    }


def merge_protocol_result(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["response_ids"].update(source.get("response_ids", {}))
    target["tool_ids"].update(source.get("tool_ids", {}))


def audit(transcript_paths: list[Path], trace_path: Path) -> dict[str, Any]:
    transcript = transcript_summary(transcript_paths)
    trace = trace_summary(trace_path)
    if trace.get("format") == "wire-v2":
        transcript_assistant = set(transcript["assistant_ids"])
        trace_responses = set(trace["response_ids"])
        messages_responses = {
            response_id for response_id, value in trace["response_ids"].items()
            if value.get("protocol") == "messages"
        }
        transcript_tools = set(transcript["tool_ids"])
        trace_tools = set(trace["tool_ids"])
        missing_assistant = sorted(messages_responses - transcript_assistant,
                                   key=lambda value: trace["response_ids"][value].get("n", 0))
        missing_tools = sorted(trace_tools - transcript_tools,
                               key=lambda value: trace["tool_ids"][value].get("n", 0))
        extra_assistant = sorted(transcript_assistant - messages_responses)
        extra_tools = sorted(transcript_tools - trace_tools)
        tool_mismatches = compare_tool_payloads(transcript["tool_ids"], trace["tool_ids"])
        transcript_requests = len(transcript["api_requests"])
        # Claude Code does not consistently emit transcript API-request rows.
        # A zero count therefore means "unavailable", rather than a mismatch.
        request_delta = (trace["model_requests"] - transcript_requests
                         if transcript_requests else None)
        messages_only = set(trace["protocols"]) <= {"messages"}
        coverage = {
            "wire_records": trace["wire_records"],
            "complete_records": trace["complete_records"],
            "error_records": trace["error_records"],
            "missing_body_file_count": len(trace["missing_body_files"]),
            "body_integrity_mismatch_count": len(trace["body_integrity_mismatches"]),
            "protocol_parse_error_count": len(trace["protocol_parse_errors"]),
            "trace_generation_requests": trace["model_requests"],
            "transcript_api_request_events": transcript_requests,
            "api_request_event_count_delta": request_delta,
            "trace_response_ids": len(trace_responses),
            "transcript_assistant_message_ids": len(transcript_assistant),
            "assistant_id_intersection": len(transcript_assistant & trace_responses),
            "assistant_id_comparison": "messages_response_ids" if messages_only else "protocol_mixed",
            "trace_responses_missing_from_transcript": [
                {"id": key, **trace["response_ids"][key]} for key in missing_assistant
            ],
            "transcript_assistant_ids_missing_from_trace": extra_assistant,
            "trace_tool_ids": len(trace_tools),
            "transcript_tool_ids": len(transcript_tools),
            "tool_id_intersection": len(trace_tools & transcript_tools),
            "trace_tool_calls_missing_from_transcript": [
                {"id": key, **trace["tool_ids"][key]} for key in missing_tools
            ],
            "matching_tool_payload_mismatches": tool_mismatches,
            "transcript_tool_ids_missing_from_trace": extra_tools,
            "incomplete_streams": trace["incomplete_streams"],
            "protocol_parse_errors": trace["protocol_parse_errors"],
            "semantic_alignment": "protocol_parsed",
        }
        has_capture_gaps = (
            trace["wire_records"] == 0
            or trace["complete_records"] != trace["wire_records"]
            or trace["error_records"] > 0
            or bool(trace["missing_body_files"])
            or bool(trace["body_integrity_mismatches"])
            or bool(trace["protocol_parse_errors"])
            or bool(trace["incomplete_streams"])
            or bool(missing_assistant)
            or (messages_only and bool(extra_assistant))
            or bool(missing_tools)
            or bool(tool_mismatches)
            or (request_delta is not None and request_delta != 0)
            or trace["model_requests"] != len(trace_responses)
        )
        result = {
            "transcript": {
                key: value for key, value in transcript.items()
                if key not in {"assistant_ids", "tool_ids", "api_requests", "shape_map"}
            },
            "trace": trace,
            "coverage": coverage,
            "status": "incomplete" if has_capture_gaps else "pass",
        }
        return result
    transcript_assistant = set(transcript["assistant_ids"])
    trace_responses = set(trace["response_ids"])
    transcript_tools = set(transcript["tool_ids"])
    trace_tools = set(trace["tool_ids"])
    missing_assistant = sorted(trace_responses - transcript_assistant,
                               key=lambda value: trace["response_ids"][value].get("n", 0))
    extra_assistant = sorted(transcript_assistant - trace_responses)
    missing_tools = sorted(trace_tools - transcript_tools,
                           key=lambda value: trace["tool_ids"][value].get("n", 0))
    extra_tools = sorted(transcript_tools - trace_tools)
    tool_mismatches = compare_tool_payloads(transcript["tool_ids"], trace["tool_ids"])
    result = {
        "transcript": {
            key: value for key, value in transcript.items()
            if key not in {"assistant_ids", "tool_ids", "api_requests", "shape_map"}
        },
        "trace": trace,
        "coverage": {
            "trace_generation_records": trace["generation_records"],
            "transcript_assistant_message_ids": len(transcript_assistant),
            "assistant_id_intersection": len(transcript_assistant & trace_responses),
            "trace_responses_missing_from_transcript": [
                {"id": key, **trace["response_ids"][key]} for key in missing_assistant
            ],
            "transcript_assistant_ids_missing_from_trace": extra_assistant,
            "trace_tool_ids": len(trace_tools),
            "transcript_tool_ids": len(transcript_tools),
            "tool_id_intersection": len(trace_tools & transcript_tools),
            "trace_tool_calls_missing_from_transcript": [
                {"id": key, **trace["tool_ids"][key]} for key in missing_tools
            ],
            "matching_tool_payload_mismatches": tool_mismatches,
            "transcript_tool_ids_missing_from_trace": extra_tools,
            "transcript_api_request_events": len(transcript["api_requests"]),
            "trace_requests_with_messages": trace["model_requests"],
            "api_request_event_count_delta": trace["model_requests"] - len(transcript["api_requests"]),
        },
    }
    coverage = result["coverage"]
    result["status"] = "pass" if not any([
        coverage["trace_responses_missing_from_transcript"],
        coverage["trace_tool_calls_missing_from_transcript"],
        coverage["matching_tool_payload_mismatches"],
        coverage["api_request_event_count_delta"],
    ]) else "incomplete"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", action="append", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.transcript, args.trace)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "coverage": result["coverage"]}, ensure_ascii=False))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
