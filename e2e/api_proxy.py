#!/usr/bin/env python3
"""Transparent HTTP capture proxy for Claude Code, vLLM and compatible APIs.

The proxy deliberately has no knowledge of Claude Messages, OpenAI Chat, or
OpenAI Responses semantics. It forwards the request payload and response bytes
unchanged and records enough metadata plus raw sidecars to reconstruct the
exchange later. Protocol parsing belongs in a separate offline audit/parser.

Capture is write-ahead, so an exchange survives the proxy being killed
(``docker kill``, OOM, container teardown) at any point:

* ``<trace>.journal.jsonl`` gets a ``proxy_start`` event per process, an
  ``exchange_start`` event as soon as request headers are parsed, and a
  ``response_head`` event as soon as upstream headers arrive.
* Request and response bodies are streamed straight into their sidecar files
  and flushed after every chunk, so the bytes received so far are on disk.
* The trace file keeps exactly one completed record per finished exchange.
  An ``exchange_start`` without a trace record is an interrupted exchange;
  ``rebuild_wire_jsonl.py`` reports it with its partial bodies.

Every proxy process has its own ``proxy_run_id``, and its sidecars live in
``<wire_dir>/<proxy_run_id>/``. ``n`` counts exchanges within one process, so
restarting the proxy on the same trace file never overwrites earlier sidecars;
``(proxy_run_id, n)`` identifies an exchange.

Environment:
    UPSTREAM: upstream origin, default ``http://127.0.0.1:8001``
    TRACE_FILE: JSONL metadata destination, default ``./api-trace.jsonl``
    WIRE_DIR: directory for raw body sidecars, default ``<trace>.wire``
    JOURNAL_FILE: write-ahead event log, default ``<trace>.journal.jsonl``
    PROXY_RUN_ID: override the generated per-process id
    CAPTURE_FSYNC: ``1`` to fsync sidecars after every chunk (survives a host
        crash, not just a process kill; slower)

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
import signal
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


SCHEMA_VERSION = 3
UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:8001").rstrip("/")
TRACE_FILE = os.environ.get("TRACE_FILE", "./api-trace.jsonl")
WIRE_DIR = os.environ.get("WIRE_DIR", TRACE_FILE + ".wire")
JOURNAL_FILE = os.environ.get("JOURNAL_FILE")  # None: <TRACE_FILE>.journal.jsonl
CAPTURE_FSYNC = os.environ.get("CAPTURE_FSYNC", "") == "1"
PROXY_RUN_ID = os.environ.get("PROXY_RUN_ID") or "{}-{}-{}".format(
    time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), os.getpid(), uuid.uuid4().hex[:8])
PROXY_STARTED_AT = time.time()
SPOOL_LIMIT = 8 * 1024 * 1024

# These fields describe one hop and must not be copied to the next hop.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

_trace_lock = threading.Lock()
_sequence = 0
_inflight: dict[int, dict] = {}


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


class _Sidecar:
    """Append-only body file that hashes as it goes and flushes every write.

    Flushing hands the bytes to the kernel, which keeps them when the process
    is killed; CAPTURE_FSYNC additionally survives a host crash.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("wb")
        self._hash = hashlib.sha256()
        self.bytes = 0

    def write(self, data: bytes) -> None:
        if not data:
            return
        self._file.write(data)
        self._file.flush()
        if CAPTURE_FSYNC:
            os.fsync(self._file.fileno())
        self._hash.update(data)
        self.bytes += len(data)

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


def _next_sequence() -> int:
    global _sequence
    with _trace_lock:
        _sequence += 1
        return _sequence


