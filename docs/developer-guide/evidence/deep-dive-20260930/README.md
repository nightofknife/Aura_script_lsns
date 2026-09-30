# 2026-09-30 识海深潜交接证据

此目录为跨机器接续保存的精简证据，不是任务运行资源。

- live4/：第四次运行会话、输入/确认事件、完整和失败布局、返回后 Reset 图、关键扫描原图及 _overlay 投影覆盖图。
- probes/：此前执行的 HUD、姿态传输和宽参考记录；run_live.py 是当时辅助入口。接续运行前将脚本复制到仓库 .pytest_tmp/<scope>，从仓库根启动，不要在本证据目录运行。
- statistics/：此前分析的源码和 JSON/文本结果，保留 stats_full、stats_bounds、stats_spawn 分组。大型 .npz 和客户端资源未推送，脚本本机路径需按实际环境调整。

JSON 可能保留 D:\project\aura_s_lsns 的绝对路径，按本目录副本查看。扫描 epoch 已过期，不能用保存布局或截图驱动游戏。

详见[交接](../../resonance-pc-deep-dive-handoff-20260930.md)及[实机记录](../../resonance-pc-deep-dive-planned-run-live-20260930.md)。
