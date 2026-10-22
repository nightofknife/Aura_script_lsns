# 任务操作与开发工具精简清单（重构前基线）

基准日期：2026-10-21。基准提交：`7e9e5657`，与本轮已拉取的 `origin/main` 一致。

> 本文保留重构前的盘点证据，不再作为当前实现说明。用户随后批准了 Codex 与 Astra 修订的层级，并明确取消旧版兼容：Android/MuMu、旧工作台、旧索引和别名、重复包装及历史研究链退出；当前架构见 [repository-architecture.md](repository-architecture.md)，开发入口见 [resonance-development.md](resonance-development.md)。下文关于保留 Android、兼容入口和“尚未改代码”的描述仅属于盘点当时的状态。

建议保留货运、客运、战斗和活动玩法等业务能力，统一用户任务目录与编排边界；将导航、买卖、恢复、扫描等步骤归入内部操作；将校准、素材生产、研究和诊断归入开发工具。优先精简重复入口、包装层和多处维护的契约。

本清单给出处置建议，不代表这些调整已经实施。范围覆盖当前 GUI 入口、PC Plan 全部 YAML 任务、业务操作能力组、旧 Android Plan 和开发维护工具。文档中尚未实现的整顿中心历史规划不计入现有能力；Python 私有辅助函数按所属能力组归类，不逐个当成任务。

## 处置含义与目标层级

| 标记 | 含义 | 保留边界 |
| --- | --- | --- |
| 保留 | 保留该能力及其明确职责 | 不要求保留现有文件布局或重复参数声明 |
| 合并 | 统一入口、参数、结果或编排骨架 | 保留业务差异、预算规则、停止条件和必要检查；不是机械合成一个大文件 |
| 降级 | 从普通任务入口调整为内部操作或开发工具 | 保留实现与调用关系；不自动代表移出发布包 |
| 移除 | 移除本行明确指出的旧注册、页面壳、重复包装或候选实现 | 涉及实现删除时，必须先迁移本行列出的依赖；不依据文件名或静态引用数直接删除 |

| 层级 | 职责 | 示例 |
| --- | --- | --- |
| 用户流程 | 安排任务顺序、共享预算、交接及失败策略 | 启动、货运、客运、战斗、关闭 |
| 用户任务 | 完成一个用户可理解的业务目标 | 自动货运、生产收菜、深潜多局 |
| 内部操作 | 在明确前置状态下完成一个可复用步骤，并返回业务结果 | 前往城市、打开房间、领取成品、结算退出 |
| 基础能力 | 提供识别、输入、数据、规划算法和运行支持 | 截图、模板匹配、点击、行情缓存、路线求解 |

货运试算、配队推荐、用户数据查看属于用户可用的辅助工具，不因其不操作游戏而归入开发工具。YAML 和 Python 是实现方式，不用来决定任务层级。

## 用户入口清单

### 当前普通界面入口

当前主导航为工作流程、独立功能、设置。工作流程有五项，独立功能有五项；下表是实际界面入口，不与后文 YAML 定义数量相加。

| 编号 | 当前入口 | 当前能力 | 建议 | 目标位置与说明 |
| --- | --- | --- | --- | --- |
| U01 | 工作流程／进入主界面 | `game_startup_pc.yaml:enter_main` | 保留 | 流程生命周期步骤，也供其他任务准备起点 |
| U02 | 工作流程／货运 | `auto_cycle_trade_pc` | 保留 | 用户任务；规划、旅行、买卖、恢复等归内部操作 |
| U03 | 工作流程／客运 | `auto_passenger_trips_pc` | 保留 | 用户任务；支持单独运行和流程编排 |
| U04 | 工作流程／自动战斗 | `auto_battle_dispatch_pc` | 保留 | 用户任务；任务单统一配置，内部分类与批次不再平铺为普通任务 |
| U05 | 工作流程／关闭游戏 | `game_startup_pc.yaml:close_game` | 保留 | 流程生命周期步骤，保留停止与收尾语义 |
| U06 | 独立功能／刷新用户数据 | `player_data_refresh` | 保留 | 用户数据工具；读取能力同时供货运准备等流程复用 |
| U07 | 独立功能／货运试算 | `preview_trade_plan_pc` | 合并 | 归货运的预览模式，共享参数契约，保留只计算、不操作游戏的功能 |
| U08 | 独立功能／配队推荐 | `team_recommendation_pc` | 保留 | 用户工具；根据缓存匹配推荐，不表示已实现自动配置队伍 |
| U09 | 独立功能／识海深潜 | 进入、完整循环、关卡运行、扫描、离线规划、移动测试 | 合并 | 统一玩法入口；保留完整循环与从当前关卡继续的不同模式，开发按钮另行归类 |
| U10 | 独立功能／无垠乱斗 | `eternal_scuffle_pc` | 保留 | 用户任务；后续可接入流程目录，不新增第二套执行队列 |

查看数据快照、刷新目标窗口、校验战斗任务单、保存参数、启动／停止、查看报告和选择文件保留为所属功能的辅助操作，不单独增加用户任务。

依据：[工作流程目录](../../packages/resonance_gui/widgets/workflow_page.py)、[独立功能目录](../../packages/resonance_gui/widgets/small_tasks_page.py)、[GUI 任务引用](../../packages/resonance_gui/logic.py)。

### 已实现但尚未接入普通 GUI 的业务任务

以下是已有 YAML 能力，不表示当前普通菜单已经展示。保留业务身份也不表示本轮增加 GUI 入口。

| 编号 | 能力 | 现有任务 | 建议 | 边界 |
| --- | --- | --- | --- | --- |
| U11 | 收容囚犯 | `admit_prisoners_pc` | 保留 | 用户任务；包含进入整顿中心与一键收容 |
| U12 | 原料车间维护 | `maintain_raw_material_workshops_pc` | 保留 | 四间原料车间的业务任务；单车间维护作为内部复用单元 |
| U13 | 生产车间补人 | `fill_product_workshop_staff_pc` | 保留 | 用户任务；当前参数只包含 1、2 号生产车间，不扩称覆盖全部车间 |
| U14 | 生产车间收菜 | `collect_product_workshops_pc` | 保留 | 用户任务；按 1～6 号开放状态领取，保留仓满停止与返回条件 |

