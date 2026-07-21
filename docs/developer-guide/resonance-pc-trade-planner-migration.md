# Resonance PC 二值满议价精确规划器迁移说明

## 1. 改造目的与版本

本次改造仅作用于 `plans/resonance_pc`。Android 计划包 `plans/resonance` 没有迁移到这套接口，也没有被修改。

旧规划器把 `negotiation_budget` 解释为底层砍价/抬价尝试次数，并根据声望、贸易等级、连续成功衰减等规则计算每次尝试后的价格概率分布。实际游戏账号的成功率、连续成功衰减和单次幅度差异较大，而且执行器不能可靠控制 0% 到 20% 之间的中间幅度，因此旧模型生成的非满幅度方案无法稳定执行。

新模型只保留两个执行意图：

```text
不议价
拉满到 20%
```

当前规则版本：

```text
trade_rules.schema_version = 2.0.0
trade_rules.model_version = resonance_pc_trade_binary_to_cap_2026_07_19
negotiation.model = binary_to_cap_expected_fatigue
```

生效日期：2026-07-19。

这里的“精确最优”是指：在行情冻结、被选中的满议价最终达到 20%、并支付理论期望疲劳的理想化模型内，求得数学最优解。规划器不模拟实际重试过程，也不保证真实执行疲劳等于期望疲劳。

## 2. 已完成的修改

### 2.1 规则数据

`plans/resonance_pc/data/meta/trade_rules.json` 已升级到 schema 2.0.0：

- 删除旧的声望议价次数、基础成功率、声望成功率加成、连续成功衰减、贸易等级幅度和按尝试次数计算概率分布的规则。
- 固定满幅度为 `2000 bps`，即 20%。
- 固定单次尝试疲劳为 8。
- 保存四项账号议价参数的默认值：砍价/抬价成功率均为 50%，单次成功幅度均为 10%。

### 2.2 精确求解器

`resonance_pc_trade_exact_solver.py` 已改为：

- 每条边只枚举进货书数量以及是否砍满、是否抬满。
- 砍满时买价按 80% 计算，抬满时卖价按 120% 计算，然后继续使用原有游戏取整和买卖税规则。
- 成功率和幅度不再参与利润概率分布，只参与期望疲劳计算。
- 疲劳由整数变为 `Fraction` 有理数，预算比较不提前取整。
- 全局搜索由整数疲劳分层改为精确标签搜索，仅使用严格支配剪枝。
- 继续支持重复城市、开放终点、空载迁移和整条路线共享进货书预算。

### 2.3 服务、动作和任务

- 公开只读动作 `resonance_pc.trade_plan_optimal_route` 已支持两种 `all_plan` 模式和四项账号议价参数。
- `auto_cycle_trade_pc.yaml` 已公开新输入并返回新的期望疲劳及完整议价计数字段。
- 旧的尝试次数字段和整数疲劳使用量字段已从新契约删除，没有兼容别名。
- 自动 UI 尚未实现满砍价/满抬价。自动任务收到 `all_plan=1` 或非零 `negotiation_budget` 时，会在任何游戏 UI 操作和行情刷新前安全拒绝。
- `all_plan=0` 且 `negotiation_budget=0` 的无议价自动路线继续使用原有执行流程。

### 2.4 Manifest 和测试

`plans/resonance_pc/manifest.yaml` 由包同步工具生成，保留了工作树中已有的 PC 战斗动作与任务导出。测试已迁移到新契约，并增加了两种模式的小图暴力对拍。

本次没有实现：

- 砍价/抬价按钮点击。
- 成功或达到 20% 的视觉识别。
- 实际重试次数、停止条件和真实疲劳记录。
- PC GUI 的任务入口和参数表单。

## 3. 输入契约

### 3.1 完整输入表

