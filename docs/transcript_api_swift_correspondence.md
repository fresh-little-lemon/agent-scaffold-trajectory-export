# Claude Code Transcript, API Trace, and Swift JSONL

> **Superseded 2026-10-26** for request reconstruction: the transcript's `api-request`/shape/blob records rebuild every non-compaction request exactly, and the compaction `<analysis>` can only come from run-time capture. See `docs/transcript_to_calls.md`. Kept for the field-level notes on the first converter.

## 结论先行

`transcript`、`API trace` 和 `Swift JSONL` 不是严格的集合包含关系。

更准确的关系是：

```text
Claude Code runtime
  ├─ native transcript: 持久化事件、分支、附件、工具结果、部分模型消息
  └─ API proxy trace: 线上 HTTP request/response 的完整字节和协议 JSON

native transcript --transcript-only converter--> Swift JSONL + manifest + assets
API trace       --offline reconstruction------> human-readable request/response JSONL
transcript + reconstructed trace --audit------> coverage/difference report
```

API trace 是“实际发给模型/从模型收到的 HTTP 交换”的权威来源；transcript 是
Claude Code 的“运行时事件日志”；Swift JSONL 是从 transcript 提取出的训练表示。
API trace 不应反向填充 Swift JSONL，否则训练数据会悄悄混入 transcript 没有持久化的
内容。

## 三种数据的边界

| 内容 | Transcript | API trace reconstruction | Swift JSONL |
|---|---|---|---|
| assistant 文本、thinking、tool call | 通常有，可能有重复 snapshot | 有实际 response block | 合并 snapshot 后保留可读文本；空 signature thinking 不伪造 |
| user prompt / tool result | 有 Claude Code 事件和原始工具结果 | 有每次请求实际发送的历史 | 按时间线转成 `user` / `tool_response` |
| system prompt | 只有 prompt snapshot 时可得 | 每次请求中实际发送的 system | 每个 session 只保留一个 snapshot-derived `system` |
| tool schema | 只有 snapshot 时可得 | 每次请求的完整动态 schema | 每个 session 只保留一个 `tools` 快照 |
| HTTP headers、status、stream framing、采样参数 | 没有完整记录 | 有 | 不进入训练 row，保留在 trace |
| response ID、tool-use ID | 常有 | 有 | manifest descriptor 保留 ID |
| UUID parent/branch/agent lineage | 有 | 通常只有 header/session 线索 | manifest 保留，训练消息只保留必要映射 |
| attachment/queue-operation provenance | 有 | 只能看到最终发出的内容，不能可靠反推来源事件 | `control-events.jsonl` 和 manifest 保留 |
| 原始 `toolUseResult` | 可能有完整结构 | 只能看到 API 实际发送版本，可能已改写 | `tool_response` JSON 中保留可用字段 |
| compact boundary / summary metadata | 有 boundary 和 summary 标记 | API trace 能看到 summary response，但未必能连接到 transcript | summary 作为 user context；boundary/metadata 在 manifest |
| 图片/文档源文件 | 可能有 base64 或本地源 | 可能有 API Base64，也可能已被改写 | content-addressed assets + `<image>`/file reference |

## 是否是“真子集”

如果把集合限定为“已经持久化、能直接对应到 API message block 的 assistant/tool
语义”，transcript 大致是 API trace 的一个不完整投影，但仍不能称为严格子集，因为：

1. API trace 中有实际发送的 system prompt、动态 tool schema、采样参数和 compact
   response，transcript 可能没有。
2. transcript 中有 parent UUID、agent/workflow 分支、queue/attachment 来源、原始
   `toolUseResult` 等 API body 没有的运行时信息。
3. Claude Code 可能在发送前改写工具输入，例如本次 page 13 的 Bash 命令增加了
   `cd /home/node &&`；两边内容因此不相同。
4. API trace 记录“模型看到的版本”，transcript 往往保存“工具执行/客户端事件的
   版本”。Deferred tool 的 `tool_reference` 就属于这种差异。

因此不能用 `transcript ⊂ API trace` 或 `API trace ⊃ transcript` 来定义训练管线。
正确做法是保存两者，并按 ID、分支和事件位置做审计。

## Swift JSONL 的转换关系

转换器只读取 transcript：

