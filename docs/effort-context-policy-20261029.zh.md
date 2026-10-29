# Claude Code effort、auto-compact 与模型上下文核验（2026-10-29）

本文记录一次只读审计的结论。审计对象包括 Claude Code 2.1.310 的真实请求、当前真实负载启动命令、DeepSeek recipe，以及 A800 上正在使用的 vLLM-backport。文中把“请求中声明的参数”“渲染器实际使用的参数”和“模型可以接受的上下文上限”分开，避免把它们混为一谈。

## 结论先行

当前 A800 的 `image_baseline_8_duration_c8_20261029` 负载不是 Ultracode，而是 `--effort high`。配置文件、`run_tasks.py` 生成的 Docker 命令、Claude 到代理的首个 Messages 请求三处一致：请求的 `output_config.effort` 为 `high`。因此该轮结果只能作为 high/stock-renderer 基线，不能宣称测试了用户要求的 `/effort ultracode` 或 xhigh 工作流。

Claude Code 的 Ultracode 是一个会启用动态 workflow 的会话设置；headless 启动时应显式传 `--effort ultracode`。2.1.310 会把它记录为请求中的 `output_config.effort: "xhigh"`。vLLM-backport 当前并不会把 xhigh 静默改成 high：Anthropic adapter 原样转发为 `reasoning_effort=xhigh`，DeepSeek V4.1 tokenizer 再按自己的映射渲染预算。

当前服务的 DeepSeek V4.1 stock 映射是 `low=25, high=50, xhigh=75, max=100`；DeepSeek recipe 的 V4.1 参考映射是 `low=50, high=75, xhigh=75, max=100`。因此“Claude high”与“Claude Ultracode/xhigh”是不同的请求，而“recipe-compatible high”还会改变 high 请求的 prompt。后者必须单独作为一个 renderer 变体测试，不能与原始 baseline 混称。

`200000` 和 `80` 是当前实验 runner 显式注入的 auto-compact 策略，不能从这次运行推导为 Claude Code 默认值。`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 的官方环境变量说明还规定：该变量不能把阈值提高到默认值以上；所以仅看到 `80` 还不足以证明实际触发点为 80%，必须审计 native transcript 的 `compact_boundary`、`isCompactSummary` 和相邻 API 请求。

## 证据

### Ultracode 和 headless 参数

本机保存的 Ultracode 文档 `C:\Users\53046\Downloads\claude-code-ultracode.md` 给出以下语义：

- 第 109–110 行：在提示中请求 workflow 或包含 `ultracode` 可以让 Claude 编写 workflow；
- 第 139–147 行：`/effort ultracode` 为会话开启自动 workflow，`claude --effort ultracode` 启动时也会把 effort 设为 `xhigh`；
- 第 159 行：该设置只持续当前 session。

仓库中的 headless 说明 `docs/trajectory_methodology.md:174-187` 与此一致：传给 `-p` 的普通文本关键词不会自动触发 Ultracode；无交互运行必须显式传 `--effort ultracode`，并允许 `Workflow` 与 `Agent` 工具。`e2e/run_batch.py:812-821` 的旧评测路径确实拼接了 `--effort ultracode`；这不能代表当前 `run_tasks.py` 真实负载路径。

### 当前真实负载实际使用 high

当前 arm 的配置为：

`/home/tsq/workspace/deepseek-infra-opt/a800/codex_exp/artifacts/real_tasks/image_baseline_8_duration_c8_20261029/config.json:16-18`

```json
"auto_compact_window": 200000,
"autocompact_pct": 80,
"effort": "high"
```

runner 的对应实现为远端仓库 `e2e/run_tasks.py:180-183` 和 `:200-202`：前者写入 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`、`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`，后者把配置的 effort 拼进 `claude ... --effort high ...`。运行中的 Docker command line 也显示同样的 `--effort high`，没有 `--effort ultracode`。

当前 arm 每个 item 的首个 wire request body 位于对应的：

