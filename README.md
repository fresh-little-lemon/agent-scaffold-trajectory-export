# Claude Code trajectory export

Remote working directory: `/home/tsq/workspace/agent-scaffold-trajectory-export`.

`claude_jsonl_to_swift.py` converts Claude Code native session JSONL into
ms-swift Agent JSONL while streaming large inputs. It merges repeated assistant
snapshots, filters interleaved root sidechains while keeping
standalone sub-agent/workflow files as separate samples, extracts base64 images
and embedded documents to sidecar assets, and writes an auditable `manifest.jsonl` with compaction metadata,
summary source rows, and branch lineage.
Model-rendered `attachment` records, including `Monitor event` system reminders,
are inserted into the training timeline with their recorded role. Prompt snapshots
provide system prompts and tool schemas when present in the transcript. API
traces are consumed only by `audit_transcript_trace.py` for coverage checks and
never populate training rows.
`queue-operation` payloads and ordered lifecycle fields remain in the manifest
and `control-events.jsonl`, linked to attachment lines and train-message indices;
unmatched events are explicitly marked unresolved rather than assumed invisible.
The algorithm and limitations are documented in
`docs/trajectory_methodology.md`.

Validation completed:

- `tests/test_converter.py`: 13/13 tests passed after adding rendered attachment,
  queue lifecycle, and prompt snapshot coverage; `py_compile` passed.
- Real Qwen page 401: transcript-only conversion produced one root row and four
  workflow rows with readable Qwen thinking retained; separate audit found 79/85
  response IDs and 111/114 tool IDs in the transcripts, so it reported an
  incomplete capture rather than filling six missing responses from the trace.
- Real Opus page 14: 216 messages; 101 tool calls and 101 tool responses; all
  tool calls joined; 25 opaque thinking blocks excluded; 17 image assets.
- ms-swift 4.5.3 + `/slow_share/models/Qwen3.5-2B`: one-step LoRA training
  completed on A800 GPU 0, loss `1.383`, checkpoint under `smoke/sft-mini2/`.

The full Qwen and Opus smoke rows encode to about 96,913 and 131,011 tokens
with the Qwen3.5 template. The one-step training smoke uses a separately created
short row (`smoke/train-mini.jsonl`) to validate the Agent roles without claiming
that a full 100k-token sample fits a 512-token training limit.

The latest Claude Code image is built locally from the existing evaluation image
and tagged `agent-scaffold-claude-code:2.1.310`; the Dockerfile is
`Dockerfile.claude-latest`. Non-empty transcript thinking is retained as
`<think>` assistant text. Empty thinking with only a provider signature is kept
only as a provenance count; no provider-hidden chain of thought is invented.

Real local `.claude` validation: copied one root transcript, its nine sub-agent
logs, one workflow-agent log, and the exact newly active sub-agent transcript into
local copies (the source directory was not modified). Conversion produced 12
separate rows in `local-claude-output-20261024-attachments-full-3/`. This pass
retained the root's 114 rendered attachment messages and the current sub-agent's
103. It indexed 494 attachments across all branches, of which 300 had usable
rendered text and 194 were metadata-only records; it also retained 146 ordered
queue operations and extracted 267 task-notification payloads from the two event
types. The task `Monitor event: ...` notifications, including progress and
`RemoteTraceback` events, appear as model-role messages when the JSONL provides
their rendered form. Alternate human-turn renderings are recorded by length and
hash, and the full source event is kept in `control-events.jsonl`. Raw export is
the default; optional redaction is applied only when explicitly requested. The local
source `.claude` directory remains unchanged.

## Current Qwen3.8 end-to-end run

The 2.1.310 + Qwen3.8 run is staged under `e2e/` and writes to the isolated
remote path `e2e/outputs-20261025-qwen38/`. It has not completed yet: port 8108
was not serving Qwen3.8 at the last check. Start the model with
`e2e/start_vllm.sh` after all eight GPUs are available, then confirm
`/v1/models` reports `qwen3.8-27b` before running
`python3 e2e/run_batch.py --config e2e/run_config.json`.

The launcher uses the provided `/dev/shm/models` mount, TP=8, Qwen3 reasoning
and Qwen3 Coder tool parsers, Rust frontend, and a 1M-token model length. The
current nightly previously warned that auto tool choice has no effect in the
Rust frontend; the actual MCP tool-call run will verify whether this combination
works. The current `e2e/api_proxy.py` is a transparent capture proxy: it
forwards all HTTP methods and endpoints without API-specific rewriting and
records received headers plus raw request/response payload sidecars. Streaming
SSE bytes are forwarded unchanged; no `max_tokens` cap, credential injection,
tool-name repair, redaction, or response reconstruction is performed.
`audit_transcript_trace.py` recognizes schema-v2 wire records, checks sidecar
lengths and hashes, incrementally parses SSE/JSON responses, and compares
protocol response/tool IDs with transcript IDs where the protocols share IDs.
It never uses trace content to construct training rows. Legacy traces with
`request_original`/`request` remain readable as semantic traces but are not
byte-exact.

`rebuild_wire_jsonl.py` creates the human-readable audit view. Its
`request.body` is the parsed API request JSON, while `response.body` is the
non-streaming response reconstructed from the SSE stream. It keeps Base64 only
when the API payload itself contains image data encoded that way; it does not
Base64-encode complete HTTP bodies.

The DeepSeek V4/V4.1 protocol reference is cloned under `deepseek-recipe/` and
summarized in `docs/deepseek_recipe_notes.md`. It keeps separate adapters and
stream accumulators for Messages, Chat Completions, and Responses; the raw
capture format preserves the event ordering those accumulators require. The
synthetic protocol parsers and capture proxy tests pass locally. A real
DeepSeek V4.1 run is under
`e2e/outputs-20261026-deepseek-v41/`; requests containing `tool_reference`
receive HTTP 400 from vLLM and remain captured for audit.

`audit_training_vs_api.py` compares transcript-derived Swift rows to the
human-readable reconstructed exchanges without using API data to build rows.
It checks wire body hashes, response IDs and content, tool calls/results,
request history, system prompts, and tool schemas. Example:

```bash
python3 audit_training_vs_api.py \
  --manifest e2e/outputs-20261026-deepseek-v41/training-audit/13/manifest.jsonl \
  --train e2e/outputs-20261026-deepseek-v41/training-audit/13/train.jsonl \
  --reconstructed e2e/outputs-20261026-deepseek-v41/claude_code/deepseek-v4.1-flash/traces/13.reconstructed.jsonl \
  --output e2e/outputs-20261026-deepseek-v41/training-audit/13/training-api-audit.json
```

The relationship between native transcripts, reconstructed API exchanges, and
Swift rows is documented in `docs/transcript_api_swift_correspondence.md`.
The measured field-by-field differences for pages 13, 14, and 401 are in
`docs/transcript_api_differences_deepseek_v41.md`.