黑月采购、投资、气泡水、便当、垃圾回收、拖车已有独立 YAML 包装，但当前主要随货运或内部流程使用，先按内部业务操作管理，详见后文；不据 `entry_point: true` 自动新增普通菜单。

### 识海深潜入口重划分

| 现有功能 | 当前入口 | 建议 | 保留内容与依赖 |
| --- | --- | --- | --- |
| 开始下潜 | 普通可见 | 降级 | 作为进入活动操作；完整循环仍调用入场任务 |
| 开始完整循环 | 普通可见 | 保留 | 作为主任务；现有循环使用随机单局运行器 |
| 关卡内自动运行 | 普通可见 | 合并 | 作为同一玩法的继续运行模式；当前采用规划运行器，不能直接当成现有循环的等价替代 |
| 测试移动循环 | 普通可见 | 降级 | 手动按钮归开发工具；`single_run_test` 实现仍被正式循环调用，应先改名归位 |
| 扫描魔方布局与打开报告 | 普通可见 | 降级 | 手动入口归诊断工具，扫描实现仍供规划运行器使用 |
| 离线生成移动方案与打开方案 | 普通可见 | 降级 | 归开发规划工具，算法仍供正式关卡运行复用 |
| 四视角素材采集 | 隐藏面板仍构建 | 降级 | 显式开发入口、按需构建，保留采集链 |
| 灵敏度及拖距探针 | 部分隐藏、部分仅 YAML 入口 | 合并 | 统一参数化校准工具，保留单次与多次采集模式 |
| 节点卡只读探针 | YAML 入口 | 降级 | 开发诊断，不作为用户玩法任务 |

依赖链：[完整循环](../../plans/resonance_pc/tasks/consciousness_deep_dive_loop_pc.yaml) → 入场 → `single_run_test` → 收尾；[规划运行器](../../plans/resonance_pc/src/actions/consciousness_deep_dive_planned_run_pc_actions.py)直接复用扫描与规划模块。

## PC YAML 全量任务清单

`plans/resonance_pc/tasks/` 共 39 个 YAML、64 个顶层任务定义。其中 `entry_point` 为 true 的 40 个、false 的 12 个、未声明的 12 个。该字段用于运行入口发现，与普通 GUI 展示不是同一契约。

表中 EP：`T` 为 true，`F` 为 false，`—` 为未声明。单任务表的任务 key 与文件名去掉 `.yaml` 相同。处置以目标身份为准，“内部保留”不要求将所有 YAML 都改写为 Python。

### 单任务文件 32 项

以下文件均位于 [PC tasks](../../plans/resonance_pc/tasks/)，EP 均为 T。

| 文件与任务 key | 目标身份 | 建议 | 当前用途与迁移边界 |
| --- | --- | --- | --- |
| `admit_prisoners_pc.yaml` | 用户任务 | 保留 | 一键收容，尚无普通 GUI 入口 |
| `auto_battle_combat_pc.yaml` | 内部操作 | 降级 | 已进入战斗后的执行；战斗调度多处复用 |
| `auto_battle_input_preview_pc.yaml` | 内部校验 | 合并 | 并入战斗配置预览；GUI 引用迁移后再取消独立入口 |
| `auto_combined_commerce_pc.yaml` | 内部组合流程 | 合并 | 统一到流程编排策略；保留共享疲劳预算与交接检查，不能用普通顺序运行简单替代 |
| `auto_cycle_trade_pc.yaml` | 用户任务 | 保留 | 完整货运；与预览、组合商贸共用输入和业务接口 |
| `auto_passenger_trips_pc.yaml` | 用户任务 | 保留 | 两城交替客运；组合商贸直接调用其 Python flow |
| `bento_consumption_pc.yaml` | 内部恢复操作 | 降级 | 货运结束处理仍调用；可保留高级手动工具 |
| `black_moon_local_prepare_pc.yaml` | 内部采购操作 | 合并 | 与完整采购共用准备阶段，取消重复任务身份 |
| `black_moon_local_purchase_pc.yaml` | 内部采购子流程 | 保留 | 货运通过 `aura.run_task` 调用；保留按周记录、选品确认和页面恢复 |
| `black_moon_local_select_pc.yaml` | 内部选品操作 | 合并 | 合并选品／预览能力，保留可选提交与退出语义 |
| `cape_island_investment_pc.yaml` | 内部到站操作 | 降级 | 货运直接复用其 action；无 YAML 调用不等于实现可删 |
| `collect_product_workshops_pc.yaml` | 用户任务 | 保留 | 收菜，调用 `open_reforming_center_room_pc` |
| `consciousness_deep_dive_cleanup_pc.yaml` | 内部收尾操作 | 降级 | 完整循环依赖；从结算返回活动首页 |
| `consciousness_deep_dive_entry_card_probe_pc.yaml` | 开发诊断 | 降级 | 只读节点卡识别 |
| `consciousness_deep_dive_pc.yaml` | 内部入场操作 | 降级 | 实际只负责入场；名称应突出进入操作 |
| `consciousness_deep_dive_plan_pc.yaml` | 开发规划工具 | 降级 | 离线规划与报告；保留正式运行复用的算法 |
| `consciousness_deep_dive_planned_run_pc.yaml` | 玩法运行模式 | 合并 | 归玩法的关卡继续模式；规划单局与随机单局实现分开保留 |
| `consciousness_deep_dive_scan_pc.yaml` | 内部扫描与开发诊断 | 降级 | 收起普通按钮，保留扫描实现与报告能力 |
| `consciousness_deep_dive_sensitivity_probe_pc.yaml` | 开发校准 | 合并 | 单次 320px 探针与拖距工具统一入口 |
| `consciousness_deep_dive_single_run_test_pc.yaml` | 内部单局操作 | 降级 | 保留并更名；正式循环仍以 `round_budget=20` 调用 |
| `enter_reforming_center_pc.yaml` | 内部导航 | 降级 | 从主界面进入整顿中心，保留独立起点契约 |
| `fill_product_workshop_staff_pc.yaml` | 用户任务 | 保留 | 生产车间补人，调用共享房间操作 |
| `maintain_raw_material_workshop_pc.yaml` | 内部单车间流程 | 降级 | 全车间维护复用，保留按房间参数运行能力 |
| `maintain_raw_material_workshops_pc.yaml` | 用户任务 | 保留 | 四车间维护，确认返回总览后再推进 |
| `navigate_reforming_center_room_pc.yaml` | 内部导航 | 合并 | 现为 OCR 导航，先进入总览再点击；与模板打开房间的起终点不同，不直接互换 |
| `open_reforming_center_room_pc.yaml` | 内部导航 | 降级 | 当前收菜、补人、原料维护实际复用的模板接口；调用方负责总览起点 |
| `preview_trade_plan_pc.yaml` | 用户辅助工具 | 合并 | 归货运预览模式，保留只计算与指定起点能力 |
| `rubbish_recycling_pc.yaml` | 内部到站操作 | 降级 | 货运直接复用 action，保留城市与页面条件 |
| `sparkling_water_pc.yaml` | 内部恢复操作 | 降级 | 货运复用 action；保留免费次数、杯数与页面恢复条件 |
| `team_recommendation_pc.yaml` | 用户辅助工具 | 保留 | 根据缓存提供配队推荐 |
| `trade_goods_investment_pc.yaml` | 内部投资操作 | 降级 | 货运通过 `aura.run_task` 实际调用，保留按解锁顺序投资 |
| `use_trailer_service_pc.yaml` | 内部出发操作 | 降级 | 旅行流程复用 action；只确认拖车出发，不负责等待到站 |