1. 根 session 中默认排除交错的 `isSidechain` 记录；独立 sub-agent/workflow 文件
   各自成为训练样本。
2. 按 `message.id` 合并 assistant streaming/retry snapshot；同一 text/thinking
   slot 只保留最长版本，tool block 按 `tool_use.id` 去重。
3. `thinking` 非空文本变成 assistant 的 `<think>...</think>`；空文本加 signature
   只进入 manifest 计数。
4. `tool_use` 变成 `role=tool_call`；`tool_result` 变成 `role=tool_response`，并
   保留 `tool_use_id`、`is_error` 和可用的 `toolUseResult`。
5. 有 rendered 内容的 attachment 按 transcript 位置插入，其中包括
   `Monitor event: ...` 这类 system reminder；queue lifecycle 本身只进 manifest
   和 control sidecar，不伪造模型轮次。
6. auto-compact 的 `isCompactSummary` user 内容保留在消息时间线；
   `compact_boundary` 只保留 metadata，不制造 user/assistant turn。
7. 图片和文档解码到 `assets/`，训练消息使用 `<image>` 或文件引用；manifest 记录
   asset 路径和哈希。
8. top-level `system`/`tools` 使用该 session 最新可用 prompt snapshot。它们不是
   每次 API request 的动态 schema，因此不能逐请求复现 API body。

标准 row 形态是：

```json
{
  "system": "...",
  "tools": "[{...}]",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "<think>...</think>"},
    {"role": "tool_call", "content": "{\"name\":\"Bash\",\"arguments\":{...}}"},
    {"role": "tool_response", "content": "{\"tool_use_id\":\"...\",\"content\":...}"}
  ]
}
```

## API trace 的重建和审计

`api_proxy.py` 不解析、不改写请求，只记录 headers、status、completion flags、
sidecar 路径、字节数和 SHA-256。`rebuild_wire_jsonl.py` 再按协议把 SSE 聚合为
普通 JSON：Messages content blocks、Chat Completions deltas、Responses output items
分别重建。大 body 仍以 sidecar 流式读取，不需要把完整 prompt 放在内存中。

`audit_training_vs_api.py` 只做核查：

* body 长度和 SHA-256 是否匹配；
* transcript `api-request` 次数是否覆盖 generation request；
* response ID 和 tool-use ID 能否连接到 manifest/Swift row；
* thinking/text/tool call/tool result 是否一致；
* 图片 Base64 解码后的 SHA-256 是否与训练 asset 文件一致；
* system prompt/tool schema 是否只是近似或存在动态版本差异。

审计失败时不能从 API trace 把缺失内容补回训练行；应修复 capture/transcript
记录，或将该样本标记为不完整。

## 特殊情况处理

### auto-compact

`compact_boundary` 是客户端生命周期元数据；其后的 `isCompactSummary` user 记录
是模型可见的压缩上下文，应作为 user context 保留。API trace 中可能出现生成该摘要
的成功 response。本次有 4 个 compact response ID 没有连接到 transcript assistant
记录；逐条按时间戳对齐后，API response 的 `<summary>` 标签内文都逐字符出现在随后
`isCompactSummary` user 正文中，因此训练上下文里的摘要可仅从 transcript 恢复。该 user
记录还包含 Claude Code 加上的续接说明。

但 API response 的 `<analysis>` 内文、response ID、usage 和原始 assistant envelope
未保存在 transcript 或 `stream-json` 输出中。转换器应继续把 compact summary 标为
特殊 user context，不能把它误标成普通用户输入，也不能声称它是完整原始 assistant
response。若训练目标需要 analysis 或逐调用的完整 response，必须在运行时另行捕获。

### sub-agent 和 workflow

并行 branch 不能按全局 API sequence 直接拼接。每个独立 transcript 文件单独转成
row，manifest 通过 `agent_id`、`logical_parent_uuids`、source UUID 和 tool result
反向连接 root 的 Agent/Workflow 调用。

### attachment、Monitor event、queue-operation

只有有 rendered payload 的 attachment 才进入消息时间线；完整源记录、alternate
render variant 和 queue lifecycle 进入 `control-events.jsonl`。未找到 rendered
对应物的 queue payload 标成 unresolved，不能武断地认为模型不可见。

### thinking

可读的 `thinking` 文本可以训练；只有 signature 的空 thinking 没有可解密 plaintext，
只能保留 provenance 计数。API trace 能审计 signature/response block，但不能凭空
恢复 provider 隐藏内容。

