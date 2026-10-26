#!/usr/bin/env python3
"""Transparent HTTP capture proxy for Claude Code, vLLM and compatible APIs.

The proxy deliberately has no knowledge of Claude Messages, OpenAI Chat, or
OpenAI Responses semantics. It forwards the request payload and response bytes
unchanged and records enough metadata plus raw sidecars to reconstruct the
exchange later. Protocol parsing belongs in a separate offline audit/parser.
Request and response bodies are spooled in memory up to 8 MiB and then spill to
temporary disk, so long-context calls do not require holding the full exchange
in RAM.

Environment:
    UPSTREAM: upstream origin, default ``http://127.0.0.1:8001``
    TRACE_FILE: JSONL metadata destination, default ``./api-trace.jsonl``
    WIRE_DIR: directory for raw body sidecars, default ``<trace>.wire``

Usage:
    UPSTREAM=http://127.0.0.1:8001 TRACE_FILE=/out/trace.jsonl \
      python3 api_proxy.py 9999

The proxy is intended to run on the same HTTP hop as the client. TLS is not
terminated here; an HTTPS client must be configured to use an HTTP upstream or
the capture point must otherwise receive plaintext HTTP.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Iterable
from urllib.parse import urlsplit


UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:8001").rstrip("/")
TRACE_FILE = os.environ.get("TRACE_FILE", "./api-trace.jsonl")
WIRE_DIR = os.environ.get("WIRE_DIR", TRACE_FILE + ".wire")
SPOOL_LIMIT = 8 * 1024 * 1024

# These fields describe one hop and must not be copied to the next hop.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

_trace_lock = threading.Lock()
_sequence = 0


def _header_pairs(headers: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Keep duplicate headers and their original order."""
    return [[str(name), str(value)] for name, value in headers]


def _forward_headers(headers: Iterable[tuple[str, str]], body_length: int,
                     upstream_host: str) -> list[tuple[str, str]]:
    connection_tokens = {
        token.strip().lower()
        for name, value in headers if name.lower() == "connection"
        for token in value.split(",") if token.strip()
    }
    forwarded: list[tuple[str, str]] = []
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in connection_tokens or lower == "content-length":
            continue
        if lower == "host":
            continue
        forwarded.append((name, value))
    forwarded.append(("Host", upstream_host))
    forwarded.append(("Content-Length", str(body_length)))
    return forwarded


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_spool(wire_dir: Path, sequence: int, label: str, stream) -> tuple[str, int, str]:
    wire_dir.mkdir(parents=True, exist_ok=True)
    path = wire_dir / f"{sequence:08d}.{label}.bin"
    hasher = hashlib.sha256()
    byte_count = 0
    stream.seek(0)
    with path.open("wb") as destination:
        while chunk := stream.read(1024 * 1024):
            destination.write(chunk)
            hasher.update(chunk)
            byte_count += len(chunk)
    return str(path), byte_count, hasher.hexdigest()


def _next_sequence() -> int:
    global _sequence
    with _trace_lock:
        _sequence += 1
        return _sequence


