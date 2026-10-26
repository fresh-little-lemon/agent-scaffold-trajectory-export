#!/usr/bin/env python3
"""Rebuild captured HTTP exchanges as human-readable request/response JSON.

Request JSON is parsed directly from its sidecar. Streaming responses are
accumulated into their non-streaming JSON API shape; image data already encoded
inside the API JSON remains unchanged. The capture trace and sidecars are read
only and remain the byte-exact evidence source.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, TextIO
from urllib.parse import urlsplit


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"skip malformed trace line {line_number}", file=sys.stderr)
                continue
            if isinstance(record, dict):
                yield record


def resolve_sidecar(value: str | None, output_mount: Path | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_file():
        return path
    prefix = "/app/outputs/"
    if output_mount and value.startswith(prefix):
        mapped = output_mount / value[len(prefix):]
        if mapped.is_file():
            return mapped
    return path


def header_values(headers: list, name: str) -> list[str]:
    return [str(value) for item in headers
            if isinstance(item, (list, tuple)) and len(item) == 2
            and str(item[0]).lower() == name.lower()
            for value in [item[1]]]


def content_encoding(headers: list) -> str:
    values = header_values(headers, "content-encoding")
    return ", ".join(values) if values else "identity"


def content_type(headers: list) -> str:
    values = header_values(headers, "content-type")
    return values[-1].split(";", 1)[0].strip().lower() if values else ""


def decoded_reader(path: Path, headers: list):
    encoding = content_encoding(headers).lower()
    if encoding in {"identity", ""}:
        return path.open("rb")
    encodings = [part.strip() for part in encoding.split(",") if part.strip()]
    if encodings == ["gzip"]:
        return gzip.open(path, "rb")
    raise ValueError(f"unsupported Content-Encoding for parsing: {encoding}")


def body_stats(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            byte_count += len(chunk)
            digest.update(chunk)
    return byte_count, digest.hexdigest()


def parse_json(path: Path, headers: list, max_bytes: int) -> tuple[Any, str | None]:
    try:
        with decoded_reader(path, headers) as source:
            payload = source.read(max_bytes + 1)
        if not payload:
            return None, None
        if len(payload) > max_bytes:
            return None, f"decoded body exceeds parse limit ({max_bytes} bytes)"
        return json.loads(payload.decode("utf-8")), None
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError,
            json.JSONDecodeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def body_json(path: Path | None, headers: list, max_bytes: int) -> tuple[Any, str | None]:
    if path is None or not path.is_file():
        return None, "missing body sidecar"
    return parse_json(path, headers, max_bytes)


def iter_sse_events(source) -> Iterable[dict[str, Any]]:
    event: dict[str, Any] = {"data_lines": [], "comments": [], "other_fields": []}
    has_fields = False

    def finish(current: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if "event" in current:
            result["event"] = current["event"]
        if "id" in current:
            result["id"] = current["id"]
        if "retry" in current:
            result["retry"] = current["retry"]
        if current["data_lines"]:
            data = "\n".join(current["data_lines"])
            result["data"] = data
            try:
                result["data_json"] = json.loads(data)
            except json.JSONDecodeError:
                pass
        if current["comments"]:
            result["comments"] = current["comments"]
        if current["other_fields"]:
            result["other_fields"] = current["other_fields"]
        return result

    for raw_line in source:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if has_fields:
                yield finish(event)
            event = {"data_lines": [], "comments": [], "other_fields": []}
            has_fields = False
            continue
        has_fields = True
        if line.startswith(":"):
            event["comments"].append(line[1:].lstrip(" "))
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            event["data_lines"].append(value)
        elif field == "event":
            event["event"] = value
        elif field == "id":
            event["id"] = value
        elif field == "retry":
            event["retry"] = value
        else:
            event["other_fields"].append([field, value])
    if has_fields:
        yield finish(event)


def detect_protocol(target: str) -> str | None:
    path = urlsplit(target).path.lower()
    if path.endswith("/messages"):
        return "messages"
    if path.endswith("/chat/completions"):
        return "chat_completions"
    if path.endswith("/responses"):
        return "responses"
    return None


def _json_data(event: dict[str, Any]) -> dict[str, Any] | None:
    value = event.get("data_json")
    return value if isinstance(value, dict) else None


def _finish_json(value: str) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def accumulate_messages(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    response: dict[str, Any] = {"type": "message", "role": "assistant", "content": []}
    blocks: dict[int, dict[str, Any]] = {}
    for event in events:
        obj = _json_data(event)
        if obj is None:
            continue
        typ = obj.get("type") or event.get("event")
        if typ == "message_start":
            response.update(obj.get("message") or {})
        elif typ == "content_block_start":
            index = int(obj.get("index", 0))
            blocks[index] = copy_block = dict(obj.get("content_block") or {})
            copy_block.setdefault("type", "text")
            if copy_block.get("type") == "text":
                copy_block.setdefault("text", "")
            elif copy_block.get("type") == "thinking":
                copy_block.setdefault("thinking", "")
            response["content"] = [blocks[key] for key in sorted(blocks)]
        elif typ == "content_block_delta":
            index = int(obj.get("index", 0))
            block = blocks.setdefault(index, {"type": "text", "text": ""})
            delta = obj.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta_type == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif delta_type == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif delta_type == "input_json_delta":
                block["_partial_json"] = block.get("_partial_json", "") + delta.get("partial_json", "")
            elif delta_type == "citations_delta":
                block.setdefault("citations", []).append(delta)
            response["content"] = [blocks[key] for key in sorted(blocks)]
        elif typ == "content_block_stop":
            index = int(obj.get("index", 0))
            block = blocks.get(index)
            if block and "_partial_json" in block:
                block["input"] = _finish_json(block.pop("_partial_json"))
        elif typ == "message_delta":
            delta = obj.get("delta") or {}
            for key in ("stop_reason", "stop_sequence"):
                if key in delta:
                    response[key] = delta[key]
            if isinstance(obj.get("usage"), dict):
                response.setdefault("usage", {}).update(obj["usage"])
        elif typ == "message_stop":
            response["_stream_complete"] = True
    for block in blocks.values():
        if "_partial_json" in block:
            block["input"] = _finish_json(block.pop("_partial_json"))
    response["content"] = [blocks[key] for key in sorted(blocks)]
    response.pop("_stream_complete", None)
    return response


def accumulate_chat_completions(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"object": "chat.completion", "choices": []}
    choice_map: dict[int, dict[str, Any]] = {}
    tools_by_choice: dict[int, dict[int, dict[str, Any]]] = {}
    for event in events:
        obj = _json_data(event)
        if obj is None:
            if event.get("data") == "[DONE]":
                result["_stream_complete"] = True
            continue
        for key in ("id", "created", "model", "system_fingerprint", "service_tier"):
            if key in obj:
                result[key] = obj[key]
        if isinstance(obj.get("usage"), dict):
            result["usage"] = obj["usage"]
        for choice_delta in obj.get("choices") or []:
            index = int(choice_delta.get("index", 0))
            choice = choice_map.setdefault(index, {
                "index": index, "message": {"role": "assistant", "content": ""},
                "finish_reason": None,
            })
            delta = choice_delta.get("delta") or {}
            message = choice["message"]
            if delta.get("role"):
                message["role"] = delta["role"]
            for key in ("content", "reasoning", "reasoning_content", "refusal"):
                if key in delta and isinstance(delta[key], str):
                    message[key] = message.get(key, "") + delta[key]
            for tool_delta in delta.get("tool_calls") or []:
                tool_index = int(tool_delta.get("index", 0))
                tool = tools_by_choice.setdefault(index, {}).setdefault(tool_index, {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}
                })
                if tool_delta.get("id"):
                    tool["id"] = tool_delta["id"]
                if tool_delta.get("type"):
                    tool["type"] = tool_delta["type"]
                function_delta = tool_delta.get("function") or {}
                if function_delta.get("name"):
                    tool["function"]["name"] += function_delta["name"]
                if function_delta.get("arguments"):
                    tool["function"]["arguments"] += function_delta["arguments"]
            if choice_delta.get("finish_reason") is not None:
                choice["finish_reason"] = choice_delta["finish_reason"]
    for index, choice in choice_map.items():
        tools = tools_by_choice.get(index, {})
        if tools:
            choice["message"]["tool_calls"] = []
            for tool_index in sorted(tools):
                tool = tools[tool_index]
                choice["message"]["tool_calls"].append(tool)
            if not choice["message"]["content"]:
                choice["message"].pop("content")
        result["choices"].append(choice)
    result["choices"].sort(key=lambda item: item["index"])
    result.pop("_stream_complete", None)
    return result


def accumulate_responses(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    response: dict[str, Any] = {"object": "response", "status": "in_progress", "output": []}
    items: dict[int, dict[str, Any]] = {}
    for event in events:
        obj = _json_data(event)
        if obj is None:
            continue
        typ = obj.get("type") or event.get("event")
        if typ in {"response.created", "response.in_progress", "response.completed",
                   "response.failed", "response.incomplete"}:
            incoming = obj.get("response")
            if isinstance(incoming, dict):
                response.update(incoming)
        if typ == "response.output_item.added":
            index = int(obj.get("output_index", len(items)))
            items[index] = dict(obj.get("item") or {})
        elif typ == "response.output_text.delta":
            index = int(obj.get("output_index", 0))
            item = items.setdefault(index, {"type": "message", "role": "assistant", "content": []})
            content_index = int(obj.get("content_index", 0))
            content = item.setdefault("content", [])
            while len(content) <= content_index:
                content.append({"type": "output_text", "text": "", "annotations": []})
            content[content_index]["text"] += obj.get("delta", "")
        elif typ == "response.reasoning_text.delta":
            index = int(obj.get("output_index", 0))
            item = items.setdefault(index, {"type": "reasoning", "summary": []})
            summary_index = int(obj.get("summary_index", 0))
            summary = item.setdefault("summary", [])
            while len(summary) <= summary_index:
                summary.append({"type": "summary_text", "text": ""})
            summary[summary_index]["text"] += obj.get("delta", "")
        elif typ in {"response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}:
            index = int(obj.get("output_index", 0))
            item = items.setdefault(index, {"type": "function_call", "arguments": ""})
            item["arguments"] = item.get("arguments", "") + obj.get("delta", "")
        elif typ in {"response.output_item.done", "response.output_item.added"}:
            item = obj.get("item")
            if isinstance(item, dict):
                items[int(obj.get("output_index", len(items)))] = dict(item)
        elif typ == "response.completed":
            response["status"] = "completed"
        elif typ == "response.failed":
            response["status"] = "failed"
        elif typ == "response.incomplete":
            response["status"] = "incomplete"
    if items:
        response["output"] = [items[index] for index in sorted(items)]
    return response


def parse_response(path: Path | None, headers: list, protocol: str | None,
                   max_bytes: int) -> tuple[Any, str | None, str]:
    if path is None or not path.is_file():
        return None, "missing response sidecar", "missing"
    if "text/event-stream" not in content_type(headers):
        value, error = parse_json(path, headers, max_bytes)
        if error is None:
            return value, None, "json"
        try:
            with decoded_reader(path, headers) as source:
                payload = source.read(max_bytes + 1)
            if len(payload) > max_bytes:
                return None, error, "unparsed"
            return payload.decode("utf-8", errors="replace"), None, "text"
        except (OSError, EOFError, gzip.BadGzipFile, ValueError) as exc:
            return None, f"{type(exc).__name__}: {exc}", "unparsed"
    accumulators = {
        "messages": accumulate_messages,
        "chat_completions": accumulate_chat_completions,
        "responses": accumulate_responses,
    }
    accumulator = accumulators.get(protocol or "")
    if accumulator is None:
        return None, f"cannot infer SSE protocol from endpoint: {protocol}", "sse"
    try:
        with decoded_reader(path, headers) as source:
            value = accumulator(iter_sse_events(source))
        return value, None, "sse_accumulated"
    except (OSError, EOFError, gzip.BadGzipFile, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}", "sse"


def integrity(path: Path | None, expected_bytes: int | None,
              expected_hash: str | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"available": False, "path": str(path) if path else None}
    actual_bytes, actual_hash = body_stats(path)
    matches = ((expected_bytes is None or actual_bytes == int(expected_bytes))
               and (not expected_hash or actual_hash == expected_hash))
    return {
        "available": True,
        "byte_length": actual_bytes,
        "sha256": actual_hash,
        "trace_byte_length": expected_bytes,
        "trace_sha256": expected_hash,
        "matches_trace_metadata": matches,
    }


def write_exchange(stream: TextIO, record: dict[str, Any], output_mount: Path | None,
                   max_json_bytes: int) -> None:
    request_headers = record.get("request_headers") or []
    response_headers = record.get("response_headers") or []
    request_path = resolve_sidecar(record.get("request_body_file"), output_mount)
    transfer_path = resolve_sidecar(record.get("request_transfer_body_file"), output_mount)
    response_path = resolve_sidecar(record.get("response_body_file"), output_mount)

    request_body, request_error = body_json(request_path, request_headers, max_json_bytes)
    protocol = detect_protocol(str(record.get("request_target", "")))
    response_body, response_error, response_format = parse_response(
        response_path, response_headers, protocol, max_json_bytes)
    if request_body is not None and not isinstance(request_body, (dict, list)):
        request_error = "request JSON root is not an object or array"

    rebuilt = {
        "schema": "api-exchange-json-v2",
        "sequence": record.get("n"),
        "exchange_id": record.get("id"),
        "request": {
            "method": record.get("method"),
            "url": str(record.get("upstream_origin", "")) + str(record.get("request_target", "")),
            "target": record.get("request_target"),
            "http_version": record.get("http_version"),
            "headers": request_headers,
            "body": request_body,
            "body_integrity": integrity(request_path, record.get("request_body_bytes"),
                                        record.get("request_body_sha256")),
        },
        "response": {
            "status": record.get("response_status"),
            "reason": record.get("response_reason"),
            "headers": response_headers,
            "body": response_body,
            "body_format": response_format,
            "body_integrity": integrity(response_path, record.get("response_body_bytes"),
                                         record.get("response_body_sha256")),
        },
        "capture": {
            "timestamp": record.get("timestamp"),
            "duration_s": record.get("duration_s"),
            "response_complete": record.get("response_complete"),
            "client_disconnected": record.get("client_disconnected"),
            "proxy_error": record.get("error"),
            "request_transfer_body_integrity": integrity(
                transfer_path, record.get("request_transfer_body_bytes"),
                record.get("request_transfer_body_sha256")),
            "request_parse_error": request_error,
            "response_parse_error": response_error,
        },
    }
    json.dump(rebuilt, stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")


def rebuild(trace_path: Path, output_path: Path, output_mount: Path | None,
            max_json_bytes: int) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Proxy records are appended after responses complete, so concurrent calls
    # may appear out of order. SQLite sorts only the small metadata records;
    # large request/response payloads remain streamed from sidecars.
    with tempfile.TemporaryDirectory(prefix="wire-jsonl-sort-") as temporary:
        database = sqlite3.connect(Path(temporary) / "records.sqlite3")
        database.execute("CREATE TABLE records (sequence INTEGER, ordinal INTEGER, value TEXT)")
        count = 0
        for count, record in enumerate(iter_jsonl(trace_path), 1):
            database.execute(
                "INSERT INTO records VALUES (?, ?, ?)",
                (int(record.get("n", 0) or 0), count,
                 json.dumps(record, ensure_ascii=False, separators=(",", ":"))),
            )
            if count % 1000 == 0:
                database.commit()
        database.commit()
        with output_path.open("w", encoding="utf-8", newline="\n") as output:
            cursor = database.execute("SELECT value FROM records ORDER BY sequence, ordinal")
            for (value,) in cursor:
                write_exchange(output, json.loads(value), output_mount, max_json_bytes)
                output.flush()
        database.close()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True,
                        help="proxy metadata JSONL")
    parser.add_argument("--output", type=Path, required=True,
                        help="reconstructed JSONL destination")
    parser.add_argument("--output-mount", type=Path,
                        help="host directory mounted as /app/outputs in capture records")
    parser.add_argument("--max-json-mib", type=int, default=256,
                        help="maximum decoded body size parsed into a JSON object (default: 256 MiB)")
    args = parser.parse_args()
    count = rebuild(args.trace, args.output, args.output_mount,
                    max_json_bytes=args.max_json_mib * 1024 * 1024)
    print(json.dumps({"records": count, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