`.../outputs/claude_code/deepseek-v4.1-flash/items/<page>/traces/<page>.jsonl.wire/.../00000001.request.bin`

对八个 item 的 JSON 只读解析结果一致：

```json
"output_config": {"effort": "high"},
"thinking": {"type": "adaptive", "display": "updates"},
"context_management": {
  "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
},
"max_tokens": 32000
```

请求头中的 `User-Agent` 为 `claude-cli/2.1.310 (external, sdk-cli)`，所以该证据确实来自 2.1.310 的 Claude Code wire request，而非仅来自 runner 配置。

### auto-compact 设置不是服务上下文上限

仓库随附的 Claude 环境变量参考 `D:\openmaic-intern\_ext\ccdocs\env-vars.md` 给出：

- 第 191 行：`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 是 auto-compact window 的触发百分比；值高于默认百分比时会被忽略；
- 第 210 行：`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 是 auto-compact window，可设置为 100000–1000000 tokens；
- 第 274 行：当 Claude Code 不认识自定义模型 ID 时，它会按自己假定的窗口主动 compact；可用 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 修正假定窗口；
- 第 320 行：增大 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 会减少 auto-compact 可用的有效上下文；
- 第 442–443 行：`DISABLE_AUTO_COMPACT` 与 `DISABLE_COMPACT` 的语义不同，分别影响自动 compact 和全部 compact。

本机 Claude changelog `C:\Users\53046\.claude\cache\changelog.md:8913` 只证明某个版本把“auto-compact warning threshold”从 60% 提高到 80%；warning threshold 不等价于实际 compact trigger。因而当前实验应保留显式的 200k/80 配置，同时以 transcript 的 native boundary 记录和 wire request 对齐来判定是否真的按该阈值触发。

### vLLM-backport 对 effort 的实际处理

正在运行的 `dsv41_image_baseline` 容器版本为 `vllm 0.1.dev1+g29e66dad4`。容器内源码证据：

- `vllm/entrypoints/anthropic/protocol.py:114-119` 的 `AnthropicOutputConfig` 接受 `low|medium|high|xhigh|max`；
- `vllm/entrypoints/anthropic/serving.py:520-538` 将 `output_config.effort` 原样赋给 `req.reasoning_effort`；
- `vllm/entrypoints/openai/chat_completion/protocol.py:245-256` 接受 `none|minimal|low|medium|high|xhigh|max`；
- `vllm/entrypoints/openai/chat_completion/protocol.py:580-605` 将 effort 写入渲染参数，并在未显式关闭时用 `enable_thinking = (reasoning_effort != "none")`；
- `vllm/tokenizers/deepseek_v41.py:70-93` 读取 `thinking`/`enable_thinking`，默认 thinking，并验证 `reasoning_effort` 为 low/high/xhigh/max 或 1–100；
- `vllm/tokenizers/deepseek_v41_encoding.py:179-210` 将字符串 effort 映射为数值并生成 `Reasoning Effort: N` 前缀。

所以对于当前版本，xhigh 不是被 vLLM 忽略或强制转换成 high；它会进入 DeepSeek V4.1 的 xhigh 分支并渲染为 75。当前 high 则渲染为 50。

### DeepSeek recipe 的参考映射

本地浅克隆 `deepseek-recipe/` 的源码是权威对照：

- `deepseek-recipe/deepseek-recipe/src/protocol/reasoning.rs:16-23,37-47`：`minimal|low → Low`、`medium|high → High`、`xhigh → Xhigh`、`ultra|max → Max`；
- `:51-76`：显式 `thinking.type` 优先于 effort；无显式 effort 时，thinking 默认 High；
- `deepseek-recipe/deepseek-recipe-encoding/src/v4/dsv41.rs:21-25`：V4.1 budget 为 low=50、high/xhigh=75、max=100；
- `deepseek-recipe/deepseek-recipe/src/protocol/anthropic/messages/request/convert.rs:53-71`：Anthropic `thinking` 与 `output_config.effort` 共同决定思考模式，显式 thinking 优先。