### 战斗调度文件 12 项

文件：[auto_battle_dispatch_pc.yaml](../../plans/resonance_pc/tasks/auto_battle_dispatch_pc.yaml)。内部调用均来自本文件的上级任务；“合并批次骨架”保留类别差异和连续同关卡分组。

| 任务 key | EP | 目标身份 | 建议 | 保留语义 |
| --- | --- | --- | --- | --- |
| `auto_battle_ct_tie_an_run_one_pc` | — | 内部操作 | 降级 | 单条铁安作业 |
| `auto_battle_ct_tie_an_batch_pc` | — | 内部批次 | 合并 | 铁安参数与顺序 |
| `auto_battle_ct_regional_ops_run_one_pc` | — | 内部操作 | 降级 | 单条区域作战中心作业 |
| `auto_battle_ct_regional_ops_batch_pc` | — | 内部批次 | 合并 | 区域参数与顺序 |
| `auto_battle_ct_batch_pc` | — | 内部分发流程 | 保留 | CT 内铁安／区域分类 |
| `auto_battle_gp_action_summary_run_difficulty_pc` | — | 内部操作 | 降级 | 当前关卡执行一个难度 |
| `auto_battle_gp_action_summary_run_group_pc` | — | 内部分组流程 | 保留 | 同关卡连续难度，避免重复进入 |
| `auto_battle_gp_action_summary_batch_pc` | — | 内部批次 | 合并 | 行动汇总关卡分组 |
| `auto_battle_gp_structural_run_one_pc` | — | 内部操作 | 降级 | 单条构析勘探目标 |
| `auto_battle_gp_structural_batch_pc` | — | 内部批次 | 合并 | 构析勘探顺序 |
| `auto_battle_gp_batch_pc` | — | 内部分发流程 | 保留 | GP 内类别分发 |
| `auto_battle_dispatch_pc` | T | 用户任务 | 保留 | 终端入口、任务单规范化、类别调度 |

### 无垠乱斗文件 9 项

文件：[eternal_scuffle_pc.yaml](../../plans/resonance_pc/tasks/eternal_scuffle_pc.yaml)。已有 `entry_point: false` 的内部操作无需再解释为公开用户任务。

| 任务 key | EP | 目标身份 | 建议 | 保留语义 |
| --- | --- | --- | --- | --- |
| `eternal_scuffle_pc` | T | 用户任务 | 保留 | 多局编排与用户进度 |
| `scuffle_checked_round_pc` | F | 内部检查包装 | 合并 | 将 checkpoint 并入单局返回边界，不能丢失失败检查 |
| `scuffle_checked_pair_pc` | F | 内部检查包装 | 合并 | 将 checkpoint 并入选人配装边界 |
| `scuffle_checked_advance_pc` | F | 内部检查包装 | 合并 | 将 checkpoint 并入阶段推进边界 |
| `scuffle_round_pc` | F | 内部单局流程 | 保留 | 单局阶段循环与结算 |
| `scuffle_draft_pair_pc` | F | 内部操作 | 保留 | 一人一装备 |
| `scuffle_advance_pc` | F | 内部操作 | 保留 | 推进一阶段，必要时处理战利品 |
| `scuffle_loot_pc` | F | 内部操作 | 保留 | 战利品选择与分配 |
| `scuffle_settle_pc` | F | 内部操作 | 保留 | 开箱和返回 |

### 其他多任务文件 11 项

| 文件 | 任务 key | EP | 目标身份 | 建议与边界 |
| --- | --- | --- | --- | --- |
| `consciousness_deep_dive_capture_pc.yaml` | `consciousness_deep_dive_capture_drag_chunk_pc` | F | 采集内部操作 | 合并有界拖动步骤，保留等待 |
| 同上 | `consciousness_deep_dive_capture_view_pc` | F | 采集内部操作 | 保留视角偏移与截图 |
| 同上 | `consciousness_deep_dive_capture_pc` | T | 开发采集工具 | 降级，采集四个标定视角 |
| `consciousness_deep_dive_drag_distance_probe_pc.yaml` | `consciousness_deep_dive_drag_distance_probe_round_pc` | F | 校准内部操作 | 合并到统一拖动校准工具 |
| 同上 | `consciousness_deep_dive_drag_distance_probe_pc` | T | 开发校准工具 | 合并单次／100 次采集模式 |
| `consciousness_deep_dive_loop_pc.yaml` | `consciousness_deep_dive_loop_pc` | T | 用户任务 | 保留完整循环 |
| 同上 | `deep_dive_cycle_pc` | F | 内部单局流程 | 保留入场、随机单局、收尾与阶段检查 |
| `game_startup_pc.yaml` | `enter_main` | T | 生命周期步骤 | 保留启动和恢复主界面 |
| 同上 | `close_game` | T | 生命周期步骤 | 保留目标游戏关闭 |
| `player_data_pc.yaml` | `player_data_refresh` | T | 用户数据工具 | 保留数据刷新，供业务任务复用 |
| 同上 | `player_data_get_latest` | — | 内部查询 | 降级为缓存查询，GUI 有实际调用 |

