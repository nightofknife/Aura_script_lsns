# 识海深潜完整循环

入口：`tasks:consciousness_deep_dive_loop_pc.yaml:consciousness_deep_dive_loop_pc`，方案 `resonance_pc`。

在 GUI 的「小任务 → 识海深潜」里选择下潜深度、开局策略和循环次数，然后点「开始完整循环」。任务启动时保存配置，每局开局都用同一组选择。

| 任务输入 | 可选值 | 默认值 |
| --- | --- | --- |
| `loop_count` | 正整数表示完整局数。`-1` 持续循环，直到取消或某段失败。 | `1` |
| `dive_depth` | 整数 `1`～`5` 对应深度Ⅰ～Ⅴ。是否解锁由游戏当前账号决定。 | `1` |
| `opening_strategy` | 源流之策 `source`、盈缺之策 `waxing_waning`、回生之策 `revival`、捍卫之策 `defense` | `source` |

启动时必须在识海深潜活动首页，通过标题与开始下潜连续两次检查。点击「开始下潜」后，如果游戏打开上次未提交的策略或编队草稿，确认页面后返回深度页并应用本轮设置。

## 三段顺序与计数

外层 `aura.run_task` 调用逐局编排子任务，每局依次：

1. `consciousness_deep_dive_pc` 从活动首页进入深度选择页，确认目标深度和开局策略，完成编队与祝福选择，并确认进入棋盘。遇到锁定深度或无法确认的选择时，循环停在当前页面。
2. `consciousness_deep_dive_planned_run_pc` 通过桥接器规划和执行，单次规划上限为 180 秒。`terminal_reached` 为真且 `terminal` 为 `settlement` 时进入收尾。探索完成和探索中断都可进入收尾，胜负单独记录。
3. `consciousness_deep_dive_cleanup_pc` 按结算页面依次继续和确认，直到活动首页连续识别成功。

收尾确认回到首页后，通常将 `completed_runs` 加一。战前补员不足而退出结算的一局会重开，回到首页时跳过计数。达到目标局数后停在活动首页。

位面间隙不足五名可战斗乘员时，关卡内自动运行通过游戏原生「结束下潜」进入结算。完整循环完成收尾后，若还有目标局数，就从首页开启下一局。

检查点读取 `aura.run_task` 返回的子任务业务结果。阶段阻塞时保留可用的失败原因、原因码和最后截图，并结束循环。`deep_dive_loop` 返回本轮设置，`last_result` 记录最后一局实选的深度和策略，以及胜负、结算截图和报告路径。

## 长时间运行与取消

引擎while循环新增显式 max_iterations=-1（无限）和 retain_last=1（只保留最后一次输出）。现有任务默认1000及保留全部结果不变。循环每次迭代响应取消并让出事件循环。外层及三次run_task节点使用timeout=-1，GUI派发timeout=0，不受GUI单次等待超时截断；各子任务本身的页面、点击和安全限制不变。

每局创建新的规划与收尾会话。外层记录对应结果后释放子会话，任务结束后释放循环会话。磁盘保留各局报告和截图。

进度事件 `task.resonance_pc_deep_dive_loop_progress`，schema= `resonance_pc.deep_dive_loop.v1`，携带外层cid和递增sequence。GUI按cid/任务/schema/sequence过滤，显示当前局、阶段和已完成局数。取消不会自动收尾或关闭游戏，当前未完整收尾的一局不计数。