def _append_jsonl(path: str, record: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _trace_lock:
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            if CAPTURE_FSYNC:
                os.fsync(stream.fileno())


def _append_record(path: str, record: dict) -> None:
    _append_jsonl(path, record)


def _journal(event: str, **fields) -> None:
    _append_jsonl(JOURNAL_FILE or TRACE_FILE + ".journal.jsonl", {"schema_version": SCHEMA_VERSION, "event": event,
                                 "proxy_run_id": PROXY_RUN_ID, "at": time.time(), **fields})


def _read_chunked(rfile, decoded=None, raw=None):
    """Copy a chunked body into ``decoded`` (payload) and ``raw`` (exact framing).

    With no sinks given, spooled temporary files are returned rewound.
    """
    spooled = decoded is None
    if spooled:
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
                    if spooled:
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


def _read_request_body(handler: BaseHTTPRequestHandler, decoded: _Sidecar,
                       transfer: _Sidecar | None) -> None:
    """Stream the client's request body into its sidecar(s) as it arrives."""
    if transfer is not None:
        _read_chunked(handler.rfile, decoded, transfer)
        return
    value = handler.headers.get("Content-Length")
    remaining = int(value) if value else 0
    while remaining:
        chunk = handler.rfile.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ConnectionError("client closed before Content-Length bytes arrived")
        decoded.write(chunk)
        remaining -= len(chunk)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[api-proxy] " + (fmt % args) + "\n")
        sys.stderr.flush()

    def _handle(self) -> None:
        sequence = _next_sequence()
        exchange_id = uuid.uuid4().hex
        started = time.time()
        trace_file = TRACE_FILE
        upstream_path = self.path
        # raw_items() makes the duplicate-header behavior explicit. Values are
        # still HTTP-parsed (continuation lines are unfolded), not TCP bytes.
        request_headers = list(self.headers.raw_items())
        chunked = "chunked" in self.headers.get("Transfer-Encoding", "").lower()
        wire_dir = Path(WIRE_DIR) / PROXY_RUN_ID
        stem = wire_dir / f"{sequence:08d}"
        request_body = _Sidecar(stem.with_name(stem.name + ".request.bin"))
        # A non-chunked body is byte-identical on the wire, so it shares one file.
        request_transfer = _Sidecar(stem.with_name(stem.name + ".request-transfer.bin")) if chunked else None
        response_body = _Sidecar(stem.with_name(stem.name + ".response.bin"))
        transfer_path = request_transfer.path if request_transfer else request_body.path

        status = reason = None
        upstream_status = None
        response_headers: list[tuple[str, str]] = []
        response_head_at = None
        error = None
        failure_phase = None
        response_complete = False
        client_disconnected = False
        synthesized = False
        forwarded_headers: list[tuple[str, str]] = []
        phase = "request_read"

        with _trace_lock:
            _inflight[sequence] = {"n": sequence, "id": exchange_id, "started_at": started,
                                   "method": self.command, "target": self.path}
        _journal("exchange_start", n=sequence, id=exchange_id, started_at=started,
                 method=self.command, request_line=self.requestline, request_target=self.path,
                 http_version=self.request_version, upstream_origin=UPSTREAM,
                 request_headers=_header_pairs(request_headers),
                 request_body_file=str(request_body.path),
                 request_transfer_body_file=str(transfer_path),
                 response_body_file=str(response_body.path))

        connection = None
        try:
            upstream = urlsplit(UPSTREAM)
            if upstream.scheme not in {"http", "https"} or not upstream.hostname:
                raise ValueError(f"invalid UPSTREAM: {UPSTREAM}")
            _read_request_body(self, request_body, request_transfer)
            request_body.close()
            forwarded_headers = _forward_headers(request_headers, request_body.bytes, upstream.netloc)
            phase = "upstream_send"
            connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
            connection = connection_cls(upstream.hostname, upstream.port, timeout=600)
            connection.putrequest(self.command, upstream_path,
                                  skip_host=True, skip_accept_encoding=True)
            for name, value in forwarded_headers:
                connection.putheader(name, value)
            with request_body.path.open("rb") as body_source:
                connection.endheaders(body_source if request_body.bytes else None)
            phase = "upstream_head"
            response = connection.getresponse()
            status, reason = response.status, response.reason
            upstream_status = status
            response_headers = list(response.getheaders())
            response_head_at = time.time()
            _journal("response_head", n=sequence, id=exchange_id, status=status, reason=reason,
                     response_headers=_header_pairs(response_headers))
            has_content_length = any(name.lower() == "content-length"
                                     for name, _value in response_headers)
            upstream_closes = any(
                name.lower() == "connection" and "close" in value.lower()
                for name, value in response_headers
            )
            phase = "client_write"
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
            self.end_headers()
            read_chunk = getattr(response, "read1", response.read)
            while True:
                phase = "response_stream"
                chunk = read_chunk(64 * 1024)
                if not chunk:
                    break
                # Persist before forwarding, so a kill never loses bytes the
                # client already received.
                response_body.write(chunk)
                phase = "client_write"
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    # The client is gone. Stop reading so the upstream sees the
                    # disconnect too (vLLM aborts the generation), as it would
                    # without a proxy in between.
                    client_disconnected = True
                    failure_phase = "client_write"
                    break
            # read1() ends quietly when the upstream closes early on a
            # Content-Length body (no IncompleteRead), so check what is left.
            missing = getattr(response, "length", None)
            if not client_disconnected and missing:
                phase = "response_stream"
                raise http.client.IncompleteRead(b"", missing)
            response_complete = not client_disconnected
        except Exception as exc:  # log the failed exchange as well
            error = f"{type(exc).__name__}: {exc}"
            failure_phase = phase
            if phase in {"client_write", "request_read"} and isinstance(exc, OSError):
                client_disconnected = True
            if status is None and not client_disconnected:
                # No upstream response head: answer the client ourselves, and
                # say so, so it is never mistaken for an upstream 502.
                try:
                    status, reason = 502, "Bad Gateway"
                    error_body = b"upstream proxy error\n"
                    response_body.write(error_body)
                    response_headers = [
                        ("Content-Type", "text/plain; charset=utf-8"),
                        ("Content-Length", str(len(error_body))),
                        ("Connection", "close"),
                    ]
                    synthesized = True
                    self.close_connection = True
                    self.send_response(status, reason)
                    for name, value in response_headers:
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(error_body)
                    self.wfile.flush()
                    response_complete = True
                except OSError:
                    client_disconnected = True
            elif status is not None:
                # Upstream broke mid-body: close so the client sees the
                # truncation instead of waiting for bytes that never come.
                self.close_connection = True
        finally:
            if connection is not None:
                connection.close()
            for sidecar in (request_body, request_transfer, response_body):
                if sidecar is not None:
                    sidecar.close()

        finished = time.time()
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "exchange",
            "proxy_run_id": PROXY_RUN_ID,
            "n": sequence,
            "id": exchange_id,
            "started_at": started,
            "response_head_at": response_head_at,
            "timestamp": finished,
            "duration_s": finished - started,
            "method": self.command,
            "request_line": self.requestline,
            "request_target": self.path,
            "upstream_origin": UPSTREAM,
            "upstream_target": upstream_path,
            "http_version": self.request_version,
            "request_headers": _header_pairs(request_headers),
            "request_body_file": str(request_body.path),
            "request_transfer_body_file": str(transfer_path),
            "request_body_bytes": request_body.bytes,
            "request_transfer_body_bytes": (request_transfer or request_body).bytes,
            "request_transfer_body_sha256": (request_transfer or request_body).sha256,
            "request_body_sha256": request_body.sha256,
            "forwarded_headers": _header_pairs(forwarded_headers),
            "response_status": status,
            "response_reason": reason,
            "upstream_status": upstream_status,
            "proxy_synthesized_response": synthesized,
            "response_headers": _header_pairs(response_headers),
            "response_body_file": str(response_body.path),
            "response_body_bytes": response_body.bytes,
            "response_body_sha256": response_body.sha256,
            "response_complete": response_complete,
            "client_disconnected": client_disconnected,
            "failure_phase": failure_phase,
            "error": error,
        }
        _append_record(trace_file, record)
        with _trace_lock:
            _inflight.pop(sequence, None)

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _stop(signum, _frame) -> None:
    """Record which exchanges were cut off, then exit. Their bytes are already on disk."""
    with _trace_lock:
        inflight = sorted(_inflight.values(), key=lambda item: item["n"])
    try:
        _journal("proxy_stop", signal=signum, inflight=inflight)
    finally:
        os._exit(128 + signum)


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 8002
    server = ThreadedHTTPServer(("0.0.0.0", port), ProxyHandler)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _journal("proxy_start", pid=os.getpid(), started_at=PROXY_STARTED_AT, port=port,
             upstream_origin=UPSTREAM, trace_file=TRACE_FILE,
             wire_dir=str(Path(WIRE_DIR) / PROXY_RUN_ID), fsync=CAPTURE_FSYNC)
    print(f"[api-proxy] listening on :{port}, upstream={UPSTREAM}, trace={TRACE_FILE}, "
          f"run={PROXY_RUN_ID}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
