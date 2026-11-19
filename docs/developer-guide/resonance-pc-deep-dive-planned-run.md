# 识海深潜关卡内自动运行

关卡内自动运行已接入；完整流程仍需独立实机验收。

## 使用入口

在 GUI 的识海深潜面板选择“关卡内自动运行”。游戏客户区须为 1280×720，开始画面须是稳定玩家棋盘或位面间隙。独立回合安全上限默认为 100，可设 1–100。复用现有取消按钮。

运行识海深潜任务前，在 GUI 设置中启用并保存「使用桥接输入器」。命令行使用 `resonance_pc.input.mode: bridge`。移动和层转规划按桥接快照报告的阶数使用三阶、四阶或五阶规则。旧的拖动和扫描路线参数继续接受，棋盘读取统一走桥接快照。

任务为 `consciousness_deep_dive_planned_run_pc`，采用 `initialize → advance → finish`。任务参数还可设置扫描预算和规划预算（默认 30 秒）。自动运行统一使用 `encounter_then_inspiration`，旧调用传入的 `chase` 或 `inspiration` 也会按这个目标运行。

位面间隙会读取可战斗人数。达到五人时继续下潜，不足五人时通过游戏的「结束下潜」进入结算。最终结算需标题与布局证据及两次稳定确认，分别记录成功、失败或胜负未确认。关卡内任务停在结算页，完整循环接着返回活动首页。

## 已接入的循环

1. 每个新玩家回合从桥接器读取一份完整棋盘快照，核对玩家、奇点、灵感和六个面的全部节点。
2. `plan_current_state` 使用当前棋盘和真实剩余回合、移动及层转额度。求解器先比较期限内碰上奇点的概率，概率相同时比较成功相遇分支的灵感期望。战斗胜负独立于路线评分。旧坐标和旧方案随重新读取失效。
3. 桥接器调用客户端的移动和层转界面处理函数。移动从客户端可移动格列表中按原生面号和行列选择，层转按预览后玩家所在面匹配客户端方向。
4. 每次调用前在客户端核对地图、棋盘阶数、位面、玩家格、移动距离和行动额度。识别节点确认卡后，通过桥接核对客户端已选格并确认进入。层转预览与目标一致后才调用客户端确认。
5. 节点、奖励、战斗各保留来源与恢复阶段。移动额度和节点完成分开记录。自身动作结束后如仍有额度，会读取真实棋盘并重新选择剩余动作，计算时可复用相同规则下的未来回合数值。额度耗尽后等待实际奇点阶段。
6. 奇点行动结束、新位面加载后重新扫描，不依赖奇点动作记录。普通战斗胜利不能结束位面；奇点战胜利后仍需实际间隙、新位面或最终结算。

回生空洞和莫测漩涡按原生事件及选项 ID 执行各自目录的选择策略。事件和战斗的奖励卡、途中出现的单选奖励和满包弹窗均由桥接器读取及操作。奖励一张选唯一、两张选第一张、三张选中间。满包时保留已有直觉，放弃新直觉。

扫描阶段独占输入并排空工作线程。其他阶段每次推进最多发出一个原生操作或点击。取消或阻塞时保存状态并释放本任务可能持有的鼠标。

位面序号、剩余回合、移动和层转额度由桥接器读取，位面序号使用当前地图配置的 `faceIndex`。移动距离和有效额度包含客户端加成。棋盘和额度来自同一份快照，校验期间位面或额度变化则重新读取。画面继续提供页面和灵感收集进度。已确认的战斗、奖励、事件和结算页面跳过 OCR。

## 代码对应

| 能力 | 源码 |
|---|---|
| 任务与会话调度 | [动作入口](../../plans/resonance_pc/src/actions/consciousness_deep_dive_planned_run_pc_actions.py)、[任务定义](../../plans/resonance_pc/tasks/consciousness_deep_dive_planned_run_pc.yaml) |
| 内部扫描引擎及原报告包装 | [扫描动作](../../plans/resonance_pc/src/actions/consciousness_deep_dive_scan_pc_actions.py) |
| 操作视图与预览置换 | [三面映射](../../plans/resonance_pc/src/actions/_deep_dive_operation_frame.py)、[动作事务](../../plans/resonance_pc/src/actions/_deep_dive_directed_operation_flow.py) |
| 当前状态规划 | [规划入口](../../plans/resonance_pc/src/actions/_deep_dive_movement_planner.py) |
| 页面、HUD、位面与终点 | [观察器](../../plans/resonance_pc/src/actions/_deep_dive_planned_run_vision.py)、[资源来源](../../plans/resonance_pc/templates/deep_dive/deep_dive_planned_run/sources.json) |
| 事件来源与恢复 | [运行时事件](../../plans/resonance_pc/src/actions/_deep_dive_runtime_events.py) |
| GUI | [面板](../../packages/resonance_gui/widgets/consciousness_deep_dive_panel.py)、[桥接](../../packages/resonance_gui/bridge.py) |

原随机游走、独立扫描及离线规划入口保留。

## 会话与证据

结果位于仓库 `logs/deep_dive_planned_run/<session_id>/`：

- `session.json`：位面、实际额度、扫描／地图／镜头版本、布局、待确认动作、事件上下文、收益账本及停止原因。
- `scans/`、`plans/`：每次完整扫描与相应方案。
- `operations/<action_id>/action.json`：目标、三面匹配、旋转预览及确认依据。
- `frames/`、`events.jsonl`、`report.md`：输入前与终止截图、事件记录和最终摘要。

`terminal_reached` 表示已经到达结算；`game_success` 表示游戏胜败，未知时为 `null`。收益账本只采用节点结束后或稳定玩家棋盘的地图灵感 HUD 进度，不按奖励点击数入账，也不把奇点战后的旧位面清空计数记作玩家拾取。

## 当前限制及后续验收

- 三至五阶的每回合模型包括一次同面移动和一次层转。读取到的移动距离为一格或两格，最多纳入四枚场上灵感。未知节点入口、无法确认的 HUD 或超出模型的移动距离和行动额度会使任务暂停。
- 节点入口和结算继续通过画面确认。移动完成后核对实际额度，再读取新棋盘。
- 尚无确认的战败专用视觉资源；未知战斗画面有界等待后暂停并保留截图。
- 本次新扫描无法对齐前次灵感身份时，不猜测单枚灵感的消失原因；规划使用当前自由灵感集合和已确认 HUD 收集基线。

已在三阶棋盘逐一打开四个层转预览并撤销，核对了移动到商店格的选中坐标后撤销。实际确认动作、位面切换、整局结算和取消清理仍需完整实机验收。
