# 本次 DeepSeek V4.1 运行：Transcript 与 API Trace 差异清单

数据目录：

```text
/home/tsq/workspace/agent-scaffold-trajectory-export/e2e/outputs-20261026-deepseek-v41/
```

本清单使用三个页面 13、14、401 的 Claude Code transcript、API 原始 sidecar、
重建 JSON 和 transcript-only Swift JSONL。API trace 只用于审计，没有用于生成或修复
Swift JSONL。

## 总体结果

| 页面 | API generation 请求 | transcript `api-request` 事件 | HTTP 200 / 400 | 成功 response ID | 可连接到训练行 | assistant 输出块相同 | tool call 参数相同 | tool result 相同 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 13 | 88 | 88 | 84 / 4 | 84 | 82 | 81/82 | 104/105 | 100/102 |
| 14 | 14 | 14 | 12 / 2 | 12 | 12 | 12/12 | 18/18 | 14/16 |
| 401 | 106 | 106 | 102 / 4 | 102 | 100 | 100/100 | 123/123 | 114/117 |

共 223 个 HTTP 交换记录。重建 JSON 中所有可用 request/response body 的字节数和
SHA-256 都与 proxy 元数据相同；JSON parse error 和未完成 response 均为 0。因而以下
差异不是 sidecar 读取或 SSE 重建造成的。

HTTP 400 的 10 次请求中，DeepSeek/vLLM Messages parser 拒绝了
`tool_reference` content block。400 是失败模型请求，没有可训练的 model response；
仍应保留在 raw trace 和 audit 中。

## API Trace 有而 transcript-only 训练行缺少的内容

| 差异 | 本次观察 | 对训练的影响 |
|---|---|---|
| Auto-compact 的原始 assistant response | 页面 13 的 sequence 18、60，以及页面 401 的 sequence 25、98，response ID 没有出现在 transcript。按 capture timestamp 对齐后，四条响应分别紧邻 `compact_boundary` 和后续 `isCompactSummary` user 记录 | 原始 response envelope、ID 和 `<analysis>` 正文不在 transcript。需要区分的是，四条响应的 `<summary>` 标签内正文都逐字符包含在对应 `isCompactSummary` 正文中；后者在前面加了 284/743/284/877 字符续接说明，并在末尾加了相同的 737 字符运行时说明。因此 summary 内容没有丢失，但不能仅据 transcript 还原完整 assistant response 或 analysis target |
| 每个 request 的完整 system prompt | API system 有每次请求的确切值；prompt snapshot 只在 transcript 有记录时可用。API system prompt 完全相同率为 0，文本相似度约 0.96–0.97 | 当前 Swift row 的一个 `system` 字段不能逐请求还原输入。API body 是核对每次调用 system 的依据 |
| 每个 request 的完整 tool schema | API tools 随调用变化：页面 13 有 12/14/30/33/87 个工具版本，14 有 12/14/87，401 有 12/14/31/34/87；Swift row 每个 session 只有一个 schema 快照 | 不能用 session 的单一 `tools` 字段声称等价于所有 API calls。训练需要逐调用 schema 时，应使用带 message/request 边界的扩展格式或单独的 request-level sidecar |
| Claude Code 发 API 前的工具输入改写 | 页面 13 有一个 Bash call：API `command` 前面多 `cd /home/node &&`，transcript 的 `tool_use.input` 没有这个前缀 | 训练“模型原始输出”应以 transcript 中的 tool call 为 target；训练“客户端实际发出的请求”则需另存 API-side input。不要将两者静默合并 |
| Deferred-tool 引用的请求兼容改写 | 成功的后续请求中，7 个 tool result 的 `tool_reference` 被短文本 `[Tool references removed - tool search not enabled]` 替代 | API trace 能说明模型实际看到的是兼容占位文本；transcript 保留的是 Claude Code 的结构化 deferred-tool result。它们是两个语义不同的序列化形式 |

