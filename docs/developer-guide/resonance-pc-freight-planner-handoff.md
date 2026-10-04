# 四模式规划器接入说明

四模式规划器已接入货运 action、任务 YAML、GUI 和生成 manifest。本说明重点记录纯规划器、规划服务缓存及求解语义；下文 2026-10-04 的测试和基准是服务实现阶段的历史记录，不代替当前接入验证或游戏实机验收。

日期：2026-10-04。默认配置同步：疲劳700、货舱750、每本收益下限500000。投资、恢复等执行设置不属于规划器参数，由action/task/GUI线程设置投资true、恢复关闭。

## 正式入口

`ResonancePcTradePlannerService.plan_optimal_route`（`plans/resonance_pc/src/services/resonance_pc_trade_planner_service.py`）仍是服务入口。`ResonancePcExactTradeSolver.solve` 是冻结数据后的纯求解入口。

| 参数 | 类型与默认值 | 契约 |
| --- | --- | --- |
| trade_mode | str = profit | profit / quick / fixed / target |
| fatigue_budget | int = 700（服务） | 整个任务预计疲劳上限，固定模式含定位 |
| cargo_capacity | int = 750（服务） | 正整数，每航段的货舱 |
| book_budget | int 或 None = 0 | 唯一全路线书总上限；None 无限；0 禁书 |
| book_profit_threshold | 精确金额 = 500000 | 每本 >0 且 >= 下限，逐本前缀均达标 |
| book_policy | str = profit | profit 允许满仓后优化商品组成；fill 首次满仓停止 |
| negotiation_policy | str = auto | auto / required / disabled；required 仅双协商贸易 |
| negotiation_budget | int 或 None = None | 可选全路线完整协商次数上限；None 不限次数 |
| fixed_route_city_ids | 有序 list[str] 或 None | 固定模式保留顺序及重复；闭环循环，开放线路只推进一次 |
| reposition_to_route | bool = False | 固定模式专用；不符且关闭报参数错误；开启先定位 |
| target_profit | 精确正金额或 None | target 模式必需；其他模式忽略 |
| available_city_ids | list[str] 或 None | 自由路线城市范围；固定模式忽略此字段 |
| required_end_city_ids | list[str] 或 None | 自由路线合法终点；固定模式忽略 |
| bargain/raise_success_rates_bps | list[int] 或 None | 基点序列；None 使用规则默认值 |
| bargain/raise_step_bps | 整数基点 = 1000（服务） | 协商幅度，保留既有期望疲劳模型 |
| city_prestige / product_unlocks | dict 或 None | 既有声望、税率、进货量及解锁契约 |
| current_city_id / key / current_city | 可选字符串 | 服务解析实际起点；solver 使用 start_city_id |
| snapshot_id | str 或 None | 服务冻结指定行情或 latest |

快速模式固定 `fill + required`。没有 `auto_book`、`all_plan`、`trade_level`、`active_events` 或固定执行次数字段的兼容入口；action 的转发参数和任务/GUI schema 已使用新契约。旧滚动/循环辅助方法未修改，本次入口不使用它们。

## 模式排序与精确性

- profit / quick：收益最多、疲劳最少、书最少、协商最少、航段最少、稳定城市路径和边签名。
- target：达标后疲劳最少、书最少、协商最少、航段最少、稳定路径；超额利润不参与平局。不同利润标签不能只凭更高利润合并，尤其更少航段或更早稳定路径的标签。收益可达性截到目标，真实收益保留用于输出。
- fixed：沿指定顺序合法前缀中疲劳最多，其后收益最多、少书、少协商、少航段、稳定顺序。允许部分最后一圈；开放线路走完停止。不同疲劳值均保留，不使用最大收益模式的跨疲劳支配。至少要有一段有效贸易；纯空载耗预算不作为可执行货运计划。

商品模型、满20%定价、JavaScript取整和 Fraction 期望疲劳不变。最大协商尝试次数仍属于执行，不扩展为概率分布模型。首站原有货物不在模型收益内。

书曲线 P(k) 由排序整数单位利润及每批数量累积求值，是离散凹函数。profit 饱和边界由最高单位利润商品的总进货量推导；fill 由全部盈利商品的总量推导。阈值采用二分寻找合法前缀，零增量永不使用。全路线有限书数进一步有严格上界 floor(F * max(K_edge / cost_edge))；超过此界的有限预算与无限在可达资源上等价，外部预算仍保留。单航段的巨大有限预算也直接使用精确曲线，不逐本构造一百万条候选。绑定的多航段有限预算仍保留合法书数用于全局分配。

