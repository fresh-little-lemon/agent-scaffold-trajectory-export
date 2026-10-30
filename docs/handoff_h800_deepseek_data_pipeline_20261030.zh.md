# H800 DeepSeek 推理与数据生产链路交接

**交接日期：2026-10-30（北京时间）**  
**交接范围：DeepSeek 推理服务、Claude Code 任务调度、video→Three.js→白模视频生产、轨迹/API 审计/训练数据转换，以及后续 H800 实测计划。**  
**状态边界：** 本文把“历史报告”“本轮已完成”“候选/尚未验证”分别标出。所有吞吐和精度数字都绑定原文中的机器、镜像、代码、请求集合与测量阶段；不要将 A800 的旧窗口成绩当成 H800 当前能力。

## 1. 给接手者的摘要

当前目标是把真实 DeepSeek V4.1 Flash + Claude Code 的数据生产任务作为唯一最终负载，在 H800 上迭代提升**质量合格视频任务的完成率**。用户希望以高并发探测系统上限，目标范围是每小时 50–1000 个完成任务；这不是已证明的容量。GPU 利用率高不等于 GPU 算力、Tensor Core、显存带宽都饱和；同时要测生成 token、prefill/cache、队列等待、Docker/主机 CPU、视频渲染/后处理和最终质量。

目前最有把握的新证据仍是有限的 A800 基线与一个短片原生 `/goal` smoke：旧设置下 8 个任务都交付，整批实际吞吐约 **2.49 个/小时**，但这批数据不是当前目标设置，也没有独立视频质量验收。新的原生 `/goal` smoke 后续续跑成功并生成了 `whitebox.mp4` 和 `compare.mp4`；它只用了一个 10 秒片段，首次 90 分钟超时后续跑，主机没有 `ffmpeg/ffprobe`，所以文件尚未完成独立完整解码检查。它证明了路径能够跑通一次，不代表质量合格率或吞吐提升。

H800 上的最佳接手顺序：确认机器、模型权重、驱动/镜像和当前生产负载；先冻结**原始镜像/原始参数**基线并接上质量与 CPU/cgroup 采样；用同一组真实完整任务做首轮高并发容量曲线；然后一次只测一个候选（优先检查 H800 上真实分发的 DeepSeek kernel/FP4 KV/调度方案），每项分别完成逐 bit、实测噪声、闭环视频质量和吞吐门禁。不要先把很多 patch 合在一起，也不要用短的合成请求代替 Claude Code 任务。

## 2. 用户目标和必须遵守的约定

### 2.1 推理与生产目标

- 生产负载是 Claude Code 在真实任务中依据 `media2threejs` skill 复刻视频，交付可编辑 Three.js 场景和渲染后的视频。模型本身必须走完 skill 规定的制作与检查流程；白模视频是交付物，模型也需要查看渲染结果，按 skill 和实际任务决定是否 refine。
- 目标是提升端到端合格任务数，而不是只提高 vLLM token/s。测不同视频时长和复杂度下的真实完成时间、成功/失败/超时、重试数、工具轮次、视频是否可完整解码及独立质量评估。尽可能使用较高并发寻找瓶颈；`waiting`/defer 请求持续堆积或完成率低时，不能宣称优化完成。
- GPU 利用率 100% 只是一个采样指标。需要分辨实际 SM/Tensor Core/显存吞吐、prefill/decode、prefix cache、队列、NFS/CPU、容器调度和渲染后处理的限制。
- 不要通过缩短模型工作、删减必要视觉输入、压低输出长度、缩小上下文或减少 skill 要求来制造表面吞吐增益。优化应提高服务和生产链路本身的效率，模型该做的观察、渲染、修正步骤仍按原 skill 和任务难度执行。
- 记录完整视频时长和复杂度分层的实际 wall time。只有闭环结束、产物有效并通过预先说明的质量判断，才能计入“质量合格任务/小时”。服务请求数、token/s、API 200 和文件存在都不能代替完成任务数。

### 2.2 精度和质量验收

每项推理优化独立报告两种数值精度结果，不能混成一个模糊的“精度没问题”：

1. **逐 bit 一致**：在明确的真实 tensor scope、dtype、rank、stage 和位置范围内，用可信 worker hook 对同一原始 tensor bytes 做 hash。缺失 hook、只比较 API 文本/token、不同条件或只看十进制打印值，都不能称为内部逐 bit 一致。
2. **可接受噪声**：先在同一请求条件下重复原始镜像，候选只能落在该请求实际观察到的 baseline 噪声包络内。不能失败后临时放宽阈值；API 输出相近不能替代 raw tensor 数值比较。

逐 bit 与噪声结果分别报告。还要独立核验真实 Claude Code 闭环：任务自然结束、Three.js 可运行、模型完成 skill 的检查、白模视频存在且完整解码、代表帧/场景达到预定质量标准。某一项数值门禁通过，不意味着产物质量通过；反之亦然。必须说明每个结论实际覆盖的 scope，不扩大解释。

验收规范与工具入口见：

- `a800/codex_exp/docs/accuracy-gates.md`
- `a800/codex_exp/scripts/compare_real_accuracy.py`
- `a800/codex_exp/scripts/replay_real_api.py`
- `a800/codex_exp/scripts/run_real_logits_probe.py`
- `a800/codex_exp/scripts/merge_tensor_observer.py`
- `a800/codex_exp/docs/real-kv-path-probe.zh.md`

### 2.3 Transcript、API capture 与 Swift JSONL 的关系

