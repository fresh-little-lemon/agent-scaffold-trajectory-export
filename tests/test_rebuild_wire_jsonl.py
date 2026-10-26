import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rebuild_wire_jsonl import (
    accumulate_chat_completions,
    accumulate_messages,
    accumulate_responses,
    iter_sse_events,
    parse_json,
    rebuild,
)


def test_sse_parser_preserves_event_fields_and_parses_json():
    events = list(iter_sse_events([
        b": ping\n",
        b"event: response.output_text.delta\n",
        b"id: ev-1\n",
        b"data: {\"delta\":\"hello\"}\n",
        b"\n",
        b"data: [DONE]\n",
        b"\n",
    ]))
    assert events == [
        {"event": "response.output_text.delta", "id": "ev-1",
         "data": '{"delta":"hello"}', "data_json": {"delta": "hello"},
         "comments": ["ping"]},
        {"data": "[DONE]"},
    ]


def test_empty_http_body_is_not_a_json_parse_error(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert parse_json(empty, [], 1024) == (None, None)


def test_rebuild_emits_readable_request_and_response_json(tmp_path):
    request = (b'{"model":"x","messages":[{"role":"user","content":[{"type":"image",'
               b'"source":{"type":"base64","media_type":"image/png","data":"aGVsbG8="}}]}]}')
    response = b'{"id":"msg_1","content":[{"type":"text","text":"ok"}]}'
    transfer = b""
    files = {}
    for key, payload in (("request", request), ("response", response),
                         ("transfer", transfer)):
        path = tmp_path / f"{key}.bin"
        path.write_bytes(payload)
        files[key] = path
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "schema_version": 2,
        "n": 1,
        "id": "exchange-1",
        "method": "POST",
        "request_line": "POST /v1/messages HTTP/1.1",
        "request_target": "/v1/messages",
        "http_version": "HTTP/1.1",
        "request_headers": [["Content-Type", "application/json"], ["X-Dup", "a"], ["X-Dup", "b"]],
        "request_body_file": str(files["request"]),
        "request_body_bytes": len(request),
        "request_body_sha256": hashlib.sha256(request).hexdigest(),
        "request_transfer_body_file": str(files["transfer"]),
        "request_transfer_body_bytes": len(transfer),
        "request_transfer_body_sha256": hashlib.sha256(transfer).hexdigest(),
        "response_status": 200,
        "response_reason": "OK",
        "response_headers": [["Content-Type", "application/json"]],
        "response_body_file": str(files["response"]),
        "response_body_bytes": len(response),
        "response_body_sha256": hashlib.sha256(response).hexdigest(),
    }) + "\n", encoding="utf-8")
    output = tmp_path / "rebuilt.jsonl"

    assert rebuild(trace, output, None, 1024) == 1

    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["request"]["headers"][-2:] == [["X-Dup", "a"], ["X-Dup", "b"]]
    assert record["request"]["body"]["messages"][0]["content"][0]["source"]["data"] == "aGVsbG8="
    assert record["request"]["body_integrity"]["matches_trace_metadata"] is True
    assert record["response"]["body"]["id"] == "msg_1"
    assert "raw_base64" not in output.read_text(encoding="utf-8")


def test_rebuild_accumulates_messages_sse_to_non_stream_json(tmp_path):
    request = b'{"model":"x","messages":[]}'
    response = (b"event: message_start\r\n"
                b'data: {"type":"message_start","message":{"id":"msg_1","model":"x","usage":{"input_tokens":9}}}\r\n\r\n'
                b"event: content_block_start\r\n"
                b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\r\n\r\n'
                b"event: content_block_delta\r\n"
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reason"}}\r\n\r\n'
                b"event: content_block_start\r\n"
                b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tool_1","name":"Read","input":{}}}\r\n\r\n'
                b"event: content_block_delta\r\n"
                b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\\"path\\\":\\\"a\\\"}"}}\r\n\r\n'
                b"event: content_block_stop\r\n"
                b'data: {"type":"content_block_stop","index":1}\r\n\r\n'
                b"event: message_delta\r\n"
                b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":12}}\r\n\r\n'
                b"event: message_stop\n"
                b'data: {"type":"message_stop"}\n\n')
    files = {}
    for key, payload in (("request", request), ("response", response), ("transfer", request)):
        path = tmp_path / f"{key}.bin"
        path.write_bytes(payload)
        files[key] = path
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "schema_version": 2,
        "n": 1,
        "method": "POST",
        "request_target": "/v1/messages",
        "request_headers": [["Content-Type", "application/json"]],
        "request_body_file": str(files["request"]),
        "request_body_bytes": len(request),
        "request_transfer_body_file": str(files["transfer"]),
        "request_transfer_body_bytes": len(request),
        "response_status": 200,
        "response_headers": [["Content-Type", "text/event-stream"]],
        "response_body_file": str(files["response"]),
        "response_body_bytes": len(response),
    }) + "\n", encoding="utf-8")
    output = tmp_path / "rebuilt-sse.jsonl"

    assert rebuild(trace, output, None, 1024) == 1

    record = json.loads(output.read_text(encoding="utf-8"))
    body = record["response"]["body"]
    assert body["id"] == "msg_1"
    assert body["content"] == [
        {"type": "thinking", "thinking": "reason"},
        {"type": "tool_use", "id": "tool_1", "name": "Read", "input": {"path": "a"}},
    ]
    assert body["stop_reason"] == "tool_use"
    assert body["usage"] == {"input_tokens": 9, "output_tokens": 12}
    assert record["response"]["body_format"] == "sse_accumulated"
    assert "sse_events" not in output.read_text(encoding="utf-8")


def test_chat_completion_sse_becomes_normal_response_json():
    payloads = [
        {"id": "chat_1", "object": "chat.completion.chunk", "model": "ds",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok "}, "finish_reason": None}]},
        {"id": "chat_1", "object": "chat.completion.chunk", "model": "ds",
         "choices": [{"index": 0, "delta": {"content": "then", "tool_calls": [
                 {"index": 0, "id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"path":"'}},
         ]}, "finish_reason": None}]},
        {"id": "chat_1", "object": "chat.completion.chunk", "model": "ds",
         "choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "function": {"arguments": 'a"}'}},
         ]}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}},
    ]
    events = [{"event": "", "data_json": value} for value in payloads]
    response = accumulate_chat_completions(events)
    choice = response["choices"][0]
    assert choice["message"]["content"] == "ok then"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "Read"
    assert choice["message"]["tool_calls"][0]["function"]["arguments"] == '{"path":"a"}'
    assert choice["finish_reason"] == "tool_calls"
    assert response["usage"]["total_tokens"] == 11


def test_responses_sse_uses_final_non_stream_response_object():
    final = {
        "id": "resp_1", "object": "response", "status": "completed", "model": "ds",
        "output": [{"id": "fc_1", "type": "function_call", "call_id": "call_1",
                    "name": "Read", "arguments": "{\"path\":\"a\"}"}],
    }
    response = accumulate_responses([{
        "event": "response.completed",
        "data_json": {"type": "response.completed", "response": final},
    }])
    assert response == final


def test_rebuild_orders_concurrent_proxy_records_by_request_sequence(tmp_path):
    trace = tmp_path / "trace.jsonl"
    rows = []
    for sequence in (2, 1):
        rows.append({"schema_version": 2, "n": sequence, "response_status": 200})
    trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    output = tmp_path / "rebuilt.jsonl"

    assert rebuild(trace, output, None, 1024) == 2

    assert [json.loads(line)["sequence"]
            for line in output.read_text(encoding="utf-8").splitlines()] == [1, 2]
