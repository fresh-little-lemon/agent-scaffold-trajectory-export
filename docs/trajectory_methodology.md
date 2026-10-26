# Claude Code trajectory export and training conversion

This note records the reproducible workflow used for the evaluation outputs under
`/home/tsq/workspace/browser_agent/evaluation`.

## What the two JSONL sources contain

The native Claude Code session file is an append-only event log. It contains
`user`, `assistant`, `attachment`, and control records, plus UUID parent links.
Assistant records can be repeated with the same API `message.id`: this is normal
for streaming/retry snapshots. A single assistant message may arrive as separate
`thinking`, `text`, and `tool_use` snapshots. Tool results are user records with a
`tool_result.tool_use_id`.

The current capture proxy writes schema-v2 records containing the HTTP method,
request target, all received headers, response headers/status, completion flags,
hashes, and paths to raw request/response body sidecars. It does not parse or
rewrite Claude Messages, OpenAI Chat/Responses, vLLM, or DeepSeek payloads, and
it forwards SSE bytes unchanged. A chunked request also keeps the original
client transfer framing. Header names, values, duplicates, and order are
preserved as parsed HTTP headers; this is capture at the HTTP proxy boundary,
not a TCP/TLS packet capture. Older evaluation traces may still use the legacy
semantic `{n, request, response}` format; those are explicitly marked legacy and
are not byte-complete. Some control/context records have no generation choices
and must not be treated as model turns.

`rebuild_wire_jsonl.py` reconstructs each captured exchange into a human-readable
JSONL record without changing the source trace or sidecars. The `request.body`
field is the parsed API request object, including system/user messages, tool
schemas and any image data that the API itself represents as Base64; it does not
Base64-encode the whole HTTP body. The `response.body` field is the ordinary
non-streaming response shape. For SSE, the
script accumulates Messages content blocks, Chat Completions deltas, or Responses
output items and tool arguments; it does not emit a raw event list or re-encode
the whole HTTP body. Headers, status, completion flags and sidecar length/hash
checks remain in the record for auditing. It orders concurrent proxy records by
their request sequence number `n`, since the trace itself is appended when each
response completes. Example:

```bash
python3 rebuild_wire_jsonl.py \
  --trace outputs/.../traces/13.jsonl \
  --output outputs/.../traces/13.reconstructed.jsonl \
  --output-mount outputs
```

The converter uses the native append order as the canonical event order. It does
not read the API trace and therefore cannot silently import fields that are absent
from the transcript. The separate audit command checks sidecar lengths and
hashes, then compares response/tool IDs and payloads after conversion where
protocol IDs allow it. It does not construct or repair training rows. An unmatched ID is a data-quality failure, not
something to repair by copying trace content into training data.

## Deduplication and reconstruction rules

1. Stream each JSONL file line by line. Never load a complete trace or transcript.
2. Exclude interleaved `isSidechain` records in a root session by default.
   Claude's standalone `subagents/` and `workflows/` JSONL files are different:
   each file is already one branch, so the converter keeps its sidechain records
   as a separate sample. Do not append parallel branch messages to the root
   sequence. The manifest records branch type, `agentId`, parent links, and the
   `Agent` call/result IDs used to link a spawned branch back to its root call.
   `--include-sidechains` remains available for diagnostics of a root log.
3. Group assistant snapshots by `message.id` and preserve first appearance. Merge
   only the same content slot across snapshots: text is updated when the later
   value extends the earlier prefix, and tool blocks are keyed by `tool_use.id`.
   Do not deduplicate unrelated text merely because it has a common prefix.
4. Keep each user text record in place. Convert each `tool_result` into a
   `tool_response` message and retain `tool_use_id`, error state, source UUID, and
   optional `toolUseResult` in its JSON content.
5. Do not discard `attachment` records as bookkeeping. `renderedRole` and
   `rendered` contain the role and text Claude Code renders into model context;
   emit each non-empty rendered item at that event's transcript position. This
   includes `<system-reminder>` task notifications such as `Monitor event: ...`
   when the record has `renderedRole: "system"`. `renderedInHumanTurn` and
   `renderedBesideToolResult` are context-dependent alternate renderings, not
   additional simultaneous messages: preserve their lengths and hashes in the
   manifest and the full source record in `control-events.jsonl`, while
   selecting `rendered` as the canonical variant. This is a provenance choice, not a
   claim that the alternate text was invisible: every variant remains available
   for review in the control-event record. Exact duplicate rendered
   items are removed only when their delivery ID, role, and content hash all
   match.
   A `prompt_snapshot` attachment can contain the CLI system prompt and tool
   schemas. Use the latest available snapshot from the transcript for the row's
   top-level `system` and `tools`, and mark their provenance as
   `prompt_snapshot attachment`. The manifest records each snapshot's line,
   lengths/counts, hashes, and rendering mode. Metadata-only attachments (for example, an empty
   `deferred_tools_record`) remain fully preserved in the control sidecar; do
   not invent a dialogue message from fields that have no rendered form.