- **训练数据只从 Claude Code transcript 转换。** API trace/proxy 用来审计顺序、请求/响应字段、工具 schema、是否漏消息和转换准确性；不能从 trace 补写或回填 Swift 训练样本。训练转换的核心主链为 `transcript_to_calls.py` → `calls_to_swift.py` → `e2e/prepare_megatron_dataset.py`。
- 代理需要在 Claude Code 到实际 API server（本地 vLLM 或云端 Claude Messages API）之间捕获所有 HTTP request/response body。先保留 raw bytes、长度和 hash，再于会话结束后将 Messages SSE 重建成普通 JSON，便于人工阅读审计。图片如果原请求本身用 Base64，就保留为 Base64；不要把整个 SSE/body 再 Base64 包装给审计者看。
- 此处记录的 A800 新 smoke 明确指向本地 vLLM：manifest 中 `model_name=deepseek-v4.1-flash`，base URL 是 `http://127.0.0.1:18105`，API key 配置为本地服务使用的 `EMPTY`。该 smoke 没有使用 Anthropic 云端 Opus API。接手新 run 时也要从 manifest/config 验证实际 endpoint，不能由“Claude Code”这个客户端名推断底层模型。
- 当前代理在 HTTP 应用层捕获，不是 TCP/TLS 原始包抓取：会调整 Host、hop-by-hop header、Content-Length 和 chunk framing；body 字节保留。完整流必须以协议终止标记核验；HTTP 200 不等于 SSE 完整。超时、取消、部分 response 和 server error 都保留并标注，不能当作完整成功 response。
- 不能把 transcript 说成 wire request 的无损副本。逐 request 的 system prompt、tool schema、模型/参数可能变化；Claude Code 会对某些工具输入做归一化。DeepSeek/V4.1 审计中曾出现 `tool_reference` 兼容占位、Bash 输入前缀在 transcript 中被剥离等差异。auto-compact 的原始 response envelope/ID/`<analysis>` 也没有完整写入 transcript；有些 summary 正文能在之后的 compact-summary user 内容中匹配到，但不等于保留了压缩前模型响应。
- `tool_reference` 是请求中出现的一个特殊内容 block/工具引用，不是普通文本工具返回。DeepSeek V4.1/vLLM 的 Messages parser 曾因只支持 text/image 而对它返回 400。之前有人提出 allowlist 作为兼容办法，但用户要求先把可读 raw request/response 和语义调查清楚；不能未审计就过滤、改写或加白名单。失败调用留在审计数据中，不能假装成成功训练样本。
- root、subagent、workflow 应按实际会话结构分开重建并保留 parent/时间关系；不能把 subagent 或 workflow 当无关噪声丢掉。附件提醒、Monitor event、RemoteTraceback、队列操作等内容，如果 transcript 里确实以模型可见的 rendered message/tool result 出现，就按其在模型上下文中的位置保留；只存在于 host/runtime metadata、模型未见的，留作 provenance/审计侧车，不伪造为模型对话。
- thinking 只保留 transcript/API 中实际可见的信息。空 thinking 加 opaque signature 不包含可训练的推理正文；不得凭签名猜测、编造或声称能够从 signature 恢复原始 CoT。训练导出保留真实可见的非空 thinking，但不要把 API trace 中才有的隐藏内容回填训练数据。
- 曾研究早期论文所述加密 thinking/signature 的解析思路，但本轮没有得到可复现、可用于当前 Claude Code transcript 的解密器，也没有从 signature 中恢复出原始推理。历史论文的方法与当前模型/API 版本的关系未证明；将它列为未解决的取证问题，不作为可以合法/技术上恢复训练数据的现成功能。
- 本地真实 `C:\Users\53046\.claude` 是只读源。此前只在副本上做了转换试验；后续排查仍需先拷贝并在副本中操作，不能覆盖源目录。
- `ms-swift` 的 Agent JSONL/Megatron 数据准备已搭建，执行转换时要保留图片资产和相对路径。1-step 小样本 smoke 不能证明全量/长上下文/全量权重训练通路有效；Qwen 训练需要在目标机器、目标长度和目标 recipe 单独做完整验证。

资料：`agent-scaffold-trajectory-export/docs/{transcript_to_calls.md,trajectory_methodology.md,api_capture.md,transcript_api_swift_correspondence.md,transcript_api_differences_deepseek_v41.md,deepseek_training_api_audit.md,deepseek_recipe_notes.md}`。

### 2.4 Claude Code 模式、上下文与 goal 写法

- Claude Code headless 要使用真正的 CLI `--effort ultracode` 和原生 goal 命令；单纯把 “ultracode” 写入 prompt 不等价。该版本 `-p` 路径需要通过 `claude -p "/goal $(cat goal.txt)"` 进入 `/goal`。
- 用户之后明确要求模型与 vLLM 都暂时按 Qwen 目标上下文设置 **262,144 tokens**，避免 DeepSeek 原生 1M 输入使 Qwen 训练分布过长；长任务可以通过自身原生 auto-compact 运行。auto-compact 阈值遵循 Claude Code 原生设置，不覆盖成 80%；早期 `200000 context / 80% / high effort` 是旧实验参数，不是后续目标。
- 当前版本 `--effort ultracode` 已在 A800 smoke 的 CLI/任务记录里使用。`ultracode` 的动态 workflow 行为属于 Claude Code 配置；DeepSeek 是否能经 vLLM 真正实现对应 `xhigh`/reasoning effort 转换，需要对照 DeepSeek recipe、Claude Code 发出的 API 字段和当前 vLLM fork 实测，不要凭参数名认为已经生效。
- goal 要短、独立、客观、通用：只写输入在哪、要交付什么、skill 要求的必要步骤与产物保存位置。不把与用户的对话、内部权衡或思考写入 goal；不预设化学或其他场景，不额外规定无关外观/材质，不强制每个任务 refine。保持 `media2threejs` skill 本身不变，先评估 skill 的步骤后再讨论是否改 skill。
- 用户的渲染取舍是：模型为了完成任务需要自己渲染并查看白模，避免把制作/验证过程做成对模型不可见的后台黑箱；同时可由任务结束后的统一 postprocessor 做规范对比、媒体完整性审计和结果汇总。后处理不是替模型进行视觉验收，也不把渲染错误闭环改成后台自动代劳。

目前新的 core goal 文本：`agent-scaffold-trajectory-export/e2e/tasks/media2threejs_goal_core_whitebox.txt`；中文比较说明：`docs/media2threejs_goal_comparison.zh.md`。

### 2.5 变更、资源与协作边界

