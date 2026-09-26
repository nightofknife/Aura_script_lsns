# PC 识海深潜：单局移动与事件测试任务设计

状态：首版任务 YAML、识别与状态 Action、GUI 测试入口已实现；两次实机分别从棋盘进入“地板塌陷”选项页和“精英战斗”确认卡后停下，尚未跑到结算。精英战斗头像变化暴露了旧识别器的限制；现已改为根据稳定标题预分类，尚未第二次实机复测修订。事件清单、选项界面结构和后续响应见[事件响应设计](resonance-pc-consciousness-deep-dive-event-response-design.md)。本文针对从魔方初始界面到任务结算界面的**一局测试**。简单版的长期目标与样图见[随机游走需求](resonance-pc-consciousness-deep-dive-simple-random-walk.md)，连续入关与重开留给[完整循环设计](resonance-pc-consciousness-deep-dive-simple-loop-design.md)。

## 启动和完成条件

- 测试任务单独公开为 `tasks:consciousness_deep_dive_single_run_test_pc.yaml:consciousness_deep_dive_single_run_test_pc`。操作者先用现有“开始下潜”任务或手动操作到达[魔方初始界面](images/resonance-pc-deep-dive-simple/01-board.png)，然后启动测试任务。它不重复执行入关任务。
- 启动时检查游戏客户区为 `1280×720`，画面稳定，处于魔方棋盘和“玩家行动中”阶段，且右侧显示尚未执行的移动步骤（如 `0/1`）。移动按钮在尚未打开选择界面时不一定呈青蓝高亮；前置条件不满足时返回 `blocked` 和现场图，不进行试探点击。
- **唯一的正常终点是已确认的结算画面**，包括目前有样图的[“探索中断”失败结算](images/resonance-pc-deep-dive-simple/04-settlement.png)以及待补样图的胜利结算。到达结算即截图、返回结果，并把画面留在游戏中；不按空格、不退出、不重新开局。
- 单局游戏失败属于测试任务的正常完成，自动化看不懂事件、动作未得到确认、超时或取消属于另一类结果。回合预算用尽只停止发起新移动，并等待游戏真正显示结算；不能把本地计数等同于结算。

## 一回合的状态机

每次观察先识别结算，再识别覆盖层（事件卡、事件页、旋转预览），最后识别移动／旋转选择态；覆盖层可能与旧蓝框或高亮按钮同时留在画面上。识别前先等动画稳定，关键界面元素在相邻帧保持一致才推进。每个等待都有超时和取消检查。

| 状态 | 识别和操作 | 进入下一状态的证据 |
| --- | --- | --- |
| `player_ready` | 确认玩家行动和移动尚未执行，点击右侧“移动” | 移动选择态出现，且检测到合法蓝框 |
| `move_options` | 从**当帧**蓝框内随机选一格，记录候选与点击位置 | 出现事件入口卡，或出现可证实的直接移动结果 |
| `move_entry` | 区分“小玩意儿”“莫测漩涡”等卡，识别底部“进入”并点击 | 进入实际事件页；此时仍未认定移动完成 |
| `event` | 根据事件页类型运行对应处理器，确认事件完成及返回棋盘 | 处理器提供明确完成证据，棋盘进入旋转可操作态 |
| `move_verified` | 无事件时确认直接移动完成；有事件时核验处理器结果。读取目标进度 | 旋转按钮可用。此时才把这次移动加入成功历史 |
| `rotate_options` | 确认旋转步骤可操作，点击右侧“旋转”，随机选择当帧检出的青蓝箭头 | 出现撤销／确认预览；选择方向不算旋转完成 |
| `rotate_preview` | 识别并点击[“确认”](images/resonance-pc-deep-dive-simple/11-rotate-preview-confirm.png) | 预览消失，后续状态显示旋转已生效；此时才把 `turns_completed` 加一 |
| `enemy_wait` | 等待敌方阶段和动画结束，期间继续检查结算 | 下一次 `player_ready` 或结算 |

如果在任一状态先出现结算，就保存结果并结束。若出现未知页面，优先重拍并确认其稳定性；仍无法归类时返回 `blocked`，绝不沿用上一帧坐标继续点。单个状态超时不自动跳过，也不把框架节点执行成功当作游戏动作成功。

## 移动选择与避免重复

从蓝色描边的可选格获取候选，不识别玩家所在格、格子身份、Boss 或灵感。既有离线样图中，玩家在边缘时检出三格，在中间时检出四格；候选数不写死。按客户区坐标点击当帧候选框内安全点，不能把带 Windows 标题栏的整张截图坐标直接用于输入。

每次选择保存 `turn_index`、当帧候选集合、被选候选的位置、点击结果和最后的移动确认结果；同一次选择界面中不重复点已尝试的候选。若画面和候选布局有足够稳定的可比特征，可以降低立即折返的选择概率，否则只随机挑当前合法选项。由于本版不识别节点或重建魔方，**不能保证全局不走到曾到达的节点**；日志不得声称做到了全局路线去重。

