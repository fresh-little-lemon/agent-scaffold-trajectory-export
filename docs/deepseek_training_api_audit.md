# DeepSeek V4.1 training/API audit

The audit compares `train.jsonl` produced from Claude transcripts with the
human-readable `*.reconstructed.jsonl` exchange files. The reconstructed API
trace is evidence only; it is never used to construct a training row.

| page | API generation requests | transcript `api-request` events | successful responses | response IDs joined | assistant blocks exact | tool calls exact | tool results exact | successful response IDs absent from transcript |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 13 | 88 | 88 | 84 | 82 | 81/82 | 104/105 | 100/102 | 2 |
| 14 | 14 | 14 | 12 | 12 | 12/12 | 18/18 | 14/16 | 0 |
| 401 | 106 | 106 | 102 | 100 | 100/100 | 123/123 | 114/117 | 2 |

All reconstructed request/response sidecars passed byte-length and SHA-256
metadata checks. There were no JSON parse errors or incomplete streams. The
HTTP status counts were page 13: `84 x 200`, `4 x 400`; page 14: `12 x 200`,
`2 x 400`; page 401: `102 x 200`, `4 x 400`. The 400 calls are the known
DeepSeek `tool_reference` compatibility failures and are retained in the raw
capture.

Four successful compact-summary response IDs are not joined to transcript
assistant records. Timestamp alignment shows that each response immediately
precedes a `compact_boundary` and an `isCompactSummary` user record. In all four
cases, the text inside the API response's `<summary>...</summary>` tags is an
exact character-for-character substring of that user record. The transcript
therefore preserves the compacted summary content that becomes later context,
with Claude Code continuation text wrapped around it.

The full API assistant response is still not recoverable from the transcript:
its response ID, outer response envelope, usage metadata, and `<analysis>` text
are absent. The analysis body was not found in the compact-adjacent transcript
records, other source transcripts, or the captured Claude CLI `stream-json`
output. For training, retain the compact summary as a specially marked user
context record; do not relabel it as an ordinary user prompt or claim it is the
original assistant response.

The exact tool-call mismatch is one Bash call on page 13. Claude Code sends a
`cd /home/node &&` prefix that is absent from the transcript's stored tool
input. Tool-result mismatches are limited to deferred-tool lifecycle records:
the successful API request contains a short compatibility placeholder while
the transcript stores the original `tool_reference` blocks. These are real
wire-versus-transcript representation differences, not body-capture loss.
All image-bearing tool results match after checking the decoded API image bytes
against the extracted asset SHA-256 encoded in the asset filename.

The joined assistant blocks, tool-call arguments, tool results, and high-level
request history are not the whole story: four compact-summary response IDs and
their analysis/envelopes are missing even though their summary text is embedded
in the compacted user context; user-history grouping/order differs after
compaction, and per-request system/tool schemas vary while Swift rows keep one
system/schema snapshot per session. So these workspace transcripts do not
contain enough information to reproduce every model-visible request and
response exactly on their own.

System prompts are close but not byte-identical (roughly 0.96-0.97 similarity
in this run). API tool schemas are dynamic: requests use 12-87 tools while a
Swift row stores one static schema set (11-87 tools). The manifest and control
sidecar retain prompt snapshots and lifecycle records, but a single static Swift
row cannot exactly reproduce every per-request schema without an additional
versioned prompt/schema representation.

The detailed, ID-level reports are:

```text
e2e/outputs-20261026-deepseek-v41/training-audit/13/training-api-audit.json
e2e/outputs-20261026-deepseek-v41/training-audit/14/training-api-audit.json
e2e/outputs-20261026-deepseek-v41/training-audit/401/training-api-audit.json
```

For the complete source relationship and conversion rules, see
[`transcript_api_swift_correspondence.md`](transcript_api_swift_correspondence.md).
For the field-by-field differences found in this run, see
[`transcript_api_differences_deepseek_v41.md`](transcript_api_differences_deepseek_v41.md).