- 优化只操作自己管理的目录；清理只允许在自己的实验/artifact 文件夹内做，保留对照、审计、复现必需的输入/日志/清单/hash。不能顺手清理其他团队、生产或用户目录。
- 在每项优化完成后及时 commit；本机 VPN 首选 `D:\DesktopProgram\XingchenVPN`，GitHub 不通时切换 `D:\DesktopProgram\Clash Verge`；若网络仍不通，先做好本地 commit/版本记录。实验前确认远端实际拉取的是已提交代码，并对关键脚本/patch 做双端 hash。
- 真实 API/Opus 调用昂贵时，先在现有本地 transcript/副本和离线样本上做研究，真实调用先用最小任务、设明确预算。
- 长任务要在用户指定的 tmux 会话/窗口运行，不能让后台子 agent 成为唯一执行与监控者；短间隔轮询关键进度。需要速度异常时按当时约定及时停止并重启下载，但阈值、最近进度、下载是否完成都必须从会话和日志核实，不能猜。
- 不触碰当前用户管理的生产任务。服务启动/停止要基于明确的容器 ID、GPU 占用和实验锁；一个 orchestrator 管一个任务批次。A800 当时用户明确授权杀掉抢占资源的容器做自己的实验，但该授权不自动转移到 H800 上的其他任务或其他人的容器。

### 2.6 前序任务脉络（说明为什么这些内容在同一交接中）

1. 最开始是审计 browser_agent evaluation 中 Claude Code 的巨大 workspace transcript 与 API trace，确认前者是否能独立恢复每次模型调用、工具 schema、user/system/assistant/tool 信息，并转换为 ms-swift 训练样本；API trace 只作审计。
2. 接着研究了 Docker headless Claude Code 2.1.310、自动 `ultracode` 与 subagent/workflow/auto-compact 的真实行为，尝试用 Qwen3.8-27B vLLM 造任务，并以 Qwen3.5-2B 探训练数据链路；训练格式与 Megatron SFT/YaRN 长上下文另需独立检验。
3. 用户随后把数据生成模型切换到 DeepSeek V4.1 Flash，要求从 Claude Code 到 vLLM/云端模型 API 的完整代理捕获；要求先保留原始请求/响应 bytes，之后重建人类可读 JSON。用户特别指出旧 `api_proxy.py` 有许多 bench patch 不适用，应按照 Anthropic Messages/streaming、OpenAI Responses API、vLLM 源码和 DeepSeek Recipe 的行为重新核实。
4. 发现 vLLM DeepSeek parser 对 `tool_reference` 报错后，用户明确要求先做 byte stream → 可读 JSON 的 request/response reconstruction，随后对 reconstructed payload 审计已由 transcript 得到的 Swift 数据，确认 transcript 与 wire trace 的交集和缺失边界；不要先启白名单绕过。
5. 工作重心再转向 A800 的 DeepSeek 推理和完整造数链路，尝试真实 Claude Code workload、内核/KV offload/调度/Docker/渲染优化，并要求逐 bit 和实测噪声两种精度判断；现在用户准备把研究交给 H800 继续，因此本文件只总结和列交接项，不再对当前 A800 服务做新测试。

之前用于 Qwen 的 8 卡 vLLM recipe（Rust frontend、tool/reasoning parser、multimodal encoder、YaRN rope）是 Qwen-specific 的配置研究；Qwen3.8 与 Qwen3.5 曾被认为是 256K 目标上下文。DeepSeek V4.1 自身原生长上下文与训练模型 target context 是不同话题；当前数据生成上下文限制按 Qwen 训练目标，不代表 DeepSeek 的模型上限。推理服务、Claude CLI context 和训练序列长度分别记录，不要将其中一个 max length 默认沿用到另外两个。

## 3. 工作区、机器和复现入口

### 3.1 本地两个仓库

1. 轨迹/任务生产仓库：`D:\openmaic-intern\agent-scaffold-trajectory-export`  
   负责 headless Claude Code runner、goal、API capture/reconstruction、transcript 转换、Swift JSONL、任务后处理。
2. 推理优化与实验仓库：`D:\openmaic-intern\deepseek-infra-opt`  
   负责 vLLM/DeepSeek baseline、candidate plans、真实 API replay、精度和容量测量、调度/Docker/系统指标。

### 3.2 A800 当前目录和运行快照（按 2026-10-30 11:18 北京时间附近读取）

- SSH：`maic_a800_80_2`
- 推理优化 repo：`/home/tsq/workspace/deepseek-infra-opt`（物理路径别名为 `/slow_share2/ultrafast_share/tsq/icw-bench/workspace/deepseek-infra-opt`）
- 任务 repo：`/home/tsq/workspace/agent-scaffold-trajectory-export`
- 模型路径（历史运行记录）：`/slow_share2/models/DeepSeek-V4.1-Flash-1017`
- baseline 服务：容器名 `dsv41_image_baseline`，端口 18105；镜像 digest `sha256:874392c05c07d53cdba5b380d67f386fd3d7f0abde21dbc36497ead87c47f483`。这是最近检查时仍运行的容器，启动参数/状态应在 H800 工作开始前重新取证，不能据此假定 A800 当前还可被停掉或配置仍不变。
- 新 Claude 任务镜像：digest `sha256:8fb0ee32275eaa64177b66b24c33dedd6072e735b063f7bbf92c0b73330d84bf`，标签/使用记录对应 Claude Code 2.1.310。
- H800 的旧文档曾使用 alias `cog_h800_80_1` 和路径 `/data0/gsy/workspace/3d-code-gen`；用户后续截图显示的终端主机名是 `skB-h800-CPT15`，模型目录是 `/share2/models`。请接手者自行确认**当前正确 SSH alias、挂载、repo 和实际下载状态**，不要沿用旧文档路径假设。

最初指定的历史 evaluation 数据根目录为 `/home/tsq/workspace/browser_agent/evaluation`：

```text
outputs/{interact|interact-review}/claude_code/{model-name}/workspace/{page-id}/
  .claude/projects/-home-node/{hash}.jsonl     # Claude Code transcript
outputs/{interact|interact-review}/claude_code/{model-name}/traces/{page-id}.jsonl
                                                # API trace，raw request/response 审计用
```