无限书的 profit/fixed 可以取每种协商配置的最大合法收益候选，因为书不影响疲劳且边收益严格递增；target 保留少书候选并按需展开，目标转换可二分求最小用书。未采用 Beam、Top-K、随机截断或伪造巨大书预算。循环展开边界来自预算和最小正移动消耗；固定线路零/负消耗航段显式拒绝。

## 返回与执行接入

1. `status`：ok / no_plan / target_unreachable / fixed_route_infeasible。非法输入由服务抛 `ResonancePcTradePlannerError(code="invalid_optimal_route_input")`。仅 ok 执行；不可达/不可行结果的 route、reposition_route 为空。
2. 保留 `route[*].buy_products / buy_product_ids / books_used / bargain_to_cap / raise_to_cap / buys`。buys 有 product_id、product_name、quantity、预计单位利润及 exact 值。
3. 每段增加 `leg_type`（trade / navigation）、cargo_capacity、loaded_quantity、is_full_load、load_ratio、book_stop_reason、legal_book_limit、next_book_marginal_profit_exact、route_leg_index。停止加书原因有 profit_threshold、capacity_reached、already_full_without_books、profit_saturated、total_book_budget、global_book_allocation、target_objective、no_suitable_products、navigation_only。
4. `city_visits` 只包含贸易线路访问，索引与 route 对齐。访问的 sell_intent 来自上一段、buy_intent 来自下一段；首站 sell_intent=None（已有货清仓由执行层另做且不抬价），末站 buy_intent=None。空载导航不虚构协商、购买或抬价。重复城市按 visit_index 区分。
5. 固定定位通过 `reposition_route` 单独返回，leg_type=reposition，无书/协商/买卖。定位使用移动图上最小疲劳路径，平局按少航段和稳定城市顺序。执行先完成定位，再清理首站已有货物，再按 city_visits 交易。定位不插入 route，不影响 route_leg_index / sell_intent 索引或圈数。
6. `expected_fatigue_used`（以及 exact）是整任务总疲劳。固定模式另外有 reposition_expected_fatigue、route_expected_fatigue、total_expected_fatigue 及相应 exact 字段，route_fatigue_budget 是扣除定位后的预算。reposition_city_path_ids、start_city_id、route_start_city_id 分清实际起点与线路起点。
7. 固定模式输出 completed_circuits、partial_circuit_legs、partial_circuit_city_path_ids、termination_route_position、termination_city_id、stop_reason、每段 round_index / round_leg_index。索引从0开始；闭环整圈结束位置为0，开放线路走完位置为最后城市下标。最终清仓只在选定前缀末站执行。
8. target 输出 target_profit、target_reached、target_gap、maximum_reachable_profit，均有适用 exact 字段。不可达的最大收益是同约束实际可达结果，不是上界，也不附可执行诊断路线；target_gap 在不可达时为目标减最高可达收益。
9. `request` 为有效模式、约束、账号参数快照；assumptions 保留模型说明；solver_backend / solver_stats 为规划诊断。旧 books_budget 保留与 book_budget 同值，remaining_books 在无限时为 None。
10. 缓存包含全部冻结数据、模式、有限/无限书预算、策略、目标、完整有序固定线路与定位开关；忽略非当前模式参数。保持8项LRU及深拷贝隔离；不接受水/便当等执行参数。

## 验证与边界

命令均从仓库根运行，系统临时目录 `.pytest_tmp/freight_modes/os_temp` 与 pytest basetemp 分开。

```
.venv/Scripts/python.exe -m pytest tests/unit/test_resonance_pc_freight_mode_solver.py tests/unit/test_resonance_pc_freight_planner_service.py tests/unit/test_freight_recovery_policy.py -q --basetemp .pytest_tmp/freight_modes/pytest
```

225 passed（2026-10-04同步700/750默认值后的focused回归3.63秒）。250组固定种子小图 × 四模式 = 1000次独立全枚举对拍；适用有限预算场景另做dense/sparse完全语义一致性。随机对拍含有限/无限书、阈值、合法终点、协商次数、分数期望疲劳、多阶段成功率、零成功率、完整排序。另有目标更低利润但少航段/更早稳定路径、百万货舱曲线、单本阈值等于50万、首次满仓、全路线分书、定位独立索引、固定部分圈和取消等专例。恢复策略原有测试覆盖保留。

package check/validate 均通过；Plan Doctor errors=0，152项警告全部是已存在的pycache/pyc，未清理其他任务产物。只做服务/纯模块变更，无新注册项，不运行 sync 改其他线程维护中的 manifest。git diff --check 通过。

冻结行情基准（21城市、229商品、750货舱、700疲劳、单本50万；快照20260808T190242Z_a1b1919fbf，非当前实机行情；单次热导入后的测量）：