def _append_record(path: str, record: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _trace_lock:
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()


def _read_chunked(rfile):
    """Return spooled decoded body and exact transfer framing from client."""
    decoded = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b")
    raw = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b")
    while True:
        size_line = rfile.readline()
        if not size_line:
            raise ConnectionError("client closed during chunked request")
        raw.write(size_line)
        try:
            size = int(size_line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise ValueError("invalid chunk size") from exc
        if size == 0:
            while True:
                trailer = rfile.readline()
                if not trailer:
                    raise ConnectionError("client closed during chunked trailer")
                raw.write(trailer)
                if trailer in (b"\r\n", b"\n"):
                    decoded.seek(0)
                    raw.seek(0)
                    return decoded, raw
        chunk = rfile.read(size)
        ending = rfile.read(2)
        if len(chunk) != size or ending != b"\r\n":
            raise ConnectionError("invalid chunk payload")
        raw.write(chunk)
        raw.write(ending)
        decoded.write(chunk)


def _read_request_body(handler: BaseHTTPRequestHandler):
    transfer_encoding = handler.headers.get("Transfer-Encoding", "").lower()
    if "chunked" in transfer_encoding:
        return _read_chunked(handler.rfile)
    value = handler.headers.get("Content-Length")
    decoded = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b")
    raw = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b")
    if not value:
        decoded.seek(0)
        raw.seek(0)
        return decoded, raw
    length = int(value)
    remaining = length
    while remaining:
        chunk = handler.rfile.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ConnectionError("client closed before Content-Length bytes arrived")
        decoded.write(chunk)
        raw.write(chunk)
        remaining -= len(chunk)
    decoded.seek(0)
    raw.seek(0)
    return decoded, raw


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[api-proxy] " + (fmt % args) + "\n")
        sys.stderr.flush()

    def _handle(self) -> None:
        sequence = _next_sequence()
        started = time.time()
        trace_file = TRACE_FILE
        upstream_path = self.path
        # raw_items() makes the duplicate-header behavior explicit. Values are
        # still HTTP-parsed (continuation lines are unfolded), not TCP bytes.
        request_headers = list(self.headers.raw_items())
        request_body = None
        request_transfer = None
        response_body = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b")
        status = None
        reason = None
        response_headers: list[tuple[str, str]] = []
        error = None
        response_complete = False
        client_disconnected = False
        forwarded_headers: list[tuple[str, str]] = []

        try:
            upstream = urlsplit(UPSTREAM)
            if upstream.scheme not in {"http", "https"} or not upstream.hostname:
                raise ValueError(f"invalid UPSTREAM: {UPSTREAM}")
            request_body, request_transfer = _read_request_body(self)
            netloc = upstream.netloc
            request_body.seek(0, os.SEEK_END)
            request_body_length = request_body.tell()
            request_body.seek(0)
            forwarded_headers = _forward_headers(request_headers, request_body_length, netloc)
            # http.client will provide the upstream Host unless one was sent by
            # the caller; keep caller headers untouched in the trace either way.
            connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
            connection = connection_cls(upstream.hostname, upstream.port, timeout=600)
            connection.putrequest(self.command, upstream_path,
                                  skip_host=True, skip_accept_encoding=True)
            for name, value in forwarded_headers:
                connection.putheader(name, value)
            connection.endheaders(request_body)
            response = connection.getresponse()
            status, reason = response.status, response.reason
            response_headers = list(response.getheaders())
            has_content_length = any(name.lower() == "content-length"
                                     for name, _value in response_headers)
            upstream_closes = any(
                name.lower() == "connection" and "close" in value.lower()
                for name, value in response_headers
            )
            self.send_response(status, reason)
            for name, value in response_headers:
                if name.lower() in HOP_BY_HOP:
                    continue
                self.send_header(name, value)
            # response.read() decodes upstream chunk framing. If the upstream
            # did not provide a length, delimit the decoded payload by closing
            # this hop instead of advertising an invalid chunked body.
            if not has_content_length and status not in {204, 304}:
                self.send_header("Connection", "close")
                self.close_connection = True
            elif upstream_closes:
                self.send_header("Connection", "close")
                self.close_connection = True
            # The body is captured and forwarded as read. For a streaming
            # response, this preserves each payload byte and its timing closely.
            self.end_headers()
            read_chunk = getattr(response, "read1", response.read)
            while True:
                chunk = read_chunk(64 * 1024)
                if not chunk:
                    break
                response_body.write(chunk)
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    client_disconnected = True
                    break
            response_complete = not client_disconnected
            connection.close()
        except Exception as exc:  # log the failed exchange as well
            error = f"{type(exc).__name__}: {exc}"
            try:
                if status is None:
                    status, reason = 502, "Bad Gateway"
                    error_body = b"upstream proxy error\n"
                    response_body.write(error_body)
                    response_headers = [
                        ("Content-Type", "text/plain; charset=utf-8"),
                        ("Content-Length", str(len(error_body))),
                        ("Connection", "close"),
                    ]
                    self.close_connection = True
                    self.send_response(status, reason)
                    for name, value in response_headers:
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(error_body)
                    self.wfile.flush()
                    response_complete = True
            except Exception:
                client_disconnected = True

        wire_default = WIRE_DIR if trace_file == TRACE_FILE else trace_file + ".wire"
        wire_dir = Path(os.environ.get("WIRE_DIR", wire_default))
        if request_body is None:
            request_body = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b")
        if request_transfer is None:
            request_transfer = tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b")
        request_file, request_body_bytes, request_body_sha256 = _store_spool(
            wire_dir, sequence, "request", request_body)
        request_transfer_file, request_transfer_body_bytes, request_transfer_sha256 = _store_spool(
            wire_dir, sequence, "request-transfer", request_transfer)
        response_file, response_body_bytes, response_body_sha256 = _store_spool(
            wire_dir, sequence, "response", response_body)
        duration = time.time() - started
        record = {
            "schema_version": 2,
            "n": sequence,
            "id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "duration_s": duration,
            "method": self.command,
            "request_line": self.requestline,
            "request_target": self.path,
            "upstream_origin": UPSTREAM,
            "upstream_target": upstream_path,
            "http_version": self.request_version,
            "request_headers": _header_pairs(request_headers),
            "request_body_file": request_file,
            "request_transfer_body_file": request_transfer_file,
            "request_body_bytes": request_body_bytes,
            "request_transfer_body_bytes": request_transfer_body_bytes,
            "request_transfer_body_sha256": request_transfer_sha256,
            "request_body_sha256": request_body_sha256,
            "forwarded_headers": _header_pairs(forwarded_headers),
            "response_status": status,
            "response_reason": reason,
            "response_headers": _header_pairs(response_headers),
            "response_body_file": response_file,
            "response_body_bytes": response_body_bytes,
            "response_body_sha256": response_body_sha256,
            "response_complete": response_complete,
            "client_disconnected": client_disconnected,
            "error": error,
        }
        _append_record(trace_file, record)
        request_body.close()
        request_transfer.close()
        response_body.close()

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 8002
    server = ThreadedHTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"[api-proxy] listening on :{port}, upstream={UPSTREAM}, trace={TRACE_FILE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