后续新任务产物所在仓库是 `/home/tsq/workspace/agent-scaffold-trajectory-export/e2e/3d-code-gen/`，不能把它和最初 evaluation 目录混为一谈。两个目录的 JSONL 都可能非常大；按 page/session、hash、时间、request ID 精确筛选，流式读取并只输出必要摘要。

### 3.3 当前本地版本状态

- `agent-scaffold-trajectory-export` 当前 HEAD：`553c773 Fix post-session render alignment and decoded artifact audits`；远端 `origin/main` 同步。此前相关提交：`c4d7754 Add core whitebox goal and postprocess audit`、`f46255f Invoke native goal command in media runner`。
- 该仓库有一个**未提交修改**：`e2e/tasks/postprocess_render_artifacts.py`（V3，新增 FFmpeg 线程上限和固定 bounded panel）。已做 py_compile、diff check 和 fixture 检查；它尚未纳入 `553c773`，也没有对 A800 最终 smoke 跑完完整后处理/解码。接手者先 `git diff` 审阅，不要把工作区内容误当成已推送版本。
- `deepseek-infra-opt` 当前 HEAD：`3c33d7e Prepare paired 16-video core goal workloads with pinned inputs and telemetry`，远端同步。工作树有两个未跟踪实验脚本：`a800/codex_exp/scripts/audit_compare_commands.py`、`a800/codex_exp/scripts/ffmpeg_cpu_matrix.py`；它们并非已提交的稳定接口。
- H800 迁移时先核对两个仓库分支和远端状态；路径里的 `/home/tsq`、`/slow_share2`、模型挂载和 A800 容器 ID 不可硬编码为 H800 配置。

用户曾提供 Qwen3.8-27B vLLM 长上下文 recipe，包含 Rust frontend、auto tool choice、Qwen Coder tool parser、Qwen reasoning parser、multimodal encoder/data 配置和 RoPE/YaRN `hf-overrides`，也讨论过 8 卡启动、多模态 limits 和 262,144 context；后续数据生成目标转到 DeepSeek V4.1 Flash，不能把 Qwen 的启动参数直接复制成 DeepSeek 服务命令。训练侧目标则是用 Qwen3.5 做 teacher-data/student-training 检验，并研究 YaRN/长上下文；应以当时用户给的 MS-SWIFT CLI 参数文档和 NVIDIA NeMo YaRN 指南，结合当前 H800 上的模型与版本重新验证。用户纠正过方法名称：应按 Megatron-SWIFT 文档做 Megatron SFT，不要把不存在的 “Swift Megatron SFT” 当作独立训练产品/命令。

## 4. Claude 之前完成的 A800 优化与历史证据

详细主文档在 `deepseek-infra-opt/a800/codex_exp/references/prior_3d_code_gen/docs/handoff-dsv41-pipeline.md`，配套 `docs/experiments/2026-10-27-dsv41-a800-serving-perf.md`、`docs/experiments/2026-10-27-dsv41-concurrency.md`、`docs/pitfalls.md`。这些是**当时 A800 的真实窗口/实验报告**，不是当前代码版本或 H800 的验收结果。后续用户已更新 goal、上下文和 effort 要求，旧配置与新要求冲突时，以本交接第 2 节后续约定为准。

### 4.1 Kernel 和 vLLM 参数

- 旧工作尝试了 SM80 `combine_topk_swa`、candidate indexer、custom all-reduce 192 MB、CUDA graph capture 到 768，以及若干 fork 默认关闭的性能开关。
- 旧报告称 `combine_topk_swa` 随机张量测试 600/600 bit exact，配对 step-period 测试约 -9.9% step time；candidate indexer 报告约 -7.5%，当时漂移被判在其测试噪声内。这些结果覆盖范围和测量方法见旧报告，尚未自动满足当前 `accuracy-gates.md` 的真实请求 worker-tensor 门禁和闭环质量门。
- `customAR192` 是 Claude 已经做过的东西，不能作为本轮“新优化/创新”宣传。早期有过很小矩阵的结果，但后来的 readiness 检查指出新 V2 observer 不兼容、没有启动真实 GPU/API；必须把它列为历史候选，再按 H800 实际 kernel dispatch、真实请求、精度和质量重测，不能直接信旧名义性能。
- candidate indexer 的 top-k 集合相同但索引数组顺序不同；它需要针对下游是否依赖索引顺序做 code-path 检查和真实调用验证，不能仅凭 set equality 放行。

### 4.2 KV offload

- Claude 曾试过 vLLM 默认 `OffloadingConnector`，在 V4.1 hybrid KV group（特别是 8-token scratch group 与 32-token hash block）初始化/缓存路径不兼容。
- 之后采用 `SimpleCPUOffloadConnector` 加 `0004-simple-kv-offload-skip-scratch-groups.patch`；历史日志确实观察到约 52,608-token 前缀来自 host tier 的 hit。旧 A800 并发表里 96 agents 曾达到约 58.3 turns/min、749 output tok/s；160 agents 变慢并堆队列，因此当时 96 agents 是旧配置下的工作点。
- 旧 `16.4 items/h` 数据同时包括多个 patch、offload、性能开关和不同 pipeline/goal；不能单独归因到 KV offload。新基线窗口曾见 external KV transfer 0，说明当前运行未证明 offload 有效。
- 在 H800 重新核对目前 vLLM 分支是否支持、每种 KV group 是否被处理、是否实际发生 CPU tier load。必须 request-bound 证明：同一真实请求在 CPU-hit 路径上命中，按当前精度协议完成 bit/noise 验收，并且全任务效果/吞吐可接受。

### 4.3 旧吞吐/调度/Docker

Claude 的旧报告记录了若干 A800 窗口：stock 32 agents 约 9.1 items/h；一组 patches+性能参数+KV tier、96 agents 的窗口约 16.4 items/h；实验期间频繁重启的窗口约 7.5 items/h。它们跨时段、代码版本、warm/cold/cache 和测量阶段，不能作严格因果 A/B，也不等价于质量合格任务/小时。