| 输入 | 类型 | 默认值 | 范围/格式 | 影响 |
|---|---|---|---|---|
| `all_plan` | int | `0` | `0` 或 `1` | 选择完整议价次数预算模式或自动分配模式 |
| `fatigue_budget` | int | `100` | `>=0` | 旅行疲劳与议价期望疲劳的总预算 |
| `cargo_capacity` | int | `650` | `>0` | 每段可装载商品数量 |
| `book_budget` | int | `0` | `>=0` | 整条路线共享的进货书数量 |
| `book_profit_threshold` | number | `0` | `>=0` | 第 N 本书相对第 N-1 本书的最低税后边际收益 |
| `negotiation_budget` | int | `0` | `>=0` | `all_plan=0` 时允许的满砍价/满抬价总次数 |
| `bargain_success_rates_bps` | list[int] | `[5000]` | 非空，每项 `0..10000` | 砍满所需期望疲劳 |
| `bargain_step_bps` | int | `1000` | `1..2000` | 每次砍价成功的幅度 |
| `raise_success_rates_bps` | list[int] | `[5000]` | 非空，每项 `0..10000` | 抬满所需期望疲劳 |
| `raise_step_bps` | int | `1000` | `1..2000` | 每次抬价成功的幅度 |
| `trade_level` | int | `20` | `1..20` | 兼容输入；不再影响议价 |
| `available_city_ids` | list[str] | PC 支持的 8 城 | 至少 2 个、不重复 | 限制精确规划参与的城市；必须包含当前城市 |
| `city_prestige` | object | 满 20 级 | 默认等级和城市 ID 覆盖 | 买卖税和购买数量 |
| `product_unlocks` | object | 全部解锁 | `all` 或 `only` | 限制可购买商品 |
| `active_events` | list | `[]` | 任意占位列表 | 当前忽略，非空时警告 |
| `use_fatigue_medicine` | bool | `false` | - | 自动执行设置，不改变规划公式 |
| `allowed_fatigue_medicines` | list | `[]` | - | 自动执行设置 |
| `fatigue_medicine_max_uses` | int | `4` | - | 自动执行设置 |

### 3.2 基点转换

后端只接受整数基点，不直接接受百分数字符串：

```text
10000 bps = 100%
5000 bps  = 50%
1170 bps  = 11.7%
1000 bps  = 10%
```

UI 可以显示百分比，但提交前必须乘以 100 并转换成整数。例如 `63% → 6300`、`11.7% → 1170`。

### 3.3 成功率序列

成功率序列按“已经成功的次数”读取，而不是按总尝试次数读取：

```yaml
bargain_success_rates_bps: [6300, 5300]
```

含义：

- 第一次成功之前，每次尝试成功率为 63%。
- 第一次成功后，下一成功阶段的每次尝试成功率为 53%。
- 失败只消耗疲劳，不推进序列。
- 如果还需要第三次成功，继续沿用最后一个值 53%。

### 3.4 `all_plan` 和完整议价计数

`negotiation_budget` 的单位已经从“底层尝试次数”变为“完整满议价操作次数”：

```text
买入前砍满一次 = 1
卖出前抬满一次 = 1
同一条边同时砍满和抬满 = 2
```

| 模式 | 议价次数约束 | 疲劳约束 |
|---|---|---|
| `all_plan=0` | `full_negotiation_used <= negotiation_budget` | 始终生效 |
| `all_plan=1` | 忽略 `negotiation_budget` | 始终生效 |

预算只是上限，求解器不要求用完。

## 4. 期望疲劳与利润计算

满幅度固定为 20%。需要的成功次数：

```text
required_successes = ceil(2000 / step_bps)
```

期望疲劳：

```text
expected_fatigue
= 8 × Σ(10000 / success_rate_bps[k])
```

默认配置：

```text
成功率 = 50%
单次成功幅度 = 10%
需要成功次数 = 2
期望尝试次数 = 1/0.5 + 1/0.5 = 4
期望疲劳 = 4 × 8 = 32
```

如果单次幅度为 11.7%，仍需要成功两次；价格调整最终封顶 20%，不会按 23.4% 计算。

如果任一必要阶段成功率为 0，达到满幅度的理论期望疲劳不可定义。求解器会移除对应的满砍价或满抬价选项，但仍可规划不议价路线，并在 `warnings` 中说明原因。