| 场景 | 时间 | 收益 | 疲劳 | 书 |
| --- | ---: | ---: | ---: | ---: |
| profit 0本 | 0.545s | 5916267 | 699 | 0 |
| profit 10本 | 0.609s | 8217414 | 695 | 4 |
| profit 50本 | 0.625s | 8217414 | 695 | 4 |
| profit 无限 | 0.606s | 8217414 | 695 | 4 |
| quick 3本 | 0.188s | 5689086 | 685 | 2 |
| quick 无限 | 0.183s | 5689086 | 685 | 2 |
| target 100万/无限 | 1.011s | 1028949 | 126 | 0 |
| target 500万/10本 | 3.674s | 5080740 | 441 | 2 |
| target 500万/无限 | 3.654s | 5080740 | 441 | 2 |
| target 1亿不可达 | 0.607s | 最大可达8217414 | 无执行计划 | 0 |
| fixed 1→21→15→1/10本 | 0.0068s | 6226584 | 696 | 2 |
| fixed 同线/无限 | 0.0070s | 6226584 | 696 | 2 |

收益0本、10/50本的路线/利润/疲劳与旧冻结基线相同；>=阈值边界是明确的新规则，旧Auto Book严格>测试已由新规则测试替代。明细在 benchmark_results.json 与各场景JSON。目标500万搜索约1424展开标签、50970生成标签、峰值7428待处理标签；缓存2124份不可变边明细后从6.27s降至3.65s。

精确搜索最坏复杂度仍随疲劳、可达标签和绑定的多航段书预算增长，不能从上述默认图测量推断任意大输入都有固定耗时。目标及固定搜索有协作取消和进度输出，没有为性能牺牲最优性。

## 接入文件与验证边界

- 修改：resonance_pc_trade_exact_solver.py、resonance_pc_trade_planner_service.py。
- 新增：resonance_pc_trade_candidate_model.py、resonance_pc_trade_objective_solver.py。
- 新增：tests/unit/test_resonance_pc_freight_mode_solver.py、test_resonance_pc_freight_planner_service.py。
- 删除：旧纯求解器测试 test_resonance_pc_auto_book_solver.py，严格>/忽略预算语义已经撤销。
- 已接入 trade_planner_pc_actions.py、city_trade_flow_pc_actions.py、任务 schema、GUI 和生成 manifest；旧 auto_book 契约及执行测试已由 book_budget、freight_modes 对应测试替代。
- 验证应覆盖任务输入、规划、执行结果和 GUI 接线。此处的历史 225 项测试和静态检查只证明当时的纯规划及相关回归，不证明游戏端已执行四模式。


## 最终签名核对与无解映射

service 使用上表完整关键字参数；solver 的参数与其区别为：start_city_id 替代 current_city_*，fatigue_budget/cargo_capacity 是必填；没有 available_city_ids/snapshot_id（构造实例时已经冻结），bargain/raise_step_bps 默认 None 使用规则默认值；有仅用于离线验证的 _backend。公开参数均没有旧 auto_book、all_plan 或执行次数字段。

无解返回值的 reason 当前与 status 同值，入口应据以下固定映射处理：

| 情况 | status | reason | 附加信息 |
| --- | --- | --- | --- |
| profit/quick 无正收益合法路线 | no_plan | no_plan | route=[]，不是异常 |
| target 未达到目标 | target_unreachable | target_unreachable | maximum_reachable_profit / target_gap / diagnostics.execution_allowed=false |
| fixed 定位加线路预算不足，或没有有效贸易前缀 | fixed_route_infeasible | fixed_route_infeasible | diagnostics 提供定位所需疲劳、扣除后的线路预算及首段移动疲劳；route=[]，reposition_route=[] |
| fixed 起点不符且定位关闭 | 不返回计划 | 不适用 | solver 抛 ValueError("fixed route must begin at the actual start city")；service 包装为 ResonancePcTradePlannerError(code="invalid_optimal_route_input") |
| fixed 开启定位但线路起点不可达 | 不返回计划 | 不适用 | solver 抛 ValueError("fixed route start is unreachable for reposition navigation")；service 同样包装参数错误 |
| fixed 零/负移动消耗、相邻重复城市等非法线路 | 不返回计划 | 不适用 | solver ValueError；service 包装参数错误 |

货运入口前置识别起点并检查定位开关是合适接入方式；service 不会向调用方直接泄漏上述 ValueError。不存在“起点不符自动拼入贸易路线”或“不可达执行最高利润诊断路线”的回退。

book_budget=None 的结果同时保留 book_budget=None、books_budget=None、remaining_books=None；books_used 为实际计划数量。loaded_quantity 位于 route[*]，代表计划商品数量；禁止当作实测货舱OCR值。fixed、target、reposition 的结果字段见第3节，每段/城市访问索引均从0开始。
