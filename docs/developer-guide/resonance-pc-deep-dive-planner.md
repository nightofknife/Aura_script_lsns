# 识海深潜两种移动规划器使用说明

规划器读取完整扫描结果，生成当前玩家回合的逻辑动作和奇点结果分支，保存 JSON 与 Markdown。独立规划入口只生成方案；游戏执行见[关卡内自动运行](resonance-pc-deep-dive-planned-run.md)。

## 选择策略

| 策略 | 参数 | 优化目标 |
| --- | --- | --- |
| 只追奇点 | chase | 最短期望到达回合，不以灵感作为决策收益；同时报告该策略在指定回合内的到达概率 |
| 灵感收益优先 | inspiration | 最大化指定回合内与奇点相遇时取得的灵感收益期望；超时分支对这一联合目标记零 |

灵感策略另外报告截止或提前相遇前的实际拾取期望，包括最终超时局的拾取；这与联合收益分开。灵感随层旋转搬运，奇点吞入时从自由集合移除，吞入物不算玩家所得，也不假设战斗会返还。

两个算法都枚举先移动后旋转和先旋转后移动，并按奇点同面曼哈顿追击、不同面相邻随机移动、随机层旋转计算分支。相遇发生后立即终止该分支。概率来自规则模型，不代表实机识别或点击成功率。

## GUI 操作

在小任务的识海深潜面板中：

1. 先完成魔方布局扫描，或使用已有的完整成功扫描 JSON。
2. 在策略中选择只追奇点或灵感收益优先，设置规划回合数，默认六回合。
3. 选择布局文件；留空时使用 logs/deep_dive_scan 中最新完整成功扫描。确认它与当前棋盘一致，旧扫描不能自动代表当前状态。
4. 点击生成移动方案，完成后查看指标和本回合动作，或点击打开方案。

GUI 计算预算为三十秒，计算放在线程中，取消时协作停止并等待线程退出。预算耗尽会保留明确的未完成结果，不把短预算求出的中间值当作请求回合的完整解。

## 任务与命令行

任务入口为 tasks:consciousness_deep_dive_plan_pc.yaml:consciousness_deep_dive_plan_pc。公开动作是 resonance_pc.plan_consciousness_deep_dive_layout，输入为布局路径、策略、规划回合数和计算时间预算。

在仓库根目录可使用：

```powershell
python -m tools.plan_deep_dive_layout --strategy chase --turn-budget 6
python -m tools.plan_deep_dive_layout --strategy inspiration --turn-budget 6 --layout-path logs/deep_dive_scan/RUN/layout.json
```

将 RUN 换为实际扫描目录。命令行默认也使用三十秒计算预算，可通过 time-budget-sec 参数调整。

每次结果写入 logs/deep_dive_plan 下的独立运行目录，包含 plan.json 与 plan.md。status 为 solved 表示方案计算完成，不表示游戏已经到达奇点。blocked 表示输入或规则条件不足，cancelled 表示取消，search_budget_exhausted 表示未在计算预算内完成请求的求解。

## 代码接口与输出

统一接口位于 _deep_dive_movement_planner.py：

```python
plan_layout(
    layout,
    strategy="inspiration",
    turn_budget=6,
    time_budget_sec=30,
    cancel_check=None,
    collected_count=0,
)
```

collected_count 是本位面此前已确认拾取的灵感数，默认零；逐回合重新规划时应携带真实的累计值，保持总收益目标一致。cancel_check 可以返回真，或抛出调用方取消异常。

next_action 是下一项逻辑操作，player_turn_actions 是本回合完整操作顺序。移动使用面、行、列作为目标；层旋转给出当前角色格、世界坐标轴、层和右手正负九十度。它们尚未转换成当帧按钮点击位置。

boss_branches 保存每个奇点结果的概率、变化后的玩家和奇点位置、自由灵感位置、预计新增吞入数、累计已拾取数以及剩余回合。实际执行后，应先确认状态再选择对应分支或重规划，不能把预计拾取数直接提交为已经获取。

metrics 中的主要字段如下：

| 字段 | 含义 |
| --- | --- |
| deadline_encounter_probability | 所选策略在指定回合内到达的概率 |
| optimal_expected_turns | 只追模式的最短平均回合 |
| expected_new_inspirations | 灵感模式从当前状态开始的实际拾取期望 |
| expected_successful_new_inspirations | 新拾取中按期到达分支贡献的联合收益期望 |
| expected_boss_consumed_inspirations | 从当前状态开始被奇点吞入的数量期望 |
| expected_encounter_turns_given_deadline | 指定回合内成功到达局的条件平均回合 |

只追的低层接口还支持 max_deadline_encounter_probability，用于最大化截止回合到达率；统一接口可通过 chase_objective 指定。GUI 的只追默认采用最短期望回合，两种目标的策略和指标不可混淆。

## 当前支持范围与实现方式

三至五阶共用坐标和层转规则。盘面须确认唯一玩家和奇点。每轮玩家有一次同面移动和一次层转，移动范围为一格或两格。奇点继续使用一格移动和随机层转的模型。灵感模式支持最多四枚场上灵感。

只追模式按二十四种整体旋转合并等价的角色位置，使用策略评价与上下误差界求解最短期望。每枚灵感在层转中始终落在其整体旋转可达的格子内。灵感较少时，只在这些候选格上做完整的有限回合动态规划，完成的求解层可在进程内复用。

拾取或吞掉灵感后，求解器可复用旧表中覆盖当前场上灵感和已拾取数的求解层。以实际棋盘、收集数和剩余回合为准。方案的 `solver.reused_inspiration_subset` 记录是否复用了灵感减少前的结果。

三至四枚灵感或联合转移数组预计超过 160 MiB 时，先按双方位置求期限相遇率，再比较当前及下一回合的灵感收益。自动运行策略仍优先最大化模型内的期限相遇率。报告会说明使用的算法，并单独显示两回合收益。求解器在内存中复用数组，无需预制磁盘表。

路径规划把相遇视为终止。节点事件、道具、骰子和战斗由执行端处理，行动额度或位置变化后读取实际棋盘并按剩余额度重规划。