已有 SM80 前端 patch 也记录了这一差异：

- `3d-code-gen/docker/dsv41_sm80/frontend/0003-OPTIONAL-dsv41-effort-budgets-match-reference-encoder.patch:7-16` 把 stock 的 low/high 从 25/50 改为 recipe-compatible 的 50/75，且明确这是会改变 prompt 的可选 patch；
- `3d-code-gen/docs/experiments/2026-10-27-dsv41-a800-serving-perf.zh.md:125-127` 记录了 effort alias 和真实请求 prompt identity 验证；`:194-200` 说明 high budget patch 因改变 prompt 而默认关闭。

这里有一个容易混淆的点：recipe 的 `xhigh` 与 `high` 都是 75，但这不等于协议层把 xhigh 变成 high。它们在 recipe 的枚举中仍是两个不同 effort；只是 V4.1 的参考 prompt budget 相同。

## 模型上下文与 YaRN

在 A800 的实际模型目录中：

- `/slow_share/models/DeepSeek-V4.1-Flash-1017/config.json:43-51`：`max_position_embeddings=1048576`，`rope_scaling.rope_type=yarn`、`factor=16`、`original_max_position_embeddings=65536`；DeepSeek 原生配置目标为 1M；
- `/slow_share/models/Qwen3.5-2B/config.json:50-76`：`max_position_embeddings=262144`，`rope_parameters` 使用 mrope、`rope_theta=10000000`、`partial_rotary_factor=0.25`、`rope_type=default`；
- `/slow_share/models/Qwen3.8-27B/config.json:93-115`：同样为 `max_position_embeddings=262144` 和 mrope/rope_theta 配置，原生文件没有 1M YaRN。

此前 Qwen3.8 服务命令通过启动参数 `--hf-overrides` 把 rope 参数改成 YaRN/factor=4、`original_max_position_embeddings=262144`，并配合 `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` 与 `--max-model-len 1000000`。这属于服务启动时的上下文外推配置，不能反推 Qwen3.5 原生 checkpoint 已经是 1M，也不能只靠训练脚本参数获得 1M 训练能力。

## 对数据生成与训练的建议

1. **不要把 `--max-model-len 262144` 当作转换截断长度。** 它是 vLLM 服务端可接受/调度的上限。Transcript→Swift 转换应保留完整消息、thinking、工具调用、工具结果和 compact 记录；超过服务端上限的请求应在生成阶段由 Claude auto-compact 处理，或显式记录为失败，不能静默截断训练数据。
2. **正确的 Ultracode 负载要单独重跑。** runner 配置应传 `effort: "ultracode"`，实际 Docker command 必须出现 `--effort ultracode`，wire request 必须检查 `output_config.effort: "xhigh"`，并允许 `Workflow` 与 `Agent`；否则只能标为 high arm。
3. **renderer 变体要分开。** 至少保留 `stock`（low=25/high=50/xhigh=75/max=100）与 `recipe_budget`（low=50/high=75/xhigh=75/max=100）两组。recipe_budget 会改变 prompt，必须重新做负载、质量和吞吐测试，不能与 bit-exact baseline 混在一起。
4. **保持 auto-compact 可审计。** 每个样本记录 Claude 启动参数、相关环境变量、native `compact_boundary`/`isCompactSummary`、触发前后 request，以及最终 transcript；服务的 max-model-len 只作为能力边界记录。
5. **上下文训练需配置闭环。** DeepSeek 可按其原生 1M YaRN 配置服务；Qwen3.5/Qwen3.8 若要 1M，必须同时验证模型 rope/Y aRN 配置、vLLM max-model-len、训练框架的 max sequence length、显存和 attention/position-id 实现，不能用 262k checkpoint 配置单独宣称 1M 训练。

