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