### 图片和文档

API body 中的 Base64 与 transcript 本地源都可能存在。训练转换保存解码后的 asset，
审计时用原始 Base64 解码哈希和 asset 文件哈希比对；只把 `<image>` 当作文本而不加载
asset 会损失多模态训练信息。

### `tool_reference` 与 deferred tools

`tool_reference` 是 Anthropic Messages 内容块中的结构化 deferred-tool 引用，常见于
Claude Code 拥有大量工具（尤其是 MCP tools）并通过 tool search 延迟加载 schema 的
场景。它不是一次工具调用，也不是工具执行结果本身；它是在 `tool_result` content 中
指向一个可用 tool name，让协议实现知道哪些已声明但 deferred 的工具需要加入当前对话
的可用工具 schema。

本次 vLLM 的 400 错误发生在 Messages request 解析阶段：它提示支持 text/image content，
却收到 `tool_reference`。这是 API server 的协议兼容缺口，不代表 DeepSeek 模型本身不
理解这些工具。DeepSeek Recipe 的 Messages converter 会从 `tool_result` 中读出
`tool_name`，在当前 `tools` definitions 里查找 schema，并将引用工具作为可用 schema
更新到 prompt；Recipe 还支持 `defer_loading`。源码可查：
[`schema.rs`](../deepseek-recipe/deepseek-recipe/src/protocol/anthropic/messages/request/schema.rs)
和 [`convert.rs`](../deepseek-recipe/deepseek-recipe/src/protocol/anthropic/messages/request/convert.rs)。

本次某些调用先以 `tool_reference` 到达 vLLM 并失败；后续成功请求中观察到 Claude Code
发送兼容性占位文本 `[Tool references removed - tool search not enabled]`。这说明它改变了
模型实际看到的内容，不能把占位文本当作原始 reference 或可互换的训练样本。

### 白名单提案的边界

之前提到的白名单没有落到代码里：当前 [`e2e/api_proxy.py`](../e2e/api_proxy.py)
仍是透明转发，没有过滤或改写；Claude Code launcher/config 也没有新增工具白名单。
当时的想法是限制 Claude Code 可见的工具集合，减少 deferred tool search/reference
进入不兼容 vLLM 的机会。这只是一个未验证的规避方案，不是已实现的修复。

白名单本身不能解决 parser 不认识 `tool_reference` 的问题。即使只允许某些 tool names，
只要 Claude Code 对其中工具继续 defer schema，request 仍可能带同一种 block。还要区分：

* **执行权限白名单**：哪些工具允许调用/无需人工确认；它不一定改变 Messages content
  block 的序列化，也不保证关闭 deferred loading。
* **工具注册白名单**：Claude Code/MCP 启动时只注册指定的工具；可能减少工具数量，但若
  剩余工具仍 deferred，仍可能产生 references。
* **协议兼容层**：server 实现 reference 解析和 schema 更新，或在 proxy 中基于严格的
  `tool_name -> full schema` 白名单将 references 转成后端支持的等价表示。后者会改写
  请求，必须先保存客户端原始 body，并另存实际转发 body；不能直接删除 blocks 或只留
  空文本，否则模型可能失去可用工具信息。

因此，当前推荐顺序是先确认 Claude Code 是否有受支持的配置可把相关工具改为 eager
schema，再检查 outgoing request 中 `tool_reference` 是否消失；若没有这个公开开关，
应在 vLLM/Anthropic adapter 中实现 Recipe 同等的 reference/schema 语义。简单过滤、
只改允许名单、或把 block 改成工具名文本都不能视为等价实现。

## 推荐的生产数据模型

对每个 session/page 保存四层文件：

```text
transcript/*.jsonl                 # 原始 Claude Code 事件，训练事实来源
traces/*.jsonl + *.wire/*.bin      # 透明 API capture，审计事实来源
traces/*.reconstructed.jsonl       # 人类可读 request/response JSON
training/{train,manifest,control-events}.jsonl + assets/
                                      # transcript-only 训练投影和 provenance
```

任何一条训练 row 都应能通过 manifest 回到 transcript 行；任何 API mismatch 都应
在 audit report 中保留 ID、sequence、字段和哈希，而不是静默修复。