## 内部操作与基础能力清单

以下按业务能力分组，包含没有单独 YAML 包装的 Python 实现。源文件默认位于 [PC actions](../../plans/resonance_pc/src/actions/)。名称缩写仅用于表格，不表示拟新增这些类或目录。

| 编号 | 能力组与现有实现 | 建议 | 目标边界与保留理由 |
| --- | --- | --- | --- |
| O01 | 启停与分辨率准备：`game_startup_pc_actions.py`、`runtime_preflight_pc_actions.py` | 保留 | 生命周期与执行前置；统一复用，不在各入口重复实现 |
| O02 | 主界面、城市面板、商店入口、返回：`city_trade_flow_pc_actions.py` 的导航 actions、`_city_panel_vision.py`、`_city_shop_entry.py` | 合并 | 从大货运 flow 中明确导航接口，供客运与到站操作复用 |
| O03 | 地图选点、出发、等待到站：`city_travel_pc_actions.py`、`_intercity_map_drag.py`、`_intercity_map_geometry.py`、`_depart_button_vision.py` | 保留 | 选择目的地、确认出发、到站各有独立结果，不折叠成一个含糊成功值 |
| O04 | 买货、卖货、砍价抬价、进货书：货运 flow 中的 buy/sell、`trade_negotiation_pc_actions.py`、`purchase_book_pc_actions.py`、`_trade_buy_selection.py`、`_trade_sell_state.py` | 保留 | 商贸业务操作，保留数量、资源消耗和失败阶段结果 |
| O05 | 行情刷新、缓存、快照、商品查询：`market_data_pc_actions.py` 与行情 service | 合并 | 归行情数据服务及诊断查询，不作为普通业务任务平铺 |
| O06 | 路线规划、循环执行状态、模拟：`trade_planner_pc_actions.py`、trade planner/solver services | 合并 | 单一输入和结算契约；求解器是基础能力，simulate 是开发入口，不直接删除旧求解分支 |
| O07 | 货运、客运、组合商贸大 flow：`city_trade_flow_pc_actions.py`、`passenger_flow_pc_actions.py`、`combined_commerce_pc_actions.py` | 合并 | 统一流程边界，保留共享预算、资源恢复及交接；避免 YAML 与 Python 各维护一套编排 |
| O08 | 客运管理、传单揽客、进城结算：`passenger_pc_actions.py` | 保留 | 客运内部业务操作，与货运共用旅行及可选倒货 |
| O09 | 资料、角色、仓库、装备扫描：`player_data_pc_actions.py`、`character_pc_actions.py`、`inventory_pc_actions.py`、`_player_data_persistence.py` | 合并 | 统一数据采集与缓存接口，保留各页面识别实现；用户刷新和配队均依赖 |
| O10 | 疲劳恢复读取、便当识别与消费：`player_recovery_pc_actions.py`、`love_bento_pc_actions.py`、`bento_consumption_pc_actions.py`、`bento_auto_recovery_pc_actions.py`、`_bento_*`、`_freight_recovery_policy.py` | 合并 | 统一恢复策略与结果，保留实时读取及库存记录；自动消费直接 import 旧 consumption 会话与布局，不能整文件删除 |
| O11 | 气泡水：`sparkling_water_pc_actions.py`、`_sparkling_water_policy.py` | 保留 | 内部恢复操作，用户配置归货运恢复选项 |
| O12 | 蜃息岛投资、交易品投资、垃圾回收：对应 `cape_island_investment`、`trade_goods_investment`、`rubbish_recycling` actions 及记录／视觉模块 | 保留 | 到站业务操作，共用执行接口，但保留城市、资源与次数限制 |
| O13 | 拖车：`trailer_service_pc_actions.py`、`_trailer_service_vision.py`、`_freight_trailer_policy.py` | 保留 | 旅行的出发方式；费用、次数、出发确认与到站等待分开 |
| O14 | 黑月准备、选择、提交、等待退出、周记录：`black_moon_local_prepare/select/exit/visit_pc_actions.py`、`_black_moon_*` | 合并 | 一套采购流程与输入契约，阶段操作仍可复用；保留周记录和选中证据 |
| O15 | 黑月旧一体化公开入口：`black_moon_local_shop_pc_actions.py` 的 `purchase_black_moon_local_from_rest_menu` | 移除候选 | 当前采购主链走 visit/select/exit 分段能力；先核对公开 action 消费者、复用实现，再退出旧入口；不是删全部黑月代码 |
| O16 | 战斗单规范化、分组、选择、编队、战斗结算：`battle_dispatch_pc_actions.py` | 合并 | 归任务单模型与战斗操作；批次复用骨架，保留各玩法选择差异 |
| O17 | 整顿中心入场、收容、OCR 房间定位：`reforming_center_pc_actions.py` | 保留 | 先区分入场、收容、历史导航；新模板接口不能替代收容逻辑 |
| O18 | 模板房间定位与进入：`reforming_center_navigation_pc_actions.py`、`_reforming_center_*` | 合并 | 与历史 OCR 导航统一能力命名及起终点契约，当前模板接口为维护任务实际依赖 |
| O19 | 原料车间观察、预存、工作／替补补人、开工、返回：`raw_workshop_pc_actions.py`、`raw_workshop_chain_pc_actions.py`、`_raw_workshop_*` | 保留 | 操作保留明确条件和有界循环，单车间／四车间编排分开 |
| O20 | 生产补员步骤与成品领取：`product_workshop_staff_pc_actions.py`、`product_collection_pc_actions.py`、对应 UI helpers | 保留 | 用户任务复用这些操作，保留保存确认、开放状态、仓满停止 |
| O21 | 生产车间旧快填：`product_workshop_pc_actions.py`、`_product_workshop_ui.py` | 移除候选 | 当前补人 YAML 使用 staff actions；先明确快填与新补员语义差异及公开 action 消费，再决定退出，不能只因无 YAML 引用删除 |
| O22 | 深潜入场、单局、结算与多局生命周期：对应 `consciousness_deep_dive_pc/single_run/cleanup/loop_pc_actions.py` | 合并 | 玩法主入口统一，保留随机单局与规划单局两种已有实现及收尾检查 |
| O23 | 深潜扫描、布局、规划、定向行动、事件与漩涡处理：`consciousness_deep_dive_scan/plan/planned_run_pc_actions.py`、`_deep_dive_*` | 保留 | 正式关卡运行依赖；只降级手动扫描／离线规划入口，不按私有文件名或研究相似命名批量删除 |
| O24 | 深潜采集、拖距、节点卡探针：对应 `capture/drag_probe/entry_card_probe_pc_actions.py` | 降级 | 开发采集与诊断；共用正式截图／识别底座，保留单独工具边界 |
| O25 | 无垠乱斗选人、装备、队长、阶段推进、战利品、结算：`eternal_scuffle_pc_actions.py`、`_eternal_scuffle_*` | 保留 | 保留操作与局部状态机；只合并重复 checked 包装身份 |
| O26 | 配队推荐：`team_recommendation_pc_actions.py` | 保留 | 用户辅助工具的纯数据能力，不扩为自动点击配队 |
| O27 | 业务进度事件：`_operation_progress.py`、`_deep_dive_runtime_events.py` 等 | 合并 | 统一事件形状和归属，保留各玩法必要业务字段与取消响应 |