规划器不接收、存储或计算尝试次数上限，也不计算限定次数内达到 20% 的概率。选中满议价后：

- 利润直接按达到 20% 计算。
- 疲劳按上述理论期望值作为硬预算成本。
- `trade_level` 不参与这两个计算。

## 5. 返回字段迁移

### 5.1 破坏性字段映射

| 旧字段 | 新字段 | 说明 |
|---|---|---|
| `fatigue_used` | `expected_fatigue_used` | 可能为小数 |
| `remaining_fatigue` | `remaining_expected_fatigue` | 总预算减期望使用量 |
| `negotiation_used` | `full_negotiation_used` | 完整满操作数量，不是尝试数 |
| `bargain_attempts` | `bargain_to_cap` | 布尔执行意图 |
| `raise_attempts` | `raise_to_cap` | 布尔执行意图 |
| `negotiation_fatigue` | `expected_negotiation_fatigue` | 砍价与抬价的期望疲劳和 |
| `fatigue_cost` | `expected_fatigue_cost` | 旅行疲劳加议价期望疲劳 |

旧字段已经删除。执行器和 UI 不应同时兼容两套含义。

所有可能为分数的字段同时提供：

```text
expected_fatigue_used       用于普通展示，例如 27.7982
expected_fatigue_used_exact 用于调试和比较，例如 "1473680/53001"
```

### 5.2 成功规划示例

```json
{
  "status": "ok",
  "reason": null,
  "snapshot_id": "20260719-example",
  "all_plan": 1,
  "expected_profit": 144.0,
  "expected_profit_exact": "144",
  "fatigue_budget": 73,
  "expected_fatigue_used": 73.0,
  "expected_fatigue_used_exact": "73",
  "remaining_expected_fatigue": 0.0,
  "remaining_expected_fatigue_exact": "0",
  "books_budget": 0,
  "books_used": 0,
  "remaining_books": 0,
  "negotiation_budget": 0,
  "negotiation_budget_ignored": true,
  "full_negotiation_used": 2,
  "full_bargain_count": 1,
  "full_raise_count": 1,
  "remaining_negotiation": null,
  "city_path": ["起点城市", "终点城市"],
  "city_path_ids": ["1", "2"],
  "route": [
    {
      "from_city": "起点城市",
      "to_city": "终点城市",
      "from_city_id": "1",
      "to_city_id": "2",
      "buy_products": ["示例商品"],
      "buy_product_ids": ["p"],
      "buys": [
        {
          "product_id": "p",
          "product_name": "示例商品",
          "quantity": 1,
          "expected_unit_profit": 144.0,
          "expected_unit_profit_exact": "144"
        }
      ],
      "books_used": 0,
      "bargain_to_cap": true,
      "raise_to_cap": true,
      "full_negotiation_used": 2,
      "travel_fatigue": 9,
      "expected_bargain_fatigue": 32.0,
      "expected_bargain_fatigue_exact": "32",
      "expected_raise_fatigue": 32.0,
      "expected_raise_fatigue_exact": "32",
      "expected_negotiation_fatigue": 64.0,
      "expected_negotiation_fatigue_exact": "64",
      "expected_fatigue_cost": 73.0,
      "expected_fatigue_cost_exact": "73",
      "expected_profit": 144.0,
      "expected_profit_exact": "144"
    }
  ],
  "assumptions": {
    "rule_schema_version": "2.0.0",
    "rule_model_version": "resonance_pc_trade_binary_to_cap_2026_07_19",
    "rounding_mode": "half_toward_positive_infinity",
    "market_snapshot_frozen": true,
    "crew_effects_included": false,
    "active_events_included": false,
    "cash_constraint_included": false,
    "unit_cargo_size": true,
    "repeat_city_purchase_available": true,
    "negotiation_model": "binary_to_cap_expected_fatigue",
    "negotiation_cap_bps": 2000,
    "negotiation_attempt_fatigue": 8,
    "negotiation_attempt_limit_included": false,
    "negotiation_profit_assumes_cap_reached": true,
    "expected_fatigue_is_hard_budget_cost": true,
    "trade_level": 20,
    "trade_level_affects_negotiation": false,
    "all_plan": 1,
    "bargain_profile": {
      "success_rates_bps": [5000],
      "step_bps": 1000,
      "required_successes": 2,
      "expected_fatigue": 32.0,
      "expected_fatigue_exact": "32"
    },
    "raise_profile": {
      "success_rates_bps": [5000],
      "step_bps": 1000,
      "required_successes": 2,
      "expected_fatigue": 32.0,
      "expected_fatigue_exact": "32"
    },
    "tax_applied_to_buy_and_sell_amounts": true
  },
  "warnings": [
    "trade rule metadata is versioned but still requires validation against game samples"
  ]
}
```

