# API capture (`e2e/api_proxy.py`)

The proxy sits between Claude Code (or any scaffold) and the model server and records every HTTP exchange,
successful or not, byte-exact. It is the ground truth used to audit the transcript-based training data
(`docs/transcript_to_calls.md`); it never feeds training rows itself.

Verified 2026-10-26 on `maic_a800_80_2` with Claude Code 2.1.310 and DeepSeek-V4.1-Flash on vLLM. The
experiments are in `tests/e2e_capture/`.

## Files written

For `TRACE_FILE=<dir>/<id>.jsonl`:

| File | Content |
|---|---|
| `<id>.jsonl` | one record per **finished** exchange (schema 3, see below) |
| `<id>.jsonl.journal.jsonl` | write-ahead events: `proxy_start`, `exchange_start`, `response_head`, `proxy_stop` |
| `<id>.jsonl.wire/<proxy_run_id>/<n>.request.bin` | request body as received (decoded from chunked if needed) |
| `.../<n>.request-transfer.bin` | exact chunked framing; only written for chunked requests |
| `.../<n>.response.bin` | response body bytes as received from upstream (still content-encoded) |

`rebuild_wire_jsonl.py --trace <id>.jsonl --output <id>.reconstructed.jsonl` reads the trace and the journal and
writes one readable exchange per line (request JSON, response JSON or SSE folded into the non-streaming shape).
Derived files (`*.reconstructed.jsonl`, `*.raw.jsonl`) can always be regenerated; do not archive them.

## Design decisions

**Write-ahead, so a killed proxy loses nothing.** The old proxy kept bodies in memory and wrote the record when
the exchange ended, so every exchange in flight when the container was killed vanished (exchange 13 of the
2026-10-26 DeepSeek run lost 2 successful calls this way). Now `exchange_start` is journaled as soon as request
headers arrive, `response_head` as soon as upstream headers arrive, and both bodies are streamed into their
sidecar files with a flush per chunk, *before* each chunk is forwarded to the client. A flush hands bytes to the
kernel, which keeps them when the process is killed; `CAPTURE_FSYNC=1` also survives a host crash, at a cost.
`rebuild_wire_jsonl.py` turns every `exchange_start` without a trace record into an exchange with
`capture.capture_status: "interrupted"` and its partial bodies.

Measured: SIGKILL mid-stream, the bytes on disk equal the bytes the client received (32,609 = 32,609). In a real
Claude Code run killed with `docker kill`, calls 2-10 were complete and call 11 (in flight, absent from the
transcript) was recovered as `interrupted` with its partial thinking + tool_use.

**One id per proxy process.** `n` counts exchanges within one process, so a restart on the same trace file used
to overwrite `00000001.*` sidecars and produce duplicate `n`. Each process now has `proxy_run_id`
(`<UTC time>-<pid>-<random>`), its sidecars live in `.wire/<proxy_run_id>/`, an exchange is identified by
`(proxy_run_id, n)`, and the rebuild orders by run start, then `n`.

**A response the proxy made itself is labelled.** When there is no upstream response head (connect refused,
upstream reset before headers), the proxy answers the client with a 502 and records
`proxy_synthesized_response: true`, `upstream_status: null`, `failure_phase` (`request_read`, `upstream_send`,
`upstream_head`, `response_stream`, `client_write`). A real upstream 502 has `upstream_status: 502`.

**Truncation is detected for both streaming and non-streaming bodies.** `http.client`'s `read1()` returns EOF
quietly when an upstream closes early on a `Content-Length` body, so the old proxy recorded such a body as
complete and left the client hanging until its timeout. The proxy now checks the remaining length, records
`response_complete: false` / `failure_phase: response_stream`, and closes the client connection so the client
sees the truncation too.

**The proxy does not change what the model sees.** It forwards headers except hop-by-hop ones, rewrites `Host`,
recomputes `Content-Length`, and turns an upstream chunked response into a close-delimited one. Body bytes are
never modified. If the client disconnects, the proxy stops reading and closes upstream, so vLLM aborts the
generation exactly as it would without a proxy (`client_disconnected: true`).

## Errors and truncation: streaming vs non-streaming

Measured against vLLM (DeepSeek-V4.1-Flash):