旧 orchestrator 支持降并发时停止一部分较新任务、排队后用 `claude --continue` 续跑，并实现按 prefix-cache/队列调节并发的 AIMD 想法。旧 Docker 调查涉及 bridge 端口上限、inotify 和频繁 `docker top` 采样成本；旧真实观察认为模型 API 等待是主要瓶颈，低并发时 container CPU 使用并不高。这些只说明值得在 H800 单独采 CPU/cgroup 数据，不能得出 “换 K8s 就更快” 或 “Docker 一定是瓶颈”。

### 4.4 Claude 的未完成/待 H800 重试候选

旧报告里列出的候选包括 DeepSeek FP4 KV/DeepInfer 格式、DSpark adaptive verification、YOCO lossless prefill truncation、NUMA、DP attention/EP、custom all-reduce 和调度准入控制。不同项目旧状态有的离线验证、有的启动失败、有的未完成，有的仅属设计，不可统称“已做”。优先阅读：

- `a800/codex_exp/docs/next-candidate-audit-20261029.zh.md`
- `a800/codex_exp/docs/real-workload-method-matrix.md`
- `a800/codex_exp/docs/fork-method-eligibility-20261029.md`
- `a800/codex_exp/docs/experiment-2026-10-29-clean-vs-sm80.md`
- `a800/codex_exp/docs/experiment-2026-10-29-yoco-parity.md`
- `a800/codex_exp/docs/orchestration-investigation.zh.md`

H800 可优先重新检查 Hopper 特有适用性，但要先 source-audit 当前实现与真实 dispatch。DSpark adaptive verification 在 A800 曾被 attention CUDA graph 支持挡住；H800 可能具备不同路径，但并未等于已验证。FP4 KV 依赖当前 H800 backend/模型格式真实实现；YOCO 路径要证明 cache-hit 的 token/worker 行为；NUMA 与容器 CPU 需要控制变量。不要从 DeepSeek repo/论文写了某方法就推定本镜像已启用。

## 5. 本轮已完成的工作与结果

### 5.1 Transcript-only 转换和可读 API 审计

在 trajectory 仓库里已建成并有文档的主链是：

```text
Claude Code transcript ──> transcript_to_calls.py ──> calls_to_swift.py ──> prepare_megatron_dataset.py
               │
               └── 与 API proxy reconstruction 对照审计，不回填训练样本
```

API proxy/raw capture 和重建工具：`e2e/api_proxy.py`、`rebuild_wire_jsonl.py`，审计工具 `audit_calls_vs_trace.py`、`audit_training_vs_api.py`。DeepSeek V4.1 的特定会话样本做过 transcript vs reconstructed trace 对照：审计涵盖 223 个 HTTP exchanges，208 个 transcript calls 中有 204 个 matched wire requests；这些 matched 项在 system/messages/tools/params 上逐项一致。剩余差异/未匹配请求需要以 `docs/deepseek_training_api_audit.md` 的细项为准，不能外推为所有模型版本/所有任务 100% 无损。

审计确认的问题包括：`tool_reference` 请求被 DeepSeek vLLM parser 拒绝或兼容处理；Bash 工具输入可能被 Claude Code 归一化；逐请求 system/tool schema 会变化；auto-compact 原始 response 不完整写入 transcript。Swift 的每条 row 代表一次模型调用时，使用当时可恢复的 context 与当前 assistant target；对话链 merge 要谨慎保留每次 API call 目标边界。

真实本机 `.claude` 副本试转保留了 root/subagent/workflow 分开的行与 attachment/queue 相关结构；审计只在副本操作，真实原目录没改。此前统计是 12 个独立 row、494 attachments（300 有 rendered text，194 只有 metadata）、146 queue lifecycle、267 个 task notification payload。不是所有 metadata 都是模型看到的内容：用 transcript 中 rendered 内容判断可见性。

MS-SWIFT 4.5.3 + Qwen3.5-2B 做过 1-step LoRA smoke，loss 1.383；它只证明短样本训练命令能启动，不证明全量训练、全量权重更新、超长上下文或 Megatron-SWIFT 配置已完成。历史长行有约 96.9k 和 131k token 的编码结果。用户选定文档化的 Megatron SFT 方向，并要求按 `ms-swift` 的 YaRN 参数文档和 NVIDIA NeMo long-context 指南研究；实际 1M 训练稳定性和训练设置尚未验证。

### 5.2 简洁 goal 和真实任务 smoke

新增 `e2e/tasks/media2threejs_goal_core_whitebox.txt` 和对应 core config/hook；runner 改为真正调用 `/goal`，并显式 `--effort ultracode`。目标文本按用户反馈保持通用，不锁场景/材质、不复述对话、不强制 refine；要求依据 skill 和任务，交付编辑场景、白模视频，并由模型自己按 skill 渲染和查看。post-session comparison 是审计辅助。

一次 10 秒真实 clip 的原生 goal smoke 最终在 resume 后成功：

- 视频 ID：`BV1QW4y1v7iU`，原视频 52.522 秒，但这次 smoke 只取 `clip_start=18.38` 的 10 秒片段。
- Claude Code 2.1.310 image digest：`sha256:8fb0ee32275eaa64177b66b24c33dedd6072e735b063f7bbf92c0b73330d84bf`；model endpoint 指向当时 A800 port 18105；goal 模式、`effort=ultracode`、context 262144，未在 smoke config 里覆盖 auto-compact 百分比。
- 第一次容器运行约 5,421.9 秒后达到 timeout，无产物；保留 workspace/transcript 后续跑继续，第二段约 2,139.7 秒、75 tool calls/78 turns 后 `success`。两段实际运行时间合计约 2.10 小时，不含中间等待时间。第一次的失败和续跑必须一起报告，不能只看最终成功。
- `batch_log.jsonl` 报告 `deliverables=["whitebox.mp4"]`，文件系统里能找到 `whitebox.mp4`（88,056 bytes）、`compare.mp4`（798,994 bytes）、`compare.jpg`（11,425 bytes）。该 run 统计的 `whitebox_s=10.034`，`compare_s` 为空。
- 当时宿主机找不到 `ffmpeg/ffprobe`，所以尚未独立验证 MP4 完整解码、duration/fps/aspect 和中间帧内容；文件存在和 runner success 不是质量通过。也没有独立审核视频是否准确复刻、是否完成该 skill 的 review 证据。
- transcript 内可见一个重要修复：第一次停在产物路径/文件布局缺失，续跑把 scene source 改到 harness 检查的位置后重新 build/render。故该 smoke 同时暴露了 goal/harness 产物路径 contract 需要测试。