消费者应忽略未来新增的假设字段，但不能忽略已经存在且值为 `false` 的模型边界字段。

### 5.3 无可用路线示例

```json
{
  "status": "no_plan",
  "reason": "no_positive_profit_route",
  "snapshot_id": "20260719-example",
  "all_plan": 0,
  "expected_profit": 0.0,
  "expected_profit_exact": "0",
  "fatigue_budget": 100,
  "expected_fatigue_used": 0.0,
  "expected_fatigue_used_exact": "0",
  "remaining_expected_fatigue": 100.0,
  "remaining_expected_fatigue_exact": "100",
  "books_budget": 0,
  "books_used": 0,
  "remaining_books": 0,
  "negotiation_budget": 0,
  "negotiation_budget_ignored": false,
  "full_negotiation_used": 0,
  "full_bargain_count": 0,
  "full_raise_count": 0,
  "remaining_negotiation": 0,
  "city_path": ["当前城市"],
  "city_path_ids": ["1"],
  "route": [],
  "assumptions": {
    "rule_schema_version": "2.0.0",
    "rule_model_version": "resonance_pc_trade_binary_to_cap_2026_07_19",
    "rounding_mode": "half_toward_positive_infinity",
    "market_snapshot_frozen": true,
    "crew_effects_included": false,
    "active_events_included": false,
    "cash_constraint_included": false,
    "unit_cargo_size": true,
    "repeat_city_purchase_available": true,
    "negotiation_model": "binary_to_cap_expected_fatigue",
    "negotiation_cap_bps": 2000,
    "negotiation_attempt_fatigue": 8,
    "negotiation_attempt_limit_included": false,
    "negotiation_profit_assumes_cap_reached": true,
    "expected_fatigue_is_hard_budget_cost": true,
    "trade_level": 20,
    "trade_level_affects_negotiation": false,
    "all_plan": 0,
    "bargain_profile": {
      "success_rates_bps": [5000],
      "step_bps": 1000,
      "required_successes": 2,
      "expected_fatigue": 32.0,
      "expected_fatigue_exact": "32"
    },
    "raise_profile": {
      "success_rates_bps": [5000],
      "step_bps": 1000,
      "required_successes": 2,
      "expected_fatigue": 32.0,
      "expected_fatigue_exact": "32"
    },
    "tax_applied_to_buy_and_sell_amounts": true
  },
  "warnings": [
    "trade rule metadata is versioned but still requires validation against game samples"
  ]
}
```

### 5.4 自动任务执行能力

自动任务已支持 `all_plan=1` 和非零 `negotiation_budget` 产生的完整议价路线，不再使用
`negotiation_execution_not_implemented` 提前拒绝。执行器只消费路线中的两个布尔意图，
使用独立的买入/卖出 `20.0%` 模板确认是否到顶；议价失败会抛出明确业务错误，且不会继续成交或旅行。

## 6. 执行器对接契约

### 6.1 两个布尔字段的归属

每条路线边表示：在 `from_city` 买入本段货物，前往 `to_city`，然后在 `to_city` 卖出本段货物。

