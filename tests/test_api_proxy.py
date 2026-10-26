import io
import json
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import api_proxy


def test_chunked_request_decodes_payload_and_keeps_wire_framing():
    payload, wire = api_proxy._read_chunked(io.BytesIO(
        b"4;ext=x\r\ntest\r\n3\r\n123\r\n0\r\nX-Test: yes\r\n\r\n"
    ))
    assert payload.read() == b"test123"
    assert wire.read().endswith(b"X-Test: yes\r\n\r\n")
    payload.close()
    wire.close()


def test_forward_headers_preserve_duplicates_and_remove_hop_by_hop():
    result = api_proxy._forward_headers([
        ("Authorization", "Bearer secret"),
        ("X-Tag", "one"),
        ("X-Tag", "two"),
        ("Transfer-Encoding", "chunked"),
        ("Host", "proxy"),
    ], 7, "upstream:8001")
    assert result.count(("X-Tag", "one")) == 1
    assert result.count(("X-Tag", "two")) == 1
    assert ("Transfer-Encoding", "chunked") not in result
    assert ("Authorization", "Bearer secret") in result
    assert ("Host", "upstream:8001") in result
    assert ("Content-Length", "7") in result


def test_proxy_forwards_arbitrary_endpoint_and_records_raw_bodies(tmp_path, monkeypatch):
    seen = {}
    response_body = b"event: custom\ndata: \x00\xff\n\n"

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            seen["path"] = self.path
            seen["body"] = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, *_args):
            pass

    upstream_server = HTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream_server.serve_forever, daemon=True)
    upstream_thread.start()
    trace_file = tmp_path / "trace.jsonl"
    wire_dir = tmp_path / "wire"
    monkeypatch.setattr(api_proxy, "UPSTREAM", f"http://127.0.0.1:{upstream_server.server_port}")
    monkeypatch.setattr(api_proxy, "TRACE_FILE", str(trace_file))
    monkeypatch.setattr(api_proxy, "JOURNAL_FILE", None)
    monkeypatch.setattr(api_proxy, "WIRE_DIR", str(wire_dir))
    monkeypatch.setattr(api_proxy, "SPOOL_LIMIT", 8)
    monkeypatch.setattr(api_proxy, "PROXY_RUN_ID", "run-a")
    proxy_server = api_proxy.ThreadedHTTPServer(("127.0.0.1", 0), api_proxy.ProxyHandler)
    proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
    proxy_thread.start()
    request_body = b'{"messages":[{"role":"user","content":"x"}]}'
    try:
        client = HTTPConnection("127.0.0.1", proxy_server.server_port, timeout=5)
        client.request("POST", "/v1/anything", body=request_body,
                       headers={"Content-Type": "application/json",
                                "Authorization": "Bearer secret"})
        response = client.getresponse()
        assert response.status == 200
        assert response.read() == response_body
        client.close()
    finally:
        proxy_server.shutdown()
        proxy_server.server_close()
        upstream_server.shutdown()
        upstream_server.server_close()
    assert seen == {"path": "/v1/anything", "body": request_body}
    record = json.loads(trace_file.read_text(encoding="utf-8"))
    assert record["schema_version"] == 3
    assert record["proxy_run_id"] == "run-a"
    assert Path(record["response_body_file"]).parent == wire_dir / "run-a"
    assert record["method"] == "POST"
    assert record["request_target"] == "/v1/anything"
    assert record["request_body_sha256"] == api_proxy._sha256(request_body)
    assert record["response_body_sha256"] == api_proxy._sha256(response_body)
    assert record["response_complete"] is True
    assert ["Authorization", "Bearer secret"] in record["request_headers"]
    assert Path(record["request_body_file"]).read_bytes() == request_body
    assert Path(record["response_body_file"]).read_bytes() == response_body


