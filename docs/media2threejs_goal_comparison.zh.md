# media2threejs goal 变更说明

本文比较 `e2e/tasks/media2threejs_goal.txt`（旧 goal）和
`e2e/tasks/media2threejs_goal_core_whitebox.txt`（新 goal）。新 goal 不修改
`media2threejs` skill；它只调整评测 runner 对会话结束条件的额外要求。

## 为什么要调整

skill 的硬要求是读取并遵循 video-scene 状态图，完成空间事实、布局/相机、
时间线和 review 证据，保持可编辑的 Three.js 源码，完成 build/typecheck 和
无浏览器运行错误的检查，并根据 review 证据选择继续、修正或停止。skill 没有
规定结果目录必须叫 `whitebox.mp4`、`compare.mp4`、`compare.jpg` 和
`README.md`，也没有要求每个任务都做一次无依据的 refine。

旧 goal 另外增加了四项固定文件门槛，并要求“先把所有产物以 blockout 形式
放齐，再决定是否提高 fidelity”。旧 Stop hook 实际只检查文件大小，没有检查
视频是否可解码、帧数/时长/宽高比、浏览器 console 或画面是否有效。因此，
模型可能把时间花在比较视频和 README 的格式上，或者已经有 scene 却在最终
全片渲染阶段超时。

历史记录支持这个判断：旧 106/116 批次中 79 个有白模视频、34 个超时、3 个
没有视频；成功样本平均约 85.7 分钟 wall time。失败记录中常见的是 scene 已经
存在，但还没有完成 Playwright/SwiftShader、逐帧编码和 ffprobe。最近的成功
任务也证明模型能够自己调用 render 脚本、ffmpeg 和 ffprobe，查看中间结果并
按实际缺陷修正；这部分行为属于训练所需的轨迹，不能删掉。

## 要求对照

| 项目 | 旧 goal | 新 goal |
| --- | --- | --- |
| skill 状态图、空间/相机/运动证据 | 要求 | 保留 |
| Three.js 源码和 build/browser 检查 | 要求 | 保留 |
| 模型自己执行 headless Chromium/Playwright 渲染 | 要求 | 保留 |
| `results/whitebox.mp4` | 必须 | 仍是模型会话的核心产物 |
| 白模时长和宽高比 | goal 中要求 ffprobe 展示 | 保留；模型应在可用证据下检查 |
| `compare.mp4`、`compare.jpg` | 会话结束前的硬门槛 | 会话后审计后处理；不占用模型会话的完成预算 |
| `README.md` | 会话结束前的硬门槛 | 缺失时，后处理根据 item 和实际测量生成简短说明；模型已有 README 保留，模型的解释仍在 transcript |
| refine | 先完成全部 blockout 后可随意 refine | 由 skill review 和具体任务证据决定；有明确缺陷才修正，不做固定轮数 |
| Stop hook | 四个文件大小门槛 | 核心 hook 检查白模视频和 scene 源码；比较文件由 postprocess 生成 |
| 训练数据 | 含全部会话和四个交付物状态 | 含完整 transcript、模型的 render/inspect/review/repair 事件；审计文件作为 sidecar |

## 新 goal 的实际边界

新 goal 仍然要求模型自己完成可见的学习闭环：读取 skill，建立 scene，执行
headless 渲染，查看白模输出，并在 skill 的 review 状态或任务证据指出问题时
修正后重新渲染。它没有把渲染变成模型看不见的后台黑箱，也没有把 compare 或
README 从数据集中删除；只是把二者从模型会话的硬停止条件移到统一的会话后
审计步骤。

新 goal 不要求模型创建额外的 `render_job`、队列或 manifest。会话后验证由
独立 postprocessor 执行，不改变模型 prompt；模型已有的
渲染命令和观察结果仍以 transcript 为准。这样不会预先假设所有任务都需要
refine，也不会把后台渲染的成功误当成模型完成了观察和修正。

## 后处理与审计

`e2e/tasks/postprocess_render_artifacts.py` 在会话结束后读取 batch log；同一 item
出现多次时只处理最新记录。它不渲染 Three.js，不改 `whitebox.mp4`，不调用模型，
也不创建提交队列。默认使用 `--image` 指定的任务镜像，先解析成不可变 image ID；
报告同时记录仓库 digest（若本地镜像有）、ffmpeg/ffprobe 版本和执行 argv。
命令直接经 `subprocess.run(argv)` 执行，不经过 bash 或 shell 字符串。