```text
bargain_to_cap 作用于 from_city 的本段买入
raise_to_cap   作用于 to_city 的本段卖出
```

执行器固定时序：

```text
到达/位于 from_city
→ 卖出上一段货物
→ 若 bargain_to_cap=true，执行本段买入砍价
→ 按 buys 购买本段货物
→ 前往 to_city
→ 若 raise_to_cap=true，在卖出本段货物前执行抬价
→ 卖出本段货物
```

当前自动流程在中间城市会把“卖出上一段”和“买入下一段”放在同一次城市商店操作中。接入时必须保存上一条边的 `raise_to_cap`：

- 到达中间城市后，先使用上一条边的 `raise_to_cap` 决定是否抬价并卖出上一段货物。
- 完成卖出后，使用当前下一条边的 `bargain_to_cap` 决定是否砍价并买入下一段货物。
- 不得把当前下一条边的 `raise_to_cap` 用于刚到达城市的上一段卖出。
- 到达最终城市后，终点清仓必须使用最后一条边的 `raise_to_cap`。

### 6.2 执行器职责

规划器只下发两个布尔意图，不下发底层尝试次数。执行器负责：

- 打开正确的砍价或抬价界面。
- 使用页面专属模板判断是否已经达到 20%。
- 在总超时内重复点击并等待议价动画结束。
- 在没有执行议价或没有达到执行器目标时返回明确业务失败，不能静默当作成功。

推荐为实际执行结果增加独立字段，但不要覆盖规划字段：

```json
{
  "requested_to_cap": true,
  "completed_to_cap": true,
  "detection_method": "template",
  "cap_confidence": 0.97,
  "elapsed_ms": 12000,
  "failure_reason": null
}
```

规划器不会根据实际执行数据中途重算路线。若未来需要实际疲劳偏差后的重规划，应设计新的任务模式，不能在当前冻结行情契约中静默加入。

## 7. UI 对接说明

### 7.1 当前 GUI 状态

`packages/resonance_gui` 已提供独立 PC 跑商页，固定使用：

```text
game_name = resonance_pc
preview_task_ref = tasks:preview_trade_plan_pc.yaml:preview_trade_plan_pc
run_task_ref = tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc
```

任务使用 `wait=false` 派发，GUI 保存 CID 后轮询运行详情，并通过现有 EventBus/UI queue
消费 `task.resonance_pc_trade_progress`。独立预计算任务使用用户选择的 `start_city_id`，只刷新市场
并调用同一精确规划器；它不依赖窗口、截图、OCR 或输入服务，也不执行任何游戏操作。刷新失败但
存在本地快照时，结果以 `market_source=fallback_cache` 明确标记。正式运行仍会识别游戏实际城市、
刷新行情并计算路线；GUI 在新的规划完成事件到达后用正式运行方案替换预计算方案。

进度 payload 使用 `resonance_pc.trade_progress.v1`，稳定字段为
`cid/sequence/stage/state`。GUI 必须按 CID 过滤，并按 sequence 去重，不能把进度事件当作最终任务结果。

### 7.2 UI 控件规则

- `all_plan` 使用 0/1 选择控件。
- `start_city_id` 使用常用参数区的单选城市控件，并且必须属于 `available_city_ids`。
- `all_plan=1` 时禁用 `negotiation_budget`，或明确显示“该字段已忽略”。
- 成功率应允许多阶段列表；UI 显示百分比，提交整数基点列表。
- 幅度可显示带一位或两位小数的百分比，提交整数基点。
- 普通展示使用数值疲劳字段，调试详情显示 `_exact` 分数字段。
- 当前方案概览显示预计收益、预计疲劳、疲劳收益比、路线规模、剩余疲劳、书籍和协商次数。
- 路线状态列只显示待执行、进行中、完成、阻断和失败图标，并通过 tooltip 提供文字说明。
- `warnings` 必须展示给用户，特别是活动未实现和 0 成功率导致满议价不可用。
- 自动任务执行结果中的议价失败必须作为业务失败展示，不能显示为普通无收益或路径计算失败。
- 正常参数使用类型化控件；原始输入、进度事件和最终结果只在折叠调试区显示。
- 取消操作必须在取得 CID 后立即可用，并继续等待任务进入真实终态。