def test_proxy_records_generated_bad_gateway_body(tmp_path, monkeypatch):
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setattr(api_proxy, "UPSTREAM", "http://127.0.0.1:1")
    monkeypatch.setattr(api_proxy, "TRACE_FILE", str(trace_file))
    monkeypatch.setattr(api_proxy, "JOURNAL_FILE", None)
    monkeypatch.setattr(api_proxy, "WIRE_DIR", str(tmp_path / "wire"))
    proxy_server = api_proxy.ThreadedHTTPServer(("127.0.0.1", 0), api_proxy.ProxyHandler)
    thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", proxy_server.server_port, timeout=5)
        client.request("GET", "/health")
        response = client.getresponse()
        assert response.status == 502
        assert response.read() == b"upstream proxy error\n"
        client.close()
    finally:
        proxy_server.shutdown()
        proxy_server.server_close()
    deadline = time.time() + 2
    while time.time() < deadline and not trace_file.exists():
        time.sleep(0.01)
    record = json.loads(trace_file.read_text(encoding="utf-8"))
    assert record["response_complete"] is True
    assert record["response_status"] == 502
    assert record["upstream_status"] is None
    assert record["proxy_synthesized_response"] is True
    assert record["failure_phase"] == "upstream_send"
    assert Path(record["response_body_file"]).read_bytes() == b"upstream proxy error\n"


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _proxy(tmp_path, monkeypatch, upstream_port, run_id):
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setattr(api_proxy, "UPSTREAM", f"http://127.0.0.1:{upstream_port}")
    monkeypatch.setattr(api_proxy, "TRACE_FILE", str(trace_file))
    monkeypatch.setattr(api_proxy, "WIRE_DIR", str(tmp_path / "wire"))
    monkeypatch.setattr(api_proxy, "JOURNAL_FILE", None)
    monkeypatch.setattr(api_proxy, "PROXY_RUN_ID", run_id)
    monkeypatch.setattr(api_proxy, "_sequence", 0)
    server = api_proxy.ThreadedHTTPServer(("127.0.0.1", 0), api_proxy.ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, trace_file


class _EchoUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_proxy_restart_on_same_trace_keeps_both_runs(tmp_path, monkeypatch):
    upstream = _serve(_EchoUpstream)
    try:
        for run_id, payload in (("run-1", b"first"), ("run-2", b"second")):
            proxy, trace_file = _proxy(tmp_path, monkeypatch, upstream.server_port, run_id)
            try:
                client = HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
                client.request("POST", "/v1/messages", body=payload)
                assert client.getresponse().read() == payload
                client.close()
            finally:
                proxy.shutdown()
                proxy.server_close()
    finally:
        upstream.shutdown()
        upstream.server_close()
    records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert [(r["proxy_run_id"], r["n"]) for r in records] == [("run-1", 1), ("run-2", 1)]
    assert Path(records[0]["response_body_file"]).read_bytes() == b"first"
    assert Path(records[1]["response_body_file"]).read_bytes() == b"second"
    journal = [json.loads(line) for line in
               (tmp_path / "trace.jsonl.journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in journal] == ["exchange_start", "response_head"] * 2


class _TruncatingUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b'{"partial":')
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *_args):
        pass


def test_upstream_cut_mid_body_is_recorded_and_reaches_client(tmp_path, monkeypatch):
    upstream = _serve(_TruncatingUpstream)
    proxy, trace_file = _proxy(tmp_path, monkeypatch, upstream.server_port, "run-t")
    try:
        client = HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
        client.request("POST", "/v1/messages", body=b"{}")
        response = client.getresponse()
        try:
            response.read()
            truncated_for_client = False
        except Exception:
            truncated_for_client = True
        client.close()
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
    assert truncated_for_client
    record = json.loads(trace_file.read_text(encoding="utf-8"))
    assert record["response_complete"] is False
    assert record["failure_phase"] == "response_stream"
    assert record["proxy_synthesized_response"] is False
    assert Path(record["response_body_file"]).read_bytes() == b'{"partial":'