6. Preserve `queue-operation` events as ordered control/audit events. They record
   enqueue/dequeue/remove/popAll lifecycle changes and may contain the same
   notification that is later rendered as an attachment. Keep every full,
   complete payload and parsed task/tool-use IDs, summary, event, status, and
   output-file reference in `queue_operation_events`; the control sidecar also
   retains the complete record. Link queue lines to attachment lines by
   delivery ID or exact payload hash, and link those to train-message indices.
   A notification with a linked rendered message is explicitly marked visible
   through that attachment. Keep queue operations themselves out of the
   conversational timeline because their enqueue/remove mechanics are not
   evidence of separate model turns. If a queue payload has no matching
   rendered message, mark visibility unresolved and retain it for review; do
   not call it invisible or silently discard it. Operations without a content
   payload are marked `lifecycle_only_no_payload`.
7. Remove an exact duplicate tool result only when its serialized payload is
   identical. Retain conflicting duplicates and record a warning.
8. Keep non-empty transcript `thinking` text as an assistant `<think>...</think>`
   message. An empty thinking string plus a non-empty signature is opaque and is
   omitted from trainable messages while its count is recorded in the manifest.
9. Extract base64 image blocks to content-addressed `assets/` files. This handles
   both `image.source.data` and Claude Code's `image.file.base64` variant. The
   training row uses `<image>` placeholders and an `images` list. Embedded
   document blocks (such as PDFs in tool results) are decoded to sidecar assets
   and represented by file references in the tool response. A failed/remote
   asset is recorded as a warning rather than downloaded implicitly.
10. Preserve source strings and payloads by default, including bearer/API key
   values. Optional `--redact-secrets` applies a basic pattern filter for a
   downstream sharing/export pass; it is not used for the raw capture or the
   canonical local reconstruction.

Auto-compaction has two relevant forms in native logs. A `compact_boundary`
system record stores compaction metadata such as trigger, token counts, and
preserved-message range; it is not itself a prompt/response turn. The following
user record marked `isCompactSummary` contains the summary that the model sees,
so retain it in sequence as user context and mark its source line in the manifest.
The manifest also preserves each boundary's `compactMetadata` object for audit.
Other
system summaries such as `away_summary` are counted as control records and are
not inserted as user messages. User records marked `isMeta` remain visible in
the transcript output; the manifest counts them so training filters can make an
explicit choice.

The output is always a reconstruction from model-rendered native events and
available prompt snapshots. Prompt snapshots can recover the CLI system prompt
and tool schemas when present; otherwise the original system prompt, tool
schemas, sampling parameters, and raw API envelope remain unknown. The audit
tool may compare a trace with this output, but never supplies missing training
content.

## ms-swift format

Each output row is JSONL of the form:

```json
{
  "system": "optional visible system prompt",
  "tools": "[JSON string containing function schemas]",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."},
    {"role": "tool_call", "content": "{\"name\":\"click\",\"arguments\":{}}"},
    {"role": "tool_response", "content": "{\"tool_use_id\":\"...\",\"content\":\"...\"}"}
  ]
}
```

`tools` and tool-call/tool-response `content` are JSON strings, as required by
ms-swift Agent support. Tool responses are context only and do not contribute to
loss; assistant text and tool calls are trainable targets according to the chosen
agent template. The converter writes `manifest.jsonl` and `control-events.jsonl`
beside `train.jsonl`. The training row contains model-rendered attachments
(including system notifications) and snapshot-derived system/tools when a trace
is absent. The manifest describes alternate render variants, deduplication,
per-operation visibility and reverse links to attachment/message indices. The
control-event sidecar keeps complete attachment and queue records for
audit and future reconstruction.

## Headless Claude Code runs

For a new run, mount the per-page workspace at `/home/node`, use an isolated
headless MCP/browser configuration, and run the CLI in print mode. A typical
command is:

```bash
claude -p "<task>" --dangerously-skip-permissions \
  --effort ultracode \
  --output-format stream-json --include-partial-messages \
  --include-hook-events
```

In Claude Code 2.1.310, `ultracode` is an effort setting that enables automatic
workflow orchestration and also sets effort to `xhigh`; it is not a separate
effort token value to translate to `xhigh`. The `ultracode` keyword in a prompt
only triggers this mode for human-entered interactive prompts, not text passed
to `-p`. For headless runs, start with `--effort ultracode` and allow both
`Workflow` and `Agent` tools. The session may then contain Workflow tool calls,
workflow task notifications/scripts, and independent agent transcript files.