## 8. 错误、限制与验收

### 8.1 主要状态与错误

| 状态/错误 | 含义 |
|---|---|
| `status=ok` | 找到模型内正利润路线 |
| `no_positive_profit_route` | 当前预算和模型下没有正利润终止状态 |
| `invalid_optimal_route_input` | 输入类型、范围或成功率序列非法 |
| `insufficient_selected_cities` | 参与规划城市少于两个 |
| `unsupported_selected_cities` | 选择了 PC 操作链尚未支持的城市 |
| `current_city_not_selected` | 当前所在城市不在参与规划城市中 |
| `trade_rules_missing` | 版本化规则文件缺失 |
| `trade_rules_invalid` | 规则 schema、模型或默认参数非法 |
| `negotiation_button_not_found` | 交易页面未找到对应砍价或抬价按钮 |
| `negotiation_animation_start_timeout` | 点击后议价动画未开始 |
| `negotiation_animation_finish_timeout` | 议价动画未在时限内返回交易页面 |
| `negotiation_page_lost` | 议价期间目标交易页面无法截图 |
| `negotiation_cap_detection_timeout` | 总超时内未确认达到 20.0% |

`active_events` 非空和必要成功阶段为 0 不会让整个规划调用失败，而是通过 `warnings` 明确说明被忽略或对应选项不可用。

### 8.2 当前模型不包含

- 乘员及其技能。
- 活动实际效果。
- 玩家本金。
- 商品不同货舱占用。
- 途中行情变化。
- 实际重试次数和真实疲劳随机波动。
- 中途根据实际执行结果重新规划。

### 8.3 当前验证记录

实施时执行了：

```powershell
python -m pytest tests/test_resonance_pc_trade_negotiation_actions.py tests/test_resonance_pc_trade_flow_execution.py tests/test_resonance_pc_auto_cycle_trade_flow.py tests/test_resonance_pc_auto_cycle_trade_task.py -q
# 33 passed

python -m pytest tests -q -k "resonance_pc or resonance_gui"
# 156 passed, 309 deselected

python -m packages.aura_core.cli.package_cli sync plans/resonance_pc
# Manifest synced

python -m packages.aura_core.cli.package_cli check plans/resonance_pc
# Manifest is up to date

python -m packages.aura_core.cli.package_cli validate plans/resonance_pc
# Manifest validation passed

python tools/plan_doctor.py --plan resonance_pc
# errors=0 warnings=0 infos=0
```

计划包合规检查最终结果为 `errors=21 warnings=0`。21 项均来自本次范围外、工作树中已有的 `auto_battle_dispatch_pc.yaml`：该文件使用 `aura.run_task`，但合规检查器把它报告为当前包未导出的本地动作。跑商任务、GUI 进度事件、求解器、规则文件和本次迁移文档没有产生新的合规错误或警告。

### 8.4 执行器接入检查清单

- [x] 买入砍价使用当前边 `bargain_to_cap`。
- [x] 卖出抬价使用上一条已完成旅行边的 `raise_to_cap`。
- [x] 终点清仓使用最后一条边的 `raise_to_cap`。
- [x] 未执行或未达到目标时不静默成功。
- [x] 执行结果记录模板确认状态和耗时，不覆盖规划期望值。
- [x] 无议价路线保持原有行为。

### 8.5 UI 接入检查清单

- [x] Runner 使用 `game_name=resonance_pc`。
- [x] 自动任务使用规范 task ref。
- [x] 规划完成后通过任务进度事件展示冻结路线。
- [x] 成功率以整数基点列表提交并校验范围。
- [x] `all_plan=1` 时禁用 `negotiation_budget`。
- [x] 显示规划警告、预计疲劳和议价执行失败状态，精确原始值保留在调试详情。
- [x] Runner 非阻塞派发，取消请求不伪装成任务终态。