远端 artifact 根目录：`/home/tsq/workspace/deepseek-infra-opt/a800/codex_exp/artifacts/real_tasks/core_whitebox_goal_smoke_20261030/`；任务输出在 `/home/tsq/workspace/agent-scaffold-trajectory-export/e2e/3d-code-gen/claude_code/deepseek-v4.1-flash/items/BV1QW4y1v7iU/results/BV1QW4y1v7iU/`。需在接手后用有 ffmpeg 的容器/环境完成解码审计。

### 5.3 新 goal 的 16 条配对实验准备（没有运行）

`deepseek-infra-opt/a800/codex_exp/experiments/core_goal_c16_20261030/` 已准备 16 个完整视频输入，并配成 baseline control 与仅加入 `worker_combine` 的候选臂。配置固定输入 hash、模型和任务镜像、native goal、ultracode、262144 context、原生 auto-compact、并发 16、每 task 6 CPU、长 timeout，并准备 system/cgroup/hardware 采样。来源是冻结 cohort，视频时长约 48.299–1242.71 秒，总时长约 3849.654 秒。

实验启动脚本会在已有任务容器时拒绝开跑，不主动停服务。远端 control 预检通过，但由于原生 smoke 正在执行，该配对 arm **没有启动/没有结果**。此外候选 `worker_combine` 配置当前只证明计划安装，不足以证明该 kernel 在真实请求中实际 dispatch；需要单独拿到适当的 dispatch 证据并审查单次变量设计。control 暖机、candidate 冷启动的顺序也会引入缓存/重启 confound；必须在报告中说明，最好设计 ABBA/重复或同一 phase 的控制。

### 5.4 A800 旧 8 视频基线和机器效率数据

有限的 `image_baseline_8_duration_c8_20261029` 数据：8/8 runner 状态成功，批次从 14:30:11Z 到 17:43:00Z，历时约 3.214 小时，观察吞吐约 **2.49 items/h**，平均单任务 wall time 约 1.835 小时。该 batch 是旧 goal、`high` effort、旧 `200000/80%` auto-compact 设置，不是当前新 goal/ultracode/原生 compact 配置；没有独立 quality review。因此 2.49/h 是旧批次吞吐观察值，不是合格任务容量预测。

这批运行约有 1,234 次完整成功 Messages exchanges；服务约 85.7 output tok/s、394 requests/h、约 91.4M prompt token、prefix cache hit 81.4%，external KV transfer 0。8 卡 `nvidia-smi` API 活跃窗口均值 GPU utilization 约 92%，median 99%，GPU memory active 约 22.1%，平均功耗约 234/400W；空闲/API 间隔 GPU utilization 约 20%。这些粗采样不能推出 Tensor Core、显存带宽是否充分，也不能直接换算任务/h。`dcgm` 不可用，且当时没有 system/cgroup 采样，不能归因 CPU/Docker throttling。

### 5.5 后处理/FFmpeg CPU 实验

本地 postprocessor V3 在 V2 基础上，为 FFmpeg 阶段加线程上限（默认 2）和 bounded comparison panel，避免在 cgroup 限制 24 CPU、但宿主机可见 128 CPU 时 FFmpeg 自动探测后创建过量线程。fixture/代码检查已完成，但 V3 仍未提交，也未在本次 smoke 产物上完成真实重跑验收。

managed A800 做过初步 CPU matrix：一条 5,135 帧、约 171 秒的真实白模整段，用全片 all2 配置约 47.3 秒产出 1920×540 比较视频，约 3.63x realtime，完整文件 decode 在当时的容器侧记录为有效。若干 150 帧配置在暖缓存后是亚秒到约 2 秒；第一组默认配置约 164 秒，存在明显冷/warm 与执行顺序混杂。全分辨率试验有超过 180 秒 timeout。结论只能是线程/分辨率设置值得 A/B，不足以证明 production bottleneck 已定位或 V3 已加速。

### 5.6 真实负载之前的优化准备/候选

- `worker_combine` 是下一项准备单独测的候选之一；旧的单点合成矩阵约 48.0→51.2 tok/s（+6.7%），600/600 kernel 单测 bit exact，但规模小且不是完整 Claude Code workload。不得写成生产提升。
- `customAR192` 是历史 Claude 候选，不算新发现；当前真实闭环和新精度门禁尚未完成。
- `Engram` prefetch 有 CPU side prototype/test，但未部署/未在 GPU API 服务和真实负载下通过。
- KV offload current path 尚未 request-bound 证明；`probe_real_kv_paths.py` 是计划/工具，不等同测试结果。
- ForgeInfer 相关工作是适配代码和 CPU mock tests。它复用了持久队列、GPU lease、detached runner 的原语，没有导入其 Qwen/SGLang benchmark 成绩，没有真实 campaign 派发、GPU precision、质量或 throughput 结果。
- 调度和容器工具已准备若干采样/运行入口，但还没有足够 cgroup/system 时间序列证明容器 CPU、launch storm、FFmpeg 是否限制任务吞吐。

## 6. H800 接手执行清单

建议接手者将每项记录到实验日志，保留命令、配置、镜像/源码/model SHA、任务 cohort SHA、时间段、操作者、失败与恢复情况。确认首轮成功后再做下一阶段。

### A. 接手前先核实环境