参考来源和时间对齐如下：

1. 优先读取**同一次运行**的 `workspace/<id>/project/input/<id>.mp4`，即模型实际
   看到的片段；此时不再对它应用 `clip_start`。若文件存在但损坏，报告失败，
   不静默改用另一份视频。
2. input clip 缺失时，才读取 item 的源 `video`，使用 `clip_start/clip_seconds`
   按 runner 相同的裁剪参数在 results 临时目录重建片段；不会直接拿完整源视频
   与短白模拼接。报告记录原文件和实际裁剪片段的 SHA-256。
3. 两路视频从零时间起点对齐，使用白模的平均帧率生成恒定帧率对比视频；按显示
   宽高比处理 sample aspect ratio，并统一成方形像素。参考画面等比缩放、留黑边，
   不强行拉伸。输出为参考在左、白模在右的无声 hstack。
4. 对比时长取两路的较短时长，避免 framesync 重复一侧的末帧来伪造覆盖。报告明确
   记录两路时长差和各自未包含的尾部；时长差超过 1 秒或显示宽高比差超过 1% 时为
   `needs_review`，不会用拼接成功掩盖原白模不匹配。
5. `compare.jpg` 取生成视频的 `floor(decoded_frame_count / 2)` 帧，同时记录零起始
   帧号和时间。旧版的 `-sseof -1` 是从末尾倒数一秒，已移除。

脚本实际运行 ffprobe 记录参考片段、白模和 compare 的时长、帧率、尺寸、像素
宽高比与解码帧数；再以 `ffmpeg -xerror ... -f null -` 完整解码校验。JPEG 也检查
尺寸并解码。只有生成、探测和解码成功的 compare/JPEG 才从临时文件发布到结果目录。
文件大小只是审计信息，不是有效性判断。状态含义为：

| 状态 | 含义 |
| --- | --- |
| `ok` | 编码/解码检查通过，白模与参考的时长和显示宽高比在上述容差内 |
| `needs_review` | 比较文件生成有效，但白模时长或显示宽高比不符合参考 |
| `failed` | 输入缺失/无效、命令失败、超时，或输出校验失败；报告保留错误及命令信息 |

这仍不是场景保真度、白模材质、黑帧合理性或 browser console 的质量评审；
`ok` 不能替代模型的观察和 skill review。后处理只写 item 的 managed results 目录，
不回写 transcript 或 batch log 的会话状态。原模型 README 保留；缺少时只生成
关于来源、实测参数和报告位置的简短 README，不伪造模型的解释或质量结论。
已有 compare 只有在旧报告成功、输入/工具版本指纹一致、hash 相同且重新解码通过时
才复用；否则重建。`--force` 强制重建，白模始终只读。

服务器用法：

```bash
python3 e2e/tasks/postprocess_render_artifacts.py \
  --batch-log /path/to/model/batch_log.jsonl --image agent-scaffold-media2threejs:lean
```

`--local` 可替代 `--image`，使用 PATH 中的 ffmpeg/ffprobe。2026-10-30 已用本机
真实 ffmpeg 短 fixture 验证：30 fps、SAR 4:3 的 300×200 源片段与 15 fps、200×100
白模对齐；源视频第 1–5 秒裁剪；优先使用另一个同运行 input clip；不同宽高比/
时长标为 `needs_review`；非空损坏白模拒绝；损坏 compare 重建；相同输入 compare
复用。六色片段的中间 JPEG 为黄色、末尾为品红，像素检查证明中间帧没有误取末尾。
相同的裁剪/对齐 fixture 也通过了本机 `lwm-render:latest` 容器中的 ffmpeg 5.1.9；
这验证了直接 Docker argv 和挂载路径，尚不代表已在 A800 任务镜像跑过整个数据集。

## 对训练数据的影响

转换脚本仍然只从 Claude transcript 重建消息序列；API trace 只用于核查顺序、
schema 和遗漏。新 goal 不会丢掉模型调用 render、查看图片、ffprobe、review
和 repair 的事件。`compare.mp4`、`compare.jpg`、README 和 postprocess report
可由后续数据整理作为文件引用或 sidecar 加入样本元数据，不伪装成模型消息；
本脚本不修改训练 JSONL。这样既保留模型
学习“自己渲染并检查”的行为，也避免让格式化审计文件控制一次长会话是否被判
定为成功。