### 基础动作与服务

基础层不因不属于用户任务而降为废弃。以下均保留实现，只调整公开目录与职责。

| 能力组 | 现有位置 | 建议 |
| --- | --- | --- |
| 鼠标、键盘、手柄、输入映射、进程与窗口 | `plans/aura_base/src/actions/input_actions.py`、`gamepad_actions.py`、`input_mapping_actions.py`、`process_actions.py`、`system_actions.py` | 保留为平台基础能力 |
| 模板、OCR、YOLO、组合查找与等待 | 同目录 `vision_actions.py`、`ocr_actions.py`、`yolo_actions.py`、`interaction_actions.py`、`wait_actions.py`、`template_actions.py` | 保留识别与操作支持；自检与后端探针的手动入口归开发工具 |
| 变量、断言、日志、字符串和数学 | `data_actions.py`、`atomic_actions.py` | 保留 DSL 支持，不增加普通用户任务 |
| 跨任务调用与长期数据 | `task_actions.py`、`persistent_data_actions.py`，`aura_core` 相应实现 | 保留；父任务应消费稳定业务返回，逐步摆脱对 `nodes.open_room`／`nodes.finish` 的依赖 |
| PC 桥接 app/controller/input | `plans/resonance_pc/src/services/bridge_app_service.py`、`bridge_controller_service.py`、`input_bridge_service.py` | 保留硬件与游戏窗口适配；不与业务流程混层 |
| 城市、行情、规划与结算 | PC services 中 `city_shop_data_pc_service.py`、`resonance_pc_market_data_service.py`、`resonance_pc_trade_*` | 保留；统一数据与算法契约，不把每个 solver 作为用户任务 |
| 深潜实体识别与隔离 worker | `deep_dive_entity_detector_service.py`、`_deep_dive_entity_*` | 保留正式识别依赖；研究入口与运行时 worker 分开 |
| 调度、引擎、取消、事件与运行门面 | `packages/aura_core`、`packages/aura_game` | 保留；本轮优先整业务层，不新增一套执行引擎 |

## 旧入口与兼容层清单

### GUI 外壳与重复状态

| 现有项 | 当前状态 | 建议 | 先迁移或保留什么 |
| --- | --- | --- | --- |
| `_build_navigation()` | 旧导航定义仍保留 | 移除 | 当前三项主导航已承担入口职责 |
| Commerce 总览壳 | 构建并连线，无当前主导航入口 | 移除 | 组合执行共享预算与交接能力先归统一流程策略 |
| Commerce 中的货运／客运编辑组件 | 参数面板被工作流程复用 | 保留 | 抽出编辑组件，不能连同旧总览整目录删除 |
| Battle 页面壳及旧页索引占位 | 编辑器已挂载工作流程 | 合并 | 保留战斗编辑器，移除旧页壳／索引兼容时同步导航 |
| Android 工作台页面、task tree、JSON 输入与旧队列入口 | 启动构建，普通主导航不展示 | 移除 | 从 PC 主程序移除旧注册和壳；Android 平台实现的去留单独决定 |
| 隐藏历史页 | 构建、刷新链仍在 | 合并 | 并入真正可用的日志／历史入口，保留历史数据 |
| `_commerce_active/_pending` 与 `_workflow_active/_pending` | 两套业务编排状态并存 | 合并 | 一个业务流程状态机；Bridge 的执行传输队列与 core 调度各保留清楚边界 |
| 小任务隐藏 category selector／legacy API | 隐藏但保留兼容 | 移除 | 同步调用方，开发入口显式管理 |
| 数据采集面板 | 隐藏普通列表但仍完整实例化 | 降级 | 开发入口按需构建，保留信号与任务链 |
| 设置中的日志与历史、更新、关于占位分类 | 当前只是文字占位 | 移除 | 移除空壳分类或接入实际功能；不删除更新、历史等已有底层能力 |

依据：[主窗口](../../packages/resonance_gui/main_window.py)、[Bridge](../../packages/resonance_gui/bridge.py)、[设置页](../../packages/resonance_gui/widgets/settings_hub_page.py)。