## Transcript 有而 API Trace 不能普遍还原的内容

API trace 捕获请求和响应，不是 Claude Code 文件系统事件日志。因此不能只凭 HTTP body
可靠还原下列 transcript provenance：

| Transcript 内容 | 为什么 API trace 不足 | 建议用途 |
|---|---|---|
| `uuid`、`parentUuid`、`logicalParentUuid`、`agentId`、`isSidechain` | API HTTP 序号能排请求顺序，但不能表达 Claude transcript 的完整 parent/child 事件图；session header 也不等同于 branch lineage | 划分 root/sub-agent/workflow row，并将 Agent/Workflow call 与 branch 连接 |
| attachment 的来源、delivery ID、rendered role、alternate renderings | API body 只呈现最终发出的请求内容，不说明它来自 Monitor、hook、queue delivery 或哪一个 render variant | 确认 `Monitor event: ...` 等文本是否由客户端渲染，以及选择哪种 render 进入训练 |
| `queue-operation` 生命周期 | enqueue/remove/popAll 等本地队列事件不是 HTTP model request，API trace 看不到这些操作 | 审计通知是否送达、关联 task/tool-use ID；不应凭 lifecycle 自造模型轮次 |
| 原始 `toolUseResult` 和工具执行 metadata | API trace 只含客户端实际放进 request 的 tool-result 版本。未进入下一次 request 的本地结构不会出现在 trace | 工具运行审计、workflow lineage、最后一次工具输出检查；训练 context 使用模型实际收到的版本 |
| 只发生在 Claude Code 内而未发送给 API 的事件 | 没有 HTTP exchange | 保留在 control sidecar，不作为模型输入/target，除非另有证据证明模型看到了它 |

这些 transcript-only 内容多为 provenance、工具运行和控制流程信息，不意味着它们都
应该作为语言模型的 target。若某条 rendered attachment 确实发给模型，它的最终文本应
出现在对应 API request；transcript 额外提供的是它从哪里来、有哪些备选渲染和分支归属。

## 本次已匹配部分

* 可连接 response ID 的 assistant text/thinking/tool-use block 基本完全匹配；唯一不同的是
  上述一个 Bash `cd /home/node &&` 客户端前缀。
* 四条未按 response ID 连接的 compact 响应，其 `<summary>` 内文都精确嵌入后续
  `isCompactSummary` user 正文；原始 `<analysis>` 内文则不在 transcript 或 CLI stream 中。
* 18、105、123 个 API tool call 中分别有 18、104、123 个参数与训练行相同。
* 7 个 tool-result 差异均为 deferred tool 引用/占位，不是图片丢失。
* 页面 13 的 5 个、页面 401 的 8 个图片 asset 已将 API Base64 解码后 SHA-256 与磁盘
  asset 文件重算比对；均相同，没有缺失图片文件。页面 14 没有图片 asset。
* 每页 API request 数与 transcript `api-request` 事件数一致，但这只证明调用计数一致，
  不能证明 request body 字段都存在于 transcript。

## “真子集”判断

若只看**逐次模型调用的精确输入/输出**，API trace 是最完整的可审计来源；transcript-only
Swift JSONL 不是其无损副本。本次 compact response 的 ID、envelope 和 analysis 缺失（summary
内文可从 compact 后的 user context 恢复）、system/tools 逐 request
变化以及一条客户端改写。

若看**所有可用于训练 pipeline 的结构信息**，transcript 又包含 API trace 没有的 branch、
attachment、queue 和工具执行 provenance。因此 `transcript-only` 不应被叫作 API trace
的严格真子集；两者是部分重叠、各自有独有信息的来源，Swift JSONL 则是 transcript 的
有损/规范化训练投影。

逐 ID 数据见：

```text
training-audit/13/training-api-audit.json
training-audit/14/training-api-audit.json
training-audit/401/training-api-audit.json
```