| Case | Non-streaming | Streaming |
|---|---|---|
| invalid params, prompt too long (errors before generation) | HTTP 400 + JSON error | identical: HTTP 400 + JSON error |
| `max_tokens` reached | complete message, `stop_reason: max_tokens` | complete stream, `stop_reason: max_tokens`, `message_stop` |
| error after generation started | cannot happen: the server answers 4xx/5xx instead | HTTP 200 already sent; stream ends with `event: error` |
| transport cut (bytes stop early) | body shorter than `Content-Length` | stream ends without `message_stop` |

`max_tokens` is a semantic stop, not a capture problem; both forms are complete and recorded as such. Whether
such responses are trained on is a data decision (`calls_to_swift.py` skips them unless `--keep-truncated`).

A stream that ends in `event: error` or without `message_stop` has no non-streaming equivalent: the non-streaming
API would have returned an error and no partial message. `rebuild_wire_jsonl.py` therefore marks it instead of
presenting the partial message as an answer: `response.body_format: "sse_partial"`,
`response.stream.termination: "error_event" | "eof_without_message_stop"`, the error objects in
`response.stream.errors`, plus `unclosed_blocks` and `unfinished_tool_json`. Complete streams have
`body_format: "sse_accumulated"`, `termination: "message_stop"`. Such partial responses are evidence, never
training targets.

`capture.capture_status` summarizes the wire outcome independently of the HTTP status:
`complete`, `interrupted`, `no_upstream_response`, `client_disconnected`, `upstream_body_truncated`.

## Traffic that does not go through the proxy

Every model call goes to `ANTHROPIC_BASE_URL` and therefore through the proxy: the main loop, subagents, the
small/fast model, compaction, `count_tokens` and the `HEAD /api/hello` probe (all seen in the traces). If the
proxy is down, these calls fail; they cannot bypass it.

What does bypass it is non-model traffic, per the official docs (`data-usage`, `network-config`): telemetry and
feature flags (`api.anthropic.com`, Datadog), error reports, the auto-updater (`downloads.claude.ai`), the
fast-mode availability check, and the WebFetch domain safety check (`api.anthropic.com`, sends the hostname only;
not covered by `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, disable with the `skipWebFetchPreflight` setting).
None of it carries model input or output. The WebFetch check matters in practice: without internet it fails and
WebFetch fails, which changes the trajectory. `run_batch.py` sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
`DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING` and `DISABLE_AUTOUPDATER` for Claude Code.

Capture is on for every run (`capture: true` in the run config, the default), not only in debug mode: the
transcript alone misses the compaction calls, calls in flight when a run is stopped, and HTTP errors.

## Why not OpenTelemetry instead of a proxy

Claude Code can emit telemetry (usage and diagnostic events about itself) over OpenTelemetry (OTel, an open
standard for exporting such data), enabled with `CLAUDE_CODE_ENABLE_TELEMETRY=1`. One option,
`OTEL_LOG_RAW_API_BODIES=file:<dir>`, writes the full request and response JSON of each call to disk with an
`index.jsonl`. Tested on 2.1.310: it does contain the compaction request and the raw compaction response including
`<analysis>`, but it **replaces every thinking block's text with `<REDACTED>`** and only writes **successful**
responses. A reasoning-model training set needs the thinking, and the audit needs the failures, so OTel is at most
a supplement. A `PostCompact` hook's `compact_summary` also equals the raw compaction response text including
`<analysis>` (no thinking, no usage); see `docs/transcript_to_calls.md` for whether to add it.

## Stopping a container without losing data

Claude Code appends each finished message to the transcript as it happens; there is no buffer to flush. Measured
by stopping a run mid-stream three ways (`tests/e2e_capture/cc_stop_exp.sh`):

| Method | Time to stop | Transcript | Proxy |
|---|---|---|---|
| `docker kill` | 0.4 s | every finished message present | (journal + partial bodies on disk) |
| `docker stop -t 60` | 60 s | same | same; bash as PID 1 ignores SIGTERM, so docker waits, then kills |
| `docker run --init -e TINI_KILL_PROCESS_GROUP=1` + `docker stop` | 0.4 s | same | writes `proxy_stop` listing in-flight exchanges |

In no case does Claude Code write the response that was streaming; only the proxy has it. `run_batch.py` uses the
third form: same speed as a kill, a clean exit for every process, and an explicit record of what was cut off.

## Tests

```bash
python -m pytest -q tests                      # unit tests, incl. restart, truncation, interrupted rebuild
bash tests/e2e_capture/new_exp.sh              # on a server with vLLM on :18005: SIGKILL/SIGTERM/restart/disconnect
bash tests/e2e_capture/cc_stop_exp.sh stop-init <scratch>   # real Claude Code, stop mid-stream
```
