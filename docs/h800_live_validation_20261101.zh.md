# H800 上首次真实跑通 agent 编排链路(2026-11-01)

此前 H800 的 `claude_exp` 区域只用来测推理服务(vLLM/DeepInfer 吞吐),从未真正跑过
Claude Code agent 容器。本文记录第一次跑通时发现的三个环境问题,以及
`detach-containers` 分支(容器脱离/编排器重启后接管)的首次真实验证结果。

## 1. h1(cog_h800_80_1)docker0 网桥缺失

`docker network inspect bridge` 显示默认 bridge 网络配置存在,但 `ip link show docker0`
报 "Device does not exist"——网桥的 Linux 设备层丢了(iptables NAT 规则还在,只是设备没了,
原因不明,未追查)。导致任何不显式指定 `--network host`/自定义网络的 `docker run` 直接失败
(`failed to create endpoint ... adding interface ... to bridge docker0 failed: Device does not exist`,
exit code 128)。

修复(未重启 dockerd,未影响任何已在跑的容器——用 `docker network inspect bridge` 确认
当时没有容器挂在默认 bridge 网络上):

```bash
ip link add name docker0 type bridge
ip addr add 172.17.0.1/16 dev docker0
ip link set docker0 up
```

验证:`docker run --rm agent-scaffold-media2threejs:h800-2.1.310 echo ok` 恢复正常。
**未查清网桥设备消失的根因**,如果复现需要进一步排查(可能与其他实验的网络操作有关)。

## 2. Claude Code 不能以 root 运行

`run_tasks.py` 的 `build_container()` 用 `--user {os.getuid()}:{os.getgid()}` 把宿主机
当前用户映射进容器。本机此前所有操作都是用 root SSH 做的,所以映射出来容器也是 root,
触发 Claude Code CLI 的安全检查:

```
--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons
```

容器里 Claude Code 进程直接 exit 1,`tool_calls=0`,两次 attempt 都是空结果,被判定
`fail_stalled`——看起来像卡住,其实是启动即失败。

修复:本机有现成的非 root 用户 `zlnn`(uid 1000),原来不在 `docker` 组。
`usermod -aG docker zlnn` 后用 `sudo -u zlnn ...` 启动 `run_tasks.py`,容器内 Claude Code
正常工作(真实跑出抽帧、contact sheet、ascii/median 分析等 media2threejs 技能产物)。

**结论:H800 上所有 `run_tasks.py` 的生产调用都必须以 `zlnn`(或其他非 root 用户)身份启动,
不能用 root。** 这是此前从未被发现的问题,因为 H800 的 `claude_exp` 目录此前只用于推理服务
实验,没人真正跑过 agent 容器。

## 3. `detach-containers` 分支首次真实验证(通过)

分支 `detach-containers`(`8d6176e`,10 个 mock 单元测试此前已全过,但没有在真实 docker
daemon 上跑过)。用一个真实 warehouse 条目(`BV1Cu4y1B7Rs_p1`,来自
`/share2/dataset/media2threejs-warehouse/lists/whole_video_triage_pass.jsonl`)、
`detach_on_signal: true`、worktree `/share2/claude_exp/detach_test`(未碰生产用的
`main` checkout)验证:

1. `docker run -d` 起容器,标签 `run_tasks_batch=<hash>` + `rb_item=<id>`,容器名
   `rt_<hash>_<id>`——确认真实 `docker ps -a --filter label=... --format` 查询语法
   和 Python 侧解析完全匹配(mock 测试没覆盖这条,是最大的真实风险点,现已验证)。
2. 容器真实跑到"已抽帧、生成 contact sheet"阶段(约 1 分钟)时,对 runner 进程发
   `SIGTERM`:runner 日志打出 `leaving containers running`,2 秒内进程退出,
   **容器继续运行**(`docker ps` 确认 `Up About a minute`,未被中断)。
3. 立即用同一份 config 重新拉起 runner:日志打出
   `re-adopting 1 container(s): BV1Cu4y1B7Rs_p1 (running)` 和
   `adopt rt_c8da54b072_BV1Cu4y1B7Rs_p1`——**正确识别并接管了存活容器,
   没有重新起一个新容器,没有要求 Claude Code `--continue`、没有重新 prefill 上下文。**

