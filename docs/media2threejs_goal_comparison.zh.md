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
| `README.md` | 会话结束前的硬门槛 | 会话后根据 item、轨迹和 ffprobe 报告生成；模型的解释仍保留在 transcript |
| refine | 先完成全部 blockout 后可随意 refine | 由 skill review 和具体任务证据决定；有明确缺陷才修正，不做固定轮数 |
| Stop hook | 四个文件大小门槛 | 核心 hook 检查白模视频和 scene 源码；比较文件由 postprocess 生成 |
| 训练数据 | 含全部会话和四个交付物状态 | 含完整 transcript、模型的 render/inspect/review/repair 事件；审计文件作为 sidecar |

## 新 goal 的实际边界

新 goal 仍然要求模型自己完成可见的学习闭环：读取 skill，建立 scene，执行
headless 渲染，查看白模输出，并在 skill 的 review 状态或任务证据指出问题时
修正后重新渲染。它没有把渲染变成模型看不见的后台黑箱，也没有把 compare 或
README 从数据集中删除；只是把二者从模型会话的硬停止条件移到统一的会话后
审计步骤。

新 goal 不要求模型创建额外的 `render_job`、队列或 manifest。后续如果需要
统一验证，可以在不改变模型 prompt 的情况下增加 postprocessor；模型已有的
渲染命令和观察结果仍以 transcript 为准。这样不会预先假设所有任务都需要
refine，也不会把后台渲染的成功误当成模型完成了观察和修正。

## 后处理与审计

`e2e/tasks/postprocess_render_artifacts.py` 在会话结束后读取 batch log。对有
`whitebox.mp4` 的 item，它在同一个任务镜像中生成整段 hstack `compare.mp4`、
中间帧 `compare.jpg`、README 和 `postprocess_report.json`，并用 ffprobe 记录
时长、帧率、尺寸、帧数及文件大小。后处理失败只标记 sidecar，不回写模型
transcript 的成功状态；训练样本是否纳入由核心白模交付和后处理报告共同决定。

后处理必须幂等、只写 item 的 managed output 目录，并保留参考视频 hash、白模
视频 hash、使用的镜像 digest 和命令参数。图片输入仍按原文件内容处理，不在
这一阶段脱敏或丢弃。

## 对训练数据的影响

转换脚本仍然只从 Claude transcript 重建消息序列；API trace 只用于核查顺序、
schema 和遗漏。新 goal 不会丢掉模型调用 render、查看图片、ffprobe、review
和 repair 的事件。`compare.mp4`、`compare.jpg`、README 和 postprocess report
作为文件引用或 sidecar 加入样本元数据，不伪装成模型消息。这样既保留模型
学习“自己渲染并检查”的行为，也避免让格式化审计文件控制一次长会话是否被判
定为成功。

