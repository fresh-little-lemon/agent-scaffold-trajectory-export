import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_transcript_trace import audit, compare_tool_payloads, parse_protocol_sse


def test_wire_v2_audit_checks_sidecar_completeness(tmp_path):
    body_dir = tmp_path / "wire"
    body_dir.mkdir()
    files = []
    for label, payload in (("request", b"{}"), ("request-transfer", b"{}"), ("response", b"{}")):
        path = body_dir / f"1.{label}.bin"
        path.write_bytes(payload)
        files.append(str(path))
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "schema_version": 2,
        "n": 1,
        "method": "POST",
        "response_status": 200,
        "response_complete": True,
        "request_body_file": files[0],
        "request_transfer_body_file": files[1],
        "response_body_file": files[2],
        "request_body_bytes": 2,
        "response_body_bytes": 2,
    }) + "\n", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
                          encoding="utf-8")
    result = audit([transcript], trace)
    assert result["status"] == "pass"
    assert result["coverage"]["semantic_alignment"] == "protocol_parsed"


def test_wire_v2_audit_reports_missing_sidecar(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "schema_version": 2,
        "n": 1,
        "method": "GET",
        "response_status": 502,
        "response_complete": False,
        "error": "upstream failed",
        "request_body_file": str(tmp_path / "missing-request"),
        "request_transfer_body_file": str(tmp_path / "missing-transfer"),
        "response_body_file": str(tmp_path / "missing-response"),
    }) + "\n", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    result = audit([transcript], trace)
    assert result["status"] == "incomplete"
    assert result["coverage"]["missing_body_file_count"] == 3


def test_wire_v2_audit_detects_sidecar_mutation(tmp_path):
    sidecars = {}
    for label, payload in (("request", b"{}"), ("request-transfer", b"{}"),
                           ("response", b'{"id":"resp_1","model":"x"}')):
        path = tmp_path / f"{label}.bin"
        path.write_bytes(payload)
        sidecars[label] = path
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "schema_version": 2,
        "n": 1,
        "method": "POST",
        "request_target": "/v1/chat/completions",
        "response_status": 200,
        "response_complete": True,
        "request_body_file": str(sidecars["request"]),
        "request_body_bytes": 2,
        "request_body_sha256": "incorrect",
        "request_transfer_body_file": str(sidecars["request-transfer"]),
        "request_transfer_body_bytes": 2,
        "request_transfer_body_sha256": "incorrect",
        "response_body_file": str(sidecars["response"]),
        "response_body_bytes": sidecars["response"].stat().st_size,
        "response_body_sha256": "incorrect",
        "response_headers": [["Content-Type", "application/json"]],
    }) + "\n", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    result = audit([transcript], trace)
    assert result["status"] == "incomplete"
    assert result["coverage"]["body_integrity_mismatch_count"] == 3


def test_messages_sse_parser_joins_tool_json_deltas():
    events = [
        b'event: message_start\n',
        b'data: {"type":"message_start","message":{"id":"msg_1","model":"deepseek-v4.1"}}\n',
        b'\n',
        b'event: content_block_start\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tool_1","name":"Read","input":{}}}\n',
        b'\n',
        b'event: content_block_delta\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\\"path\\\":\\\"a\\\"}"}}\n',
        b'\n',
        b'event: content_block_stop\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'\n',
        b'event: message_stop\n',
        b'data: {"type":"message_stop"}\n',
        b'\n',
    ]
    parsed = parse_protocol_sse("messages", iter(events))
    assert "msg_1" in parsed["response_ids"]
    assert parsed["terminal"] is True
    assert parsed["tool_ids"]["tool_1"]["name"] == "Read"


def test_responses_sse_parser_reads_completed_response_and_tool_call():
    import json
    body = {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "model": "deepseek-v4.1",
            "status": "completed",
            "output": [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                        "name": "Read", "arguments": "{\"path\":\"a\"}"}],
        },
    }
    event = ("data: " + json.dumps(body) + "\n\n").encode()
    parsed = parse_protocol_sse("responses", event.splitlines(keepends=True))
    assert parsed["terminal"] is True
    assert "resp_1" in parsed["response_ids"]
    assert parsed["tool_ids"]["call_1"]["name"] == "Read"


def test_tool_content_audit_checks_name_and_arguments_digest():
    mismatch = compare_tool_payloads(
        {"tool_1": {"name": "Read", "input_digest": "a"}},
        {"tool_1": {"name": "Write", "input_digest": "b"}},
    )
    assert mismatch == [{
        "id": "tool_1",
        "fields": ["name", "input_digest"],
        "transcript": {"name": "Read", "input_digest": "a"},
        "trace": {"name": "Write", "input_digest": "b"},
    }]