### 旧 Android 工作台注册 17 项

来源：[task_specs.py](../../packages/resonance_gui/task_specs.py)。这些引用属于 `GAME_NAME="resonance"`，不能作为 PC 任务清单使用。下表的“移除”首先指 PC GUI 中的旧注册；Android 实现仍按平台边界保留。

| task_id | 旧名称与引用 | 建议 | 目标归属 |
| --- | --- | --- | --- |
| `game_startup_enter_main` | 进入主界面／`game_startup.yaml:enter_main` | 移除 | PC 已有生命周期步骤；Android 留平台入口 |
| `game_startup_close_game` | 关闭游戏／`game_startup.yaml:close_game` | 移除 | 同上 |
| `player_data_refresh` | 刷新用户数据／`player_data.yaml:player_data_refresh` | 移除 | PC 已有对应工具 |
| `player_data_latest` | 缓存读取／`player_data.yaml:player_data_get_latest` | 降级 | 数据读取操作 |
| `market_refresh` | 行情刷新／`market_data.yaml:market_data_refresh` | 降级 | 行情服务 |
| `market_latest` | 最新快照／`market_data.yaml:market_data_get_latest` | 降级 | 数据读取 |
| `market_query_products` | 查询商品／`market_data.yaml:market_data_query_products` | 降级 | 查询／开发工具 |
| `trade_plan_next` | 下一步规划／`trade_planner.yaml:trade_plan_next` | 合并 | 规划服务 |
| `trade_plan_best_cycle` | 最佳循环／`trade_planner.yaml:trade_plan_best_cycle` | 合并 | 规划服务 |
| `trade_simulate` | 模拟／`trade_planner.yaml:trade_simulate` | 降级 | 算法开发工具 |
| `auto_cycle_trade` | 自动跑商／`auto_cycle_trade.yaml:auto_cycle_trade` | 移除 | PC 已有货运；Android 留平台入口 |
| `city_travel` | 城市旅行／`city_travel.yaml:intercity_select_destination` | 降级 | 业务操作 |
| `enter_city_shop` | 进商店／`city_shop.yaml:enter_city_shop` | 移除 | 旧注册引用的 key 在当前该 YAML 中不存在，不继续作为有效能力展示 |
| `buy_goods` | 买货／`buy_goods.yaml:buy_goods` | 降级 | 业务操作 |
| `sell_goods` | 卖货／`sell_goods.yaml:sell_all_goods` | 降级 | 业务操作 |
| `battle_input_preview` | 输入预览／`auto_battle_input_preview.yaml:auto_battle_input_preview` | 合并 | 战斗参数校验 |
| `battle_dispatch` | 自动战斗／`auto_battle_dispatch.yaml:auto_battle_dispatch` | 移除 | PC 已有战斗；Android 留平台入口 |

### Android YAML 实际能力覆盖

`plans/resonance/tasks/` 的 14 个文件共有 39 个任务定义。它们不全部出现在旧 17 项工作台注册中。平台是否继续支持尚未由本清单决定，因此建议先退出 PC 普通产品目录，不批量删除 Android 源码。

| 文件 | 现有 task key | 建议 |
| --- | --- | --- |
| `game_startup.yaml` | `enter_main`、`close_game` | 降级到平台专用任务目录 |
| `player_data.yaml` | `player_data_refresh`、`player_data_get_latest` | 合并数据契约，保留 Android 页面适配 |
| `market_data.yaml` | `market_data_refresh`、`market_data_sync_web_constants`、`market_data_get_latest`、`market_data_get_snapshot`、`market_data_list_snapshots`、`market_data_query_products` | 合并行情服务契约，查询／同步不作普通任务 |
| `trade_planner.yaml` | `trade_plan_next`、`trade_plan_best_cycle`、`trade_simulate` | 合并规划契约；simulate 降级开发工具 |
| `preview_trade_plan.yaml` | `preview_trade_plan` | 合并为平台货运预览模式 |
| `auto_cycle_trade.yaml` | `auto_cycle_trade` | 保留平台业务实现，退出 PC 普通入口 |
| `auto_cycle_trade_exact.yaml` | `auto_cycle_trade_exact` | 合并平台货运入口；先区分算法与旧流程差异 |
| `city_travel.yaml` | `intercity_select_destination` | 降级内部旅行操作 |
| `city_shop.yaml` | `open_city_panel_from_main`、`tap_back_once`、`go_city_main_direct`、`read_city_name_on_city_panel`、`click_city_shop_by_name`、`click_shop_menu_node` | 降级内部导航／商店操作 |
| `buy_goods.yaml` | `buy_goods` | 降级内部买货操作 |
| `sell_goods.yaml` | `sell_all_goods` | 降级内部卖货操作 |
| `auto_battle_input_preview.yaml` | `auto_battle_input_preview` | 合并平台战斗校验 |
| `auto_battle_combat.yaml` | `auto_battle_combat` | 降级共用战斗操作 |
| `auto_battle_dispatch.yaml` | `auto_battle_dispatch`；`auto_battle_ct_tie_an_run_one`、`auto_battle_ct_tie_an_batch`、`auto_battle_ct_regional_ops_run_one`、`auto_battle_ct_regional_ops_batch`、`auto_battle_ct_batch`、`auto_battle_gp_action_summary_run_difficulty`、`auto_battle_gp_action_summary_run_group`、`auto_battle_gp_action_summary_batch`、`auto_battle_gp_structural_run_one`、`auto_battle_gp_structural_batch`、`auto_battle_gp_batch` | 保留平台调度能力，内部 11 项归操作／批次；可共用目录契约，不直接混用 PC 坐标和页面识别 |

## 开发工具与维护入口清单

以下路径为仓库相对路径。分组列出的入口统一归开发／维护范围；“合并”主要合并入口和公共参数，不要求把各阶段算法塞进同一文件。素材生成器产物被正式运行使用，生成脚本本身不是用户任务。

### 通用运行和诊断

