# Rebuilding every model call from the transcript (`transcript_to_calls.py`)

Goal: from a saved workspace alone (`workspace/<id>/.claude/`), rebuild every Messages API call Claude Code made,
exactly as sent (system prompt, tools, every message, sampling/context params, cache_control) together with its
response, and turn the calls into ms-swift agent SFT rows. The API trace (`docs/api_capture.md`) is not an input;
it is the ground truth the result is audited against.

Verified 2026-10-26 on Claude Code 2.1.310 with DeepSeek-V4.1-Flash. Probe scripts: `tests/e2e_capture/`.

## What the transcript records about each request

Besides user/assistant/attachment records, Claude Code 2.1.3xx writes content-addressed request records into
the session JSONL (`projects/<cwd>/<session>.jsonl`) and into every subagent JSONL
(`<session>/subagents/**/agent-*.jsonl`):

| Record | Fields | Meaning |
|---|---|---|
| `api-request-shape` | `shapeHash`, `shape: {system, tools}` | one per distinct system prompt + tool list |
| `api-request` | `id`, `base`, `keep`, `tail`, `shapeHash`, `params`, `cacheControl`, `querySource`, `requestDigest` | one per call |
| `api-request-blob` | `hash`, `message` | a tail message that the regular records cannot reproduce |

The messages of a call are `messages(base)[:keep] + [message(h) for h in tail]`: each request is an increment on
an earlier one. `params` holds `model`, `betas` (sent as a header, not in the body), `max_tokens`, `thinking`,
`context_management`, `output_config`. `cacheControl` lists `{path, value}` markers to put back.

A message hash is `sha256(json.dumps(message without cache_control, sort_keys=True, separators=(",", ":"),
ensure_ascii=False))`. This gives every message a verifiable fingerprint.

So the system prompt and tool list of every individual request are already versioned inside the transcript. The
tool list really changes during a session (item 13: 12/14/30/33/87 tools across 5 shapes), and all 88 requests of
item 13, including its workflow subagents, matched their shape exactly. No extra versioning of the workspace is
needed to know what each call saw.

## Algorithm

1. Pool shapes and blobs from the main session file and all its subagent files.
2. Per file, walk `api-request` records in order. Resolve each tail hash from a blob, else from candidates built
   from the records of that file: an assistant message is all records sharing one `message.id` (Claude Code writes
   thinking, text and each tool_use as separate records); a user message is one user record or a run of
   consecutive user records (up to 6). A candidate is used only if its hash equals the tail hash, so a rebuilt
   message is exact or reported as `unresolved`, never guessed.
3. Request = shape system/tools + messages + params, then put the `cacheControl` markers back. One wire-form
   rule matters here: a `role: system` message is sent as a plain string unless a cache_control sits on it, in
   which case it is a one-element list of text blocks (measured: 236 string vs 9 list, all 9 cached).
4. Response = the assistant message whose first record follows this `api-request` record and precedes the next
   one in the same file (content, stop_reason, usage, id).

Status per call: `ok`, `no_response` (the call failed, e.g. HTTP 400, or was in flight when the process stopped;
Claude Code logs the request either way), `unresolved_messages`.

## Audit result (`audit_calls_vs_trace.py`)

Rebuilt calls are matched to wire requests by their message list (hashes) and compared field by field.

| Run | Calls | Matched to a wire request | Messages incl. cache_control | System | Tools | Params | Responses equal |
|---|---|---|---|---|---|---|---|
| items 13, 14, 401 (2026-10-26, old proxy) | 208 | 204 | 204/204 | 204/204 | 204/204 | 204/204 | 193/194 |
| item 401, batch stopped mid-run (new pipeline) | 22 | 22 | 22/22 | 22/22 | 22/22 | 22/22 | 17/17 |

Every wire request without a rebuilt call was a compaction call. The 4 calls without a wire request were in
flight when the old proxy was killed (the new proxy keeps those). The one response difference is described
below.

## What the transcript cannot give, and what to do

| Gap | Why | Handling |
|---|---|---|
| Compaction call (request and response) | not logged as `api-request`; the stored summary (`isCompactSummary` user record) drops the response's `<analysis>` block and rewrites `<summary>` as `Summary:` | the summary itself is in the next requests, so later calls are exact. To also train the compaction call, capture it at run time: a `PostCompact` hook gets `compact_summary` = the raw response text incl. `<analysis>` (tested; no thinking/usage), or take it from the trace. Not enabled; a decision for the user |
| Response of a call that never finished | in flight when stopped, or HTTP error | status `no_response`; the trace has the request and (new proxy) the partial response |
| Bash `cd <cwd> && ` prefix | Claude Code strips a leading `cd /home/node && ` from Bash tool_use commands before storing (and before sending them back in later requests) | the rebuilt response is what later calls saw; the raw model output exists only in the trace (1 of 194 responses) |
| Attribution header system block (`x-anthropic-billing-header: cc_version=...`) | per-request block, not in the shape | `run_batch.py` sets `CLAUDE_CODE_ATTRIBUTION_HEADER=0`, so it is not sent at all; for old runs the audit ignores it |

## ms-swift rows (`calls_to_swift.py`)

One row per call: that call's exact request as context and its response as the only trained turn
(`"loss": true`; every earlier assistant/tool_call message `"loss": false`, supported by ms-swift's per-message
`loss` key). Earlier thinking stays in the context exactly as it was sent. With `--merge-chains`, consecutive calls
are folded into one row when the later request is exactly the earlier request + its response + new messages under
the same system/tools/params (compared by text, ignoring the string vs one-block-list form); every response is
then trained once with the context it really had. Items 13/14/401: 194 trained responses in 194 rows (27 MB) or
21 merged rows (4 MB).

Rendering: system blocks → `system` (joined by newline); tools → OpenAI function schema JSON; user text/images →
`user` (images extracted to `--assets`, `<image>` markers); tool_result → `tool_response` with the tool output the
model saw (no JSON envelope); `role: system` messages → `system` (folded into the previous turn by
`e2e/prepare_megatron_dataset.py`); thinking → `<think>...</think>` assistant; tool_use → `tool_call`
`{"name", "arguments"}`. Calls with `stop_reason: max_tokens` are skipped unless `--keep-truncated`. Provenance per
row goes to `<output>.meta.jsonl`.

The rows are one text rendering. The calls JSONL is the lossless record; to train with a model's own chat
encoding (e.g. `deepseek-recipe`), encode the calls JSONL directly.

`claude_jsonl_to_swift.py` (one row per session, rebuilt from user/assistant/attachment records) is superseded:
it takes system/tools from the last snapshot only, can place tool responses before their tool calls, and
includes `toolUseResult` metadata the model never saw.

## Commands

```bash
python3 transcript_to_calls.py --workspace-root <out>/claude_code/<model>/workspace --out calls.jsonl
python3 rebuild_wire_jsonl.py --trace <out>/claude_code/<model>/items/<id>/traces/<id>.jsonl --output <id>.reconstructed.jsonl
python3 audit_calls_vs_trace.py --calls calls.jsonl --workspace <workspace/<id>> --trace <id>.reconstructed.jsonl
python3 calls_to_swift.py calls.jsonl train.jsonl --assets assets --merge-chains
python3 e2e/prepare_megatron_dataset.py train.jsonl prepared.jsonl --asset-root assets --image-prefix /data/assets
```