**结论:`detach-containers` 分支的核心机制(标签化容器 + SIGTERM 不杀容器 + 重启后按标签
接管)在真实环境下工作正常,可以合并。** 这是此前文档里"编排器重启不得停容器"这条生产约束
(`docs/01-orchestration-findings.zh.md` 对应 deepseek-infra-opt 仓库)的实际解决方案,
预计能把 `batch_stop` 造成的跨 attempt 死时间(此前实测约 2.6 小时/item,占总墙钟时间的
大头)基本消掉。

## 4. 待办

- 把 `detach-containers` 合并进 `main`(已在真实环境验证,无阻塞项)。
- 生产配置(`media2threejs_core_whitebox.json` 等)需要加上 `"detach_on_signal": true`,
  并确认统一用 `zlnn` 身份启动。
- 之前的 `chem_test10.jsonl` 等条目文件里的视频路径是 A800 的 `/slow_share2/...`,
  在 H800 上不存在(本次测试因此踩了一次坑,已查明并绕开,改用
  `media2threejs-warehouse` 的真实清单)。H800 上的生产跑批必须用
  `/share2/dataset/media2threejs-warehouse/lists/whole_video_triage_pass.jsonl`
  (23,092 行,1483.5 小时),不要沿用旧的 chem_test10/chem_videos 条目文件。
- docker0 网桥消失的根因未查,如果再次复现要留意。

## 5. 16 并发(cpus_per_item=6)下的"CPU 争用"排查:不是核数不够,是容器内线程池没按配额设

跑了两个 16 并发的真实试点批次(每台机 16 个真实 warehouse 条目,`cpus_per_item=6`)后,
`uptime` 显示 load average 一度到 **819**(192 核的机器),看起来像严重争用。但 `top` 同时
显示 `%Cpu(s): 49.1 us ... 49.2 id`——**实际 CPU 使用率只有约 49%,一半是空的**。

真正原因:`docker inspect` 确认每个容器的 `--cpus 6` 配额(cgroup NanoCpus)确实在生效,
但容器里的 OCR/帧分析/ffmpeg 这类工具(用到 OpenCV、NumPy、OpenBLAS 等库)默认会读**宿主机**
的 CPU 核数(192)来决定开多少线程,而不是容器的 cgroup 配额(6)。于是每个容器内部有远超
6 个的线程在抢那 6 个核,被 cgroup CFS 配额持续限流——这些线程处于"可运行但在等下一个配额
周期"的状态,会被内核计入 load average(`/proc/loadavg` 第四个字段当时是 `3115 个可运行线程
/ 33723 个总任务`),但并不代表真的需要那么多核,也不代表 CPU 已经跑满。

**这是容器内部的线程抢占/抖动,不是主机级别的核数不够。** 修复:给 `claude_env` 加上
`OMP_NUM_THREADS/OPENBLAS_NUM_THREADS/MKL_NUM_THREADS/NUMEXPR_NUM_THREADS/OPENCV_NUM_THREADS/
VECLIB_MAXIMUM_THREADS=6`(和 `cpus_per_item` 对齐),让这些库的线程池大小匹配实际配额,
减少无意义的线程抖动。已经对两个试点批次的配置生效(仅影响重启后新接的条目,已在跑的 32 个
容器不受影响,按 detach 机制安全重启验证过不会丢进度)。

**另一个观察,未深挖:** `ps -eo stat` 当时统计到 4300+ 个僵尸进程(`Z`/`ZN` 状态)。
不在 D(磁盘等待)状态里排队,所以不是 NFS I/O 造成 load average 虚高的原因;
本机已运行 99 天,僵尸堆积的根因可能和今晚的工作无关(也可能是之前 PD 容器清理时反复遇到
"zombie, can not be killed" 的残留),未进一步排查,留给后续关注,暂不影响任务本身。

**对"docker 调度优化"的结论:** 加大 `cpus_per_item` 本身不是最优修复(容器内工具不感知
cgroup 配额,给更多核只是推迟而非解决线程抖动);真正该做的是让容器内工具的线程池大小
匹配分配到的核数。生产配置之后应该把这几个线程数环境变量作为默认项,而不是每次临时加。