- [ ] 核对 H800 SSH alias、机器 GPU/PCIe/NVLink/NUMA、driver/CUDA/NCCL、容器 runtime、可用 CPU/内存/共享内存和模型挂载；不要把 A800 环境变量或路径复制过去。
- [ ] 核对 DeepSeek V4.1 Flash 权重是否完整落盘、snapshot/权重 manifest/hash、tokenizer/template 和模型 revision；确认 Hugging Face 下载是否完成。用户曾要求在 tmux 里持续盯下载、速度骤降时按阈值及时重启；实际阈值和最终完成状态要查原 tmux/session/log，不能从旧 screenshot 推断。若下载 token 曾出现在命令行/终端回显，核实是否已轮换，不要复用或写进脚本。
- [ ] 找到 H800 的真实生产数据目录和任务创建入口，确认只读原始视频/生产数据，以独立 managed output/artifact 目录做实验。不能清理/覆盖其他人的数据。
- [ ] 核对 `media2threejs` skill 的实际版本、入口、状态图、render command、产物路径、需要的 evidence/review。用户要求原生 skill，不改 skill scope。
- [ ] 核实 Claude Code 镜像 `2.1.310` 或更新版本的实际 digest、`--effort ultracode` 是否被 CLI 接收、API request 是否含期望 reasoning 字段、Claude Code workflow/agent 是否真实启用、上下文 262144 和 auto-compact 原生值。不要把 high/200k/80% 的旧配置沿用为当前标准。
- [ ] 核对 API proxy 捕获链是否处在所有 Claude→API 流量必经路径，能否同时覆盖 Messages API 和未来的其他 API dialect；保留完整 request/response、partial/error，并测 proxy 自身不会改写 body。
- [ ] 读取 `docs/pitfalls.md`、当前 accuracy gates、native profile/capacity、quality 与 orchestration 文档后再启动 GPU 实验。

### B. 建立干净 baseline 和容量测量

- [ ] 冻结 H800 原始镜像 digest 和原始启动参数，作为 immutable baseline。记录 GPU 数、TP/DP、max-model-len、quantization/KV dtype、speculative decoding、max batch/token、GPU memory fraction、prefix caching、environment variables、vLLM fork/revision、所有实际 patches hash。
- [ ] 用 1–2 个固定短任务做 smoke，确认模型 API、Claude Code `/goal`、transcript、raw proxy capture、Swift conversion、Four artifact paths、task stop condition 正常。先解码输出，避免把未闭合媒体当成成功。
- [ ] 冻结一组具有视频时长/复杂度分层的真实任务，完整视频作为 workload，不截短用于正式 capacity 结论。独立记录 clip 时长、分辨率/帧率、视觉复杂度分层和完整输入 hash。
- [ ] 用并发梯度实测（可从 8/16/32/64/96 等开始，按显存、队列和请求延迟动态设置安全步进），每个并发点跑足够时间和自然完成样本。记录 `running/waiting/defer` 时间序列、API 吞吐、prefill/decode、prefix/external KV 命中、任务开始/结束/重试、视频 wall time、host CPU/memory、container cgroup quota/throttle/cpuset、FFmpeg threads、GPU hardware counters。
- [ ] 用重复同 cohort、改变顺序或 ABBA 方式控制 warm/cold、服务 restart 和 NFS 页面缓存；明确稳定态开始/结束、run-in/warm-up 和窗口边界。没有足够自然结束样本就标右删失，不计算合格任务/h。
- [ ] 设定 `quality_pass / invalid_artifact / failed / timeout / interrupted / censored` 独立 outcome；质量通过数/实际墙钟时长才是有效任务/h。保留原始 request logs、transcript、完整任务 workspace、视频和审计 report 的 hash。

### C. 一次只做一个优化

1. **Source/dispatch 检查**：目标 patch 来自论文或 DeepSeek 仓库时，先核对当前 H800 vLLM 的实现、依赖、架构分支、kernel launch 条件和真实 shape 范围；静态代码存在不代表服务路径已使用。
2. **离线微测**：能用 CPU/unit/replay 抓参数、shape、hash 的先做；可复现错误应先离线定位，避免为一个 Python/parser 错误重启多卡服务。
3. **精度诊断**：对冻结真实 API requests 做原始 request replay。baseline 至少 3 次重复建立噪声；bit 与 raw tensor noise 分别跑；API response 仅作额外可观测证据。处理 batch、cache path、seed、图像/多模态 token、sampling、spec decode 的条件一致性。
4. **闭环质量与性能**：候选只改变一项，使用相同真实任务 cohort 和 Claude Code goal。检查自然终点、tool/reference error、文件结构、Three.js build/browser、模型自行渲染/检查的 transcript 证据、白模完整 decode、对比图、独立质量评审和资源时间序列。
5. **接受/回退**：两种精度、质量和性能目标分别报结论；任何门不通过或证据 insufficient 就保持为 candidate，不混入 baseline。性能 gain 不能自动抵消质量降低或排队恶化。完成一项后再提交代码、推送并核对 H800 revision/hash。

优先检查项（按 H800 实际支持程度排，而非宣称既定收益）：

- H800 Hopper 路径是否真正启用、基准 SM80 hot patch 是否适用/是否被原生 Hopper kernel 取代；包括 `combine_topk_swa`/indexer 真实分发与索引语义。
- DeepSeek FP4 KV/DeepInfer storage/dequant path 是否被当前 backend 原生支持，及长 context/多模态 cache-hit 精度、显存、host transfer 和任务 quality。不可凭 `--kv-cache-dtype` 名字或模型说明推定工作。
- DSpark speculative/adaptive verification 在该 H800 attention graph/backend 上是否可用，真实 Claude Code 大上下文下 acceptance/吞吐/队列和输出质量如何。
- Simple CPU KV tier/YOCO prefill reclaim：只在能证实目标请求实际发生 external CPU load、命中结构和对应精度时进入候选；避免在共享服务靠全局 counter 猜归属。
- Host NUMA/CPU affinity、Docker quota/cpuset、worker processes、video rendering concurrency、NFS I/O 的真实影响。先测，再决定是否需要 host scheduler、Docker 参数或 Kubernetes；K8s 是待比较的部署选项，不是先验提速方案。
- 任务 admission/back-pressure 可结合实时 waiting/defer、prefix hit、KV pressure 和目标质量通过率优化，但要记录停止/续跑是否改变 Claude Code transcript/compact/agent 生命周期，不得丢轨迹。
- 新 goal 与原 goal 用成对 cohort 比较完成耗时、step coverage、白模交付率和独立质量；不能只比较模型调用数，也不能把 10 秒 smoke 当全视频数据。