## 事件处理合同

[“小玩意儿”](images/resonance-pc-deep-dive-simple/09-move-event-item.png)与[“莫测漩涡”](images/resonance-pc-deep-dive-simple/10-move-event-vortex.png)目前只验证了入口卡：两者都可在客户区约 `(1090,542)` 找到“进入”。该点击仅打开事件，不能代表事件已解决。设计两个独立的事件处理器，由进入后的**实际页面**分派，而不是只按入口标题猜后续操作。

后续事件处理器应接收当前截图与当前局状态，逐步返回 `{event_type, phase, action_taken, completion_evidence, progress_after}`。它只能在观察到明确的完成／返回棋盘证据时报告 `completed`；对尚无规则的页面返回 `unsupported_event`，附当前截图、识别信息和状态。首版只实现入口识别与点击：若事件自动返回到已完成移动的棋盘，可以继续；否则保存进入后的画面并停止，等待补充该事件的处理器。事件造成的失败或胜利若直接显示结算，应由全局结算识别接管。不同事件之间不共用盲点式“确定”流程。

任务进度与事件结果分开记账：右侧“获取灵感”等进度可读时记录读数；读不清时标记 `unknown`，不可填造数值。若该进度不是完成当前回合的必要条件，不应因读数缺失而重复事件操作；记录后继续按可见棋盘状态推进。

## 回合计数、超时和任务结果

输入建议为 `{round_budget, random_seed?, max_round_iterations?, phase_timeouts?}`。`round_budget` 由实际游戏规则确认后配置，不能由某张示例图中的剩余回合数硬编码，也不做剩余回合 OCR。`turns_completed` 初始为零；一次**已确认的移动**加一次**已确认的游戏旋转**才完成一回合。已点击蓝框、已点击事件“进入”或已点击旋转箭头都不加回合。

单局状态可用 `{session_key, phase, turns_completed, round_budget, move_attempts, move_history, events, objective_progress, last_frame}` 保存。设置显式 `max_iterations` 与每阶段超时，防止画面不变时无限循环。完成回合后若还有额度，等待敌方结束并继续；额度耗尽时只观察结算，不再发起移动。如果迟迟没有结算，返回 `blocked` 并保留现场。

任务返回业务结果示例：

```yaml
status: completed             # completed | blocked | cancelled
terminal: settlement          # completed 时必须是 settlement
outcome: failure              # failure | victory | unknown
turns_completed: 3
move_history: [...]           # 只包含得到确认的移动
events: [...]                 # 含事件识别、处理结果及证据
objective_progress: unknown   # 或读到的结构化进度
settlement_frame: <repo-local artifact path>
```

仅“探索中断”失败结算已有样图；在取得胜利样图和验证识别器前，不能把所有其他画面都当成胜利。确认是结算但尚不能区分胜负时允许 `outcome: unknown`，仍可完成“到达结算”的测试目标。`blocked` 输出另含 `{phase, reason, last_frame, turns_completed, move_attempts}`，用于复盘失败点。

## 在 PC 计划中的实现边界

任务 YAML 负责编排：先运行分辨率／初始状态预检，再用有界 `loop.while` 反复调用一次状态推进 Action，最后由结果 Action 返回已确认的结算或阻塞状态。循环条件读取本局共享状态，不能仅凭节点 `status=success` 继续。现有 `eternal_scuffle_pc.yaml` 提供了有界 `while` 的编排实例。

Action 分别承担一次页面识别／候选点击／事件步骤／进度读取／状态核验；跨调用的本局历史由计划内状态对象或服务持有。正式视觉模板与处理器归入 `plans/resonance_pc`；`.pytest_tmp` 中的离线探测脚本仅是实验，不作为打包依赖。实施后按仓库流程同步 manifest，并从仓库根目录运行 `package_cli check/validate`、Plan Doctor 和聚焦测试，临时输出放在 `.pytest_tmp`。

## 验证顺序与缺少的画面

1. 静态回归：用已给的初始、边缘／中间蓝框、边缘／中间箭头、两张事件入口卡、旋转预览及失败结算图检验阶段优先级与候选点；加亮度变化和动画误判样本。已有原型仅证明九张静态图能定位两个“进入”和一个“确认”，并非实机通过。
2. 状态测试：蓝框点击、事件“进入”、旋转方向点击均不得提前记账；事件完成后才记移动，旋转“确认”后才记回合；结算在任意阶段抢占并保留画面；未知事件和超时返回 `blocked`；预算耗尽后不再发出移动。
3. 实机先验证单回合，再跑到结算，检查最后游戏画面和返回的截图、回合历史一致。运行期间不自动重开。

为实现完整事件处理，仍需两类事件点击“进入”后的页面、每个选项产生的后续画面，以及事件完成／返回棋盘的样图；还需敌方阶段、直接移动无事件路径和胜利结算样图。取得这些证据前，测试任务可以安全停止在 `unsupported_event`，但不能承诺从任意初始棋盘自动走到结算。