| 编号 | 现有入口或文件 | 建议 | 目标职责与边界 |
| --- | --- | --- | --- |
| D01 | `cli.py` 的 games、tasks、run、runs、run-detail；`scripts/run_cli.ps1` | 保留 | 无 GUI 的枚举、运行和排障入口；以后接入统一任务分类 |
| D02 | `cli.py doctor`、`tools/plan_doctor.py` | 合并 | 一个诊断总入口，保留运行环境诊断和 Plan 合规检查的不同职责 |
| D03 | `cli.py tui`、`packages/aura_core/cli/tui_app.py` | 降级 | 高级／开发终端，复用统一目录；保留无 GUI 使用价值 |
| D04 | TUI 早期 `_collect_inputs`、`_convert_input` 同名定义 | 移除候选 | 后续同名定义覆盖早期实现；只移除被覆盖定义，保留后续输入表单 |
| D05 | `tools/runtime_probe.py`、`capture_probe.py`、`input_profile_debugger.py`、`gpu_runtime_diagnostics.py`、`window_visibility_monitor.py` | 合并 | 诊断入口下分 runtime/capture/input/gpu/window；保留窗口持续监控模式 |
| D06 | `tools/log_analyzer.py` | 保留 | 日志解析，归诊断工具 |
| D07 | `tools/task_graph_viewer.py`、`state_map_inspector.py` | 合并 | 结构检查入口；保留任务 DAG 与状态迁移图两种模型 |
| D08 | `tools/quick_template_capture.py` | 保留 | 开发素材采集；复用窗口选择和截图底座 |
| D09 | `tools/yolo_workbench.py`、`yolo_project_lib.py`、`extract_video_frames_every_n.py` | 合并 | 视觉数据工具组；保留标注、数据集、导出及抽帧能力 |
| D10 | `tools/export_paddleocr_onnx.py` | 保留 | 模型维护链；运行时不依赖 Paddle 不等于可以删转换工具 |
| D11 | `tools/_shared.py`、`tools/__init__.py` | 保留 | 公共配置、窗口、路径与输出底座，不是重复入口 |
| D12 | `plans/aura_base/src/actions/windows_diagnostics_actions.py` 及模板／输入／YOLO 自检入口 | 降级 | 公开手动诊断只在开发目录发现；保留正式运行需要的底层服务 |

### 素材与数据生产

表中除特别说明外均位于 `tools/`。

| 编号 | 现有文件 | 建议 | 目标分组与边界 |
| --- | --- | --- | --- |
| D13 | `build_trade_product_templates.py`、`build_city_identity_templates.py`、`build_trailer_route_distances.py`、`generate_trade_rebargain_template.py` | 合并 | 商贸素材入口；图标、城市、距离、议价状态各保留产物 |
| D14 | `build_trade_goods_investment_cities.py`、`build_trade_goods_investment_templates.py`、`calibrate_trade_goods_investment_fonts.py`、`review_trade_goods_investment_cards.py`、`review_trade_goods_investment_templates.py`、`_trade_investment_native.py` | 合并 | 投资素材流水线；build/calibrate/review 和原生资源公共库分别保留 |
| D15 | `build_black_moon_local_shop_catalog.py`、`build_black_moon_local_shop_templates.py`、`build_black_moon_local_select_templates.py`、`build_black_moon_sold_out_template.py`、`black_moon_purchase_schema.py` | 合并 | 黑月目录、模板、状态和输入声明流水线；优先消除重复 schema 维护 |
| D16 | `build_raw_workshop_templates.py`、`build_raw_workshop_digit_templates.py`、`build_product_workshop_templates.py`、`build_product_workshop_staff_templates.py` | 合并 | 车间素材流水线；保留截图裁剪、数字渲染和补员模板的差异 |
| D17 | `generate_profile_status_digit_templates.py`、`generate_bento_count_templates.py`、`generate_bento_recovery_digit_templates.py` | 合并 | 公共数字渲染底座及业务 preset；不混用不同字体、ROI 和阈值 |
| D18 | `build_reforming_center_map.py`、`build_reforming_center_templates.py` | 保留 | 整顿中心地图标定与 UI 模板是不同产物，可共用入口 |
| D19 | `build_eternal_scuffle_assets.py` | 保留 | 无垠乱斗素材维护 |
| D20 | `sync_team_recommendations.py` | 降级 | 外部攻略同步归数据维护；运行时读取生成目录 |
| D21 | `build_deep_dive_vortex_catalog.py`、`build_deep_dive_event_templates.py`、`review_deep_dive_event_templates.py` | 合并 | 深潜事件素材流水线；候选 review 素材不等于运行时已接入 |

### 深潜研究与专项工具

[research/README.md](../../research/README.md) 明确区分当前四视角研究与历史扫描路线。研究模块和 `plans/resonance_pc/src/actions/_deep_dive_*` 中正式运行依赖的模块应分别处理。

| 编号 | 现有文件或入口 | 建议 | 保留依赖与退出条件 |
| --- | --- | --- | --- |
| D22 | `tools/plan_deep_dive_layout.py` | 降级 | 离线规划 CLI，调用已有正式算法；可与 GUI 离线规划统一入口 |
| D23 | `tools/deep_dive_four_view_servo_experiment.py`、`deep_dive_vertical_capture_broker.py`、`deep_dive_vertical_command.py` | 降级 | 显式研究工具，保留伺服／capture broker／命令协议；不是正式用户任务 |
| D24 | `research/deep_dive_four_view_template_alignment.py`、`deep_dive_four_view_pose_feedback.py`、`deep_dive_four_view_node_reader.py`、`deep_dive_four_view_reading_check.py` | 保留 | 当前研究依赖；保留开发身份，离线评分不作正式玩法成功门 |
| D25 | `research/deep_dive_four_view_arrival.py`、`deep_dive_anonymous_grid.py`、`deep_dive_pixel_glyph_proposals.py`、`deep_dive_glyph_lattice.py`、`deep_dive_response_fit.py`、`deep_dive_local_grip.py`、`deep_dive_multiplane_tracker.py`、`deep_dive_face_plane_tracker.py`、`deep_dive_face_view_quality.py`、`deep_dive_exposure_prefix.py`；`tools/deep_dive_front_view_experiment.py` | 移除候选 | 先降为历史复现；迁移 import 闭包和配套回归后成组退出，不能单删被引用模块 |
| D26 | `research/read_saved_front_view.py` | 合并 | 保存帧读取归研究 CLI；仍依赖旧 FacePlaneScanner，且当前 reading_check 复用其 detector 配置，是历史链退出前要迁移的依赖 |
| D27 | `tools/deep_dive_live_acceptance.py`、`deep_dive_acceptance.py` | 移除候选 | 旧七路线 harness 与证据账本先归历史工具；旧报告不能证明四视角方案，通用证据能力可提取保留 |