### D. 报告与交付

- [ ] 报告实际并发曲线、自然完成/质量通过件每小时、P50/P90 wall time，按视频时长和复杂度拆分；列明观察时长和删失任务。
- [ ] 单独给 vLLM requests/token/s、prefix/external cache、waiting/defer、GPU counters、CPU/cgroup、NFS、render/postprocess 时间，不混同为任务完成率。
- [ ] 每项优化用表格列 candidate、原始 baseline、代码/hash、实际 dispatch、bit scope/result、noise scope/result、quality result、性能结果、主要 caveat。
- [ ] 训练数据报告 transcript 转换 row 数、source transcript hash、附件/sidecar 缺失和错误、Swift schema 校验、API audit 覆盖率；清晰标注 trace 仅为审计。
- [ ] 对外呈现“已验证”“部分验证”“候选”“失败/不可行”“尚未测试”五类状态；不要把论文结果、repo 配置、CPU mock 或单点 microbench 混写为 H800 生产收益。
- [ ] 每个实质改动尽快 commit/push，记录 H800 实验的确切 commit/digest。完成交接后保留原始对照和必要审计文件。

## 7. 建议首先阅读的资料（按顺序）

1. `deepseek-infra-opt/a800/codex_exp/docs/accuracy-gates.md` — 两类精度门禁和实际比较范围。
2. `deepseek-infra-opt/a800/codex_exp/docs/bottleneck-initial.zh.md`、`docs/duration-complexity-survival-quality-20261029.zh.md` — 初始瓶颈、删失与质量/时长测量口径。
3. `deepseek-infra-opt/a800/codex_exp/docs/image-baseline-four-hour-20261029.zh.md`、`docs/pipeline-capacity.zh.md` — 旧生产窗口和完成率不可识别/不可外推的证据。
4. `deepseek-infra-opt/a800/codex_exp/experiments/core_goal_c16_20261030/README.zh.md` — 已准备但未跑的 16 任务原生 goal 对照实验；重点看验证限制和候选 dispatch 证据。
5. `deepseek-infra-opt/a800/codex_exp/docs/forgeinfer-adaptation.zh.md`、`pipeline/docs/docker-pipeline-ablation.zh.md`、`pipeline/docs/k8s-control-plane.zh.md` — 调度/容器研究的代码状态与限制。
6. `deepseek-infra-opt/a800/codex_exp/references/prior_3d_code_gen/docs/handoff-dsv41-pipeline.md` 与 `docs/pitfalls.md` — Claude 的 A800 历史尝试及踩坑。所有历史数字按报告口径解读，不当成 H800 结论。
7. `agent-scaffold-trajectory-export/docs/transcript_to_calls.md`、`docs/api_capture.md`、`docs/deepseek_training_api_audit.md`、`docs/trajectory_methodology.md` — 数据生产转换和审计真相。
8. `agent-scaffold-trajectory-export/docs/media2threejs_goal_comparison.zh.md` 和 `e2e/tasks/media2threejs_goal_core_whitebox.txt` — 新 goal 目的/内容；用户要求保持简洁、通用、skill-native。

## 8. 当前遗留事项（不要漏掉）

- A800 native goal smoke：runner 标成功且白模/compare 文件存在，但尚未独立完整解码、视觉检查、skill review quality audit；先完成该审计或把结果明确记作未验证，不要拿它申请合格任务产能。
- V3 FFmpeg postprocessor 仍是 trajectory repo 的未提交修改；确认是否采纳、测试和 commit。两个 FFmpeg matrix 脚本在 infra repo 里是未跟踪文件；决定要保留就检查代码、复现和 commit，不要默认进入 H800 production。
- 16 条 native-goal control/candidate arm 已准备但未运行；先复核冻结输入、goal、auto-compact 和资源占用，再看是否适合作为 H800 workload 参考。原配置的 control/candidate 顺序有缓存 confound，candidate dispatch 需要实证。
- H800 下载监控的当前 tmux/session、model 完整性、download checkpoint、速度中断/恢复历史、已运行容量都未由本交接确认。先查询状态，不要用可能存在的已完成文件名推定下载完成。
- 旧 A800 8-task 测试没有 quality score，旧 Claude 9.1/16.4 items/h 窗口也不是新的 goal/精度/闭环门禁。所有这些作为历史对照而非新目标基线。
- 训练方面：full-data Megatron-SFT、超长 context/YaRN、完整图像资产和代表性 1M token batch 训练尚未被当前 smoke 证明；先在 H800 明确环境/显存/sequence packing/position extension，再按完整数据 schema 做 dry-run/极小 step。
- thinking signature/加密内容没有实现可靠解析；不要把这项列为已完成工作或将 opaque signature 伪造成 reasoning text。
- `tool_reference` 的语义保真路径尚未敲定。下一步先使用安全的最小 request 复现其生成条件、schema 和服务拒绝机制，再评估原样保留/上游支持/兼容编码的影响；在确认前不使用静默白名单过滤。
- 确认两个本地 repo 的未提交状态、不要覆盖任何他人文件；完成接手后将具体 H800 的 baseline、模型、task cohort、精度和质量报告放进专属 experiment directory 并按用户要求及时版本管理。

## 9. 交接完成定义

接手者开始 H800 工作前，应先把本文作为入口；明确真实 SSH/路径并确认下载及资源状态；成功跑通一条 native `/goal` 全链路；建好可复现原始 baseline 与质量/系统指标采集；再用同一真实任务集合跑基线容量曲线。之后逐项评估候选。每个性能数字都能回到具体 run manifest、日志、输入 hash、镜像/源码 hash 和质量/精度报告，每个未测问题都留在清单上，不从“代码已写/容器已起/指标很高”跳到“优化已成功”。

