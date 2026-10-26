# DeepSeek Recipe Notes

The local reference is the shallow clone at `deepseek-recipe/`, currently at
commit `935817c`. It is a protocol conversion and stream formatting library,
not an HTTP client or inference server. The caller still owns HTTP transport,
backend inference, and tool execution.

The repository has three independent protocol adapters:

- Anthropic Messages: `deepseek-recipe/deepseek-recipe/src/protocol/anthropic/messages/`
- OpenAI Chat Completions: `deepseek-recipe/deepseek-recipe/src/protocol/openai/chat_completion/`
- OpenAI Responses: `deepseek-recipe/deepseek-recipe/src/protocol/openai/responses/`

Each adapter has its own request schema/converter and response/chunk generator.
The Responses adapter additionally has an accumulator keyed by output item ID
and index. It joins text deltas, reasoning text deltas, function-call argument
deltas, and custom-tool input deltas, then replaces each field with the final
`done` value when one is supplied. This is why a capture proxy must preserve
every SSE event in order; a final reconstructed JSON response alone is not
enough to audit stream omissions or malformed partial JSON.

The current recipe's important conversion behavior for data generation is:

- `thinking.type` and `output_config.effort` jointly determine thinking mode;
  an explicit `thinking` setting wins over effort.
- Thinking content is a separate reasoning channel. In thinking mode, earlier
  assistant reasoning is expected to be passed back, and missing reasoning can
  either be filled with an empty string or rejected according to conversion
  options.
- Tool calls are validated and paired with the following tool results. Messages
  requires earlier `tool_use` blocks to be answered immediately by matching
  `tool_result` blocks; this is a useful invariant for transcript audits.
- V4.1 accepts images and mid-conversation system messages. V4 replaces image
  blocks with an unsupported-image placeholder. Documents are handled
  separately from image sources.
- The default maximum token bound is `393216`; thinking and non-thinking bounds
  can be configured independently. The adapter may clamp Messages requests but
  Chat Completions and Responses reject values above their configured bound.
- Reasoning token usage is reported only when the stream processor has a
  tokenizer. A transcript or raw response stream must not infer token counts
  from character lengths.

For the trajectory pipeline, the proxy stores raw body sidecars and does not
apply these conversions. `audit_transcript_trace.py` selects Messages, Chat
Completions, or Responses from the request path, incrementally parses SSE events
in order, and audits response IDs, tool IDs/arguments, terminal events, and
sidecar lengths/hashes. It does not reconstruct training rows. Transcript to
ms-swift conversion still reads only the Claude transcript; DeepSeek/API traces
are verification inputs.

Useful recipe source files for the audit parser are:

- `.../anthropic/messages/response/chunk_generator.rs`
- `.../openai/chat_completion/response/chunk_generator.rs`
- `.../openai/responses/response/accumulator.rs`
- `.../request/options.rs`
- `docs/streaming.md`