### 基准开发环境与发布维护

| 编号 | 现有入口或文件 | 建议 | 目标职责与依赖 |
| --- | --- | --- | --- |
| D28 | `plans/aura_benchmark` 的 single_sleep、serial_sleep、parallel_sleep；benchmark actions 与 probe service | 降级 | 开发基准包；当前 GUI self-check、build_preflight 及发布 Plan 收集有依赖，先迁移检查和包选择，再考虑移出正式包 |
| D29 | `scripts/run_resonance_auto_cycle_trade.py` | 移除候选 | 将 Windows 参数包装迁移到通用 `cli.py run --inputs-file`；与 Android 支持边界联动 |
| D30 | `scripts/run_resonance_mumu_probe.py` | 移除候选 | 先降级历史模拟器场景探针；平台退出或统一探针覆盖其场景后移除 |
| D31 | `scripts/setup_dev_environment.ps1`、`setup_python_runtime.ps1`、`build_preflight.ps1`、`fetch_mumu_runtime_assets.py` | 保留 | 环境安装、runtime、前置检查已有调用分层；MuMu 资源去留跟随平台决定 |
| D32 | `scripts/package_release.ps1`；内部 `build_release.ps1` | 保留 | package_release 为唯一正式发布入口，build_release 作为内部组装，不再平行对外说明 |
| D33 | `scripts/release/release_contract.py`、`pyinstaller_filters.py`、`assemble_release_plans.py`、`prune_release_payload.py`、`write_build_info.py`、`detect_release_scope.py` | 保留 | 内部发布契约、过滤、组装、裁剪、元信息与 CI 范围；不合成大脚本 |
| D34 | `scripts/release/verify_release_environment.py`、`validate_ocr_bundle.py`、`validate_windows_execution_level.py`、`validate_release.py`、`validate_release_set.py` | 保留 | 发布入口统一调度；环境、OCR、执行级别、单包及整套各有不同职责 |
| D35 | `scripts/update_release_locks.ps1`、`scripts/release/download_ocr_models.ps1`、`publish_ocr_model_release.ps1` | 降级 | 显式维护入口；保留锁文件和独立模型发布链 |
| D36 | `scripts/clean_workspace.ps1` | 保留 | 清生成物、缓存与环境；不用于决定研究源码或业务能力的去留 |

开发工具当前已由 `pyinstaller_filters.py` 排除于冻结 runtime；“开发专用”不能统一解读为还需要从所有发布介质删除。反过来，Plan 组装当前会收集有 manifest 的包，benchmark 不能只改菜单就宣称已退出发布。

## 移除候选与先行条件

| 候选对象 | 建议移除的具体内容 | 先行条件 |
| --- | --- | --- |
| 旧 PC GUI 外壳 | 旧导航、工作台壳、空页索引兼容、隐藏 category API、未接功能的设置占位分类 | 迁移实际信号、编辑组件、历史与更新入口；不删除数据和底层业务能力 |
| 旧商贸编排身份 | Commerce 总览壳、重复业务状态机、组合商贸的独立用户任务身份 | 统一编排层保留共享预算、恢复策略、交接验证，GUI 引用同步迁移 |
| 失效旧注册 | `task_specs.enter_city_shop` 的 `city_shop.yaml:enter_city_shop` 引用 | 当前 YAML 中没有该 key；若继续支持 Android 工作台，应改为有效操作，而非保留失效注册 |
| 重复检查包装 | `scuffle_checked_round/pair/advance` 的独立包装层 | 在相同业务边界保留 checkpoint 与失败传播 |
| 旧公开业务 action | 黑月一体化 purchase、生产车间 quick_fill | 明确公开消费者和语义差异；迁移或保留兼容入口后退出，不能只按静态无引用判断 |
| TUI 被覆盖定义 | 早期 `_collect_inputs`、`_convert_input` | 保留后续实际生效定义及所需输入行为 |
| 旧模拟器专用 wrapper/probe | 两个 `run_resonance_*` 脚本 | 输入文件与场景迁移，Android 平台范围明确 |
| 历史深潜研究／旧验收 | 已不作为当前路线的整组模块和 harness | 迁移保存帧读取及当前 research 配置依赖，源码与配套回归成组退出；保留需要的历史证据 |

以下对象不列为直接删除项：`single_run_test` 实现、扫描／规划核心、便当消费共享会话、`aura_benchmark` 整包、Android Plan 整包、整顿中心全部旧 action、识别模板及模型资源。它们分别存在正式调用、共享依赖或平台范围尚未决定的问题。

## 建议推进顺序

1. 统一目录身份：明确用户任务、用户辅助工具、内部操作、开发工具；将入口可见性与底层导出分开。
2. 收拢 GUI：保留复用编辑组件，统一业务编排状态，迁移旧壳与空入口。
3. 用整顿中心作为分层样本：用户任务调用业务操作，明确总览起点、房间终点、返回状态，父任务消费稳定业务结果。
4. 统一货运、客运、组合商贸的输入、结果与预算交接；保留预览只计算的边界。
5. 整理战斗批次与乱斗检查包装，保留玩法选择和 checkpoint 语义。
6. 整理开发入口和历史模块；深潜研究、Android 平台、benchmark 发布依赖按各自先行条件处理。

本轮交付仅为清单文档，不包含上述代码调整、文件删除或测试执行。