Use the transparent proxy when a complete HTTP exchange is required. The native
session file remains the authoritative persisted trajectory for training. The
official CLI documents `stream-json`, partial messages, hooks, subagent text, and
budget/turn limits at [CLI usage](https://code.claude.com/docs/en/cli-usage). The
Agent SDK session documentation describes persisted prompts, tool calls, tool
results, and responses at [sessions](https://code.claude.com/docs/en/agent-sdk/sessions).
The evaluation image currently uses a pinned npm Claude Code package. The current
official setup page recommends the signed/native install channels and the project
README notes that npm installation is deprecated; pin the resolved `claude
--version` in a derived image for reproducibility: [setup](https://code.claude.com/docs/en/getting-started), [repository](https://github.com/anthropics/claude-code).

The staged Qwen3.8 launcher is `e2e/start_vllm.sh`. It follows the supplied
command's `/dev/shm/models` mount, TP=8, `qwen3_coder` tool parser, `qwen3`
reasoning parser, Rust frontend, and `--max-model-len 1000000` with
`VLLM_ALLOW_LONG_MAX_MODEL_LEN=1`; it omits `--hf-overrides` as requested. The
current nightly previously warned that `enable_auto_tool_choice` has no effect
in its Rust frontend, so successful startup alone will not prove tool use. The
end-to-end run must verify MCP tool calls in the native trajectory and trace.

The current `e2e/api_proxy.py` is transparent: it does not cap `max_tokens`,
inject credentials, normalize tool names, strip fields, or reconstruct SSE. It
records the exact payload bytes and headers visible at the proxy boundary. The
separate audit parser decodes Claude Messages, OpenAI Chat Completions, and
OpenAI Responses JSON/SSE bodies for ID-level auditing, joining streamed tool
argument fragments and checking terminal stream events. It hashes large request
sidecars incrementally and reads SSE response events incrementally. Claude Code
2.1.310 logs `output_config.effort: "xhigh"` for `--effort ultracode`. Its
auto-compact window has a 100k minimum; the staged run config requests 120k and
a 60% threshold, then checks native `compact_boundary` and `isCompactSummary`
records to see whether compaction actually fired.

DeepSeek data generation follows the same capture boundary. Its recipe keeps
separate converters for Anthropic Messages, OpenAI Chat Completions, and OpenAI
Responses, including separate streaming accumulators. The proxy stores the
selected endpoint, parsed headers, and raw body first; the audit parser then
chooses the matching DeepSeek/vLLM protocol and checks response IDs, tool calls,
and stream termination without changing the captured exchange. Reasoning text
remains available in the raw sidecars for a separately designed analysis; the
ID-level audit does not export it to training data.

## Hidden reasoning blocks

Claude provider responses may persist an empty `thinking` field with only a
signature; that signature is not decryptable transcript content and is kept only
as a manifest count. In the Qwen3.8 run, readable reasoning was persisted in
`thinking` blocks, so the converter retains it as `<think>` text. The cited
paper's extraction technique is not used to invent missing provider reasoning.
If the transcript has no readable reasoning, the training row cannot recover it
without an independent capture.

## Commands

```bash
python3 claude_jsonl_to_swift.py \
  /path/to/.claude/projects/-home-node/<session>.jsonl \
  --output-dir ./converted

python3 audit_transcript_trace.py \
  --transcript /path/to/session.jsonl \
  --trace /path/to/traces/<page>.jsonl \
  --output ./converted/trace-audit.json
```

The converter is streaming and tested locally and on A800 with synthetic cases
for snapshot deduplication, sidechain filtering, image extraction, directory
filtering, and trace ID alignment. A real Qwen page and a real Opus page were also
converted. The latter produced 216 messages (101 tool calls and 101 tool
responses), excluded 25 opaque thinking blocks, and matched every tool call to a
trace sequence number. It was also run on a read-only copy of a real local
Claude Code session: one root plus nine sub-agents and one workflow agent produced
11 separate training rows. The root retained 601 matched tool calls/results;
all nine sub-agent files were kept despite `isSidechain=true`; the workflow row
retained its `isCompactSummary` user context and reported its `compact_boundary`.
Repeated snapshots were merged (895 duplicate snapshots in the root), while 600
opaque thinking blocks were excluded. The local session had no API trace, so its
system prompt and tool schemas were correctly reported unavailable rather than
invented.

The workflow agent contained a PDF returned inside a tool result as a 17.4M
character base64 document block. Conversion now writes the decoded PDF to
`assets/` and keeps a file reference in the tool response; a SHA-256 comparison
against the decoded source payload passed. The exported training JSONL no longer
contains that base64 payload. Image and document assets are listed separately in
the per-sample manifest; the root session's Claude-specific `image.file.base64`
block is also extracted. These file references preserve provenance, but a
training pipeline that should consume PDF/image contents must load the sidecar
assets explicitly.

