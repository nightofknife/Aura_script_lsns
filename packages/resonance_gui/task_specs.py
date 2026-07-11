"""Workbench task definitions for the Resonance GUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TASK_EXECUTION_CHAINABLE = "chainable"
TASK_EXECUTION_INTERACTIVE = "interactive"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    category: str
    title: str
    task_ref: str
    description: str = ""
    default_inputs: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = TASK_EXECUTION_CHAINABLE


WORKBENCH_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        task_id="game_startup_enter_main",
        category="\u542f\u52a8",
        title="\u8fdb\u5165\u4e3b\u754c\u9762",
        task_ref="tasks:game_startup.yaml:enter_main",
        description="\u4ece\u684c\u9762\u3001\u6807\u9898\u9875\u6216\u4e2d\u9014\u754c\u9762\u63a8\u8fdb\u5230\u96f7\u7d22\u7eb3\u65af\u4e3b\u754c\u9762\u3002",
        default_inputs={
            "launch_from_home": True,
            "max_settle_rounds": 300,
            "fail_if_login_required": True,
        },
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="game_startup_close_game",
        category="\u542f\u52a8",
        title="\u5173\u95ed\u6e38\u620f",
        task_ref="tasks:game_startup.yaml:close_game",
        description="\u901a\u8fc7 ADB force-stop \u5173\u95ed\u96f7\u7d22\u7eb3\u65af\u6e38\u620f\u8fdb\u7a0b\u3002",
        default_inputs={
            "android_package": "com.hermes.goda",
            "timeout_sec": 10.0,
        },
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="player_data_refresh",
        category="用户数据",
        title="刷新用户数据",
        task_ref="tasks:player_data.yaml:player_data_refresh",
        description="安全读取账号、货币、位置、澄明度、疲劳值、货舱容量和恢复道具。",
        default_inputs={"persist": True, "enter_main_first": True},
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="player_data_latest",
        category="用户数据",
        title="读取用户数据缓存",
        task_ref="tasks:player_data.yaml:player_data_get_latest",
        description="读取本地最新用户数据缓存。",
    ),
    TaskSpec(
        task_id="market_refresh",
        category="市场数据",
        title="刷新市场数据",
        task_ref="tasks:market_data.yaml:market_data_refresh",
        description="抓取、标准化并缓存最新市场快照。",
        default_inputs={"force": False},
    ),
    TaskSpec(
        task_id="market_latest",
        category="市场数据",
        title="读取最新快照",
        task_ref="tasks:market_data.yaml:market_data_get_latest",
        description="读取本地最新市场快照。",
    ),
    TaskSpec(
        task_id="market_query_products",
        category="市场数据",
        title="查询商品",
        task_ref="tasks:market_data.yaml:market_data_query_products",
        description="按范围、城市或买卖方向查询商品数据。",
        default_inputs={"scope": None, "city_id": None, "side": None},
    ),
    TaskSpec(
        task_id="trade_plan_next",
        category="跑商规划",
        title="下一步规划",
        task_ref="tasks:trade_planner.yaml:trade_plan_next",
        description="基于当前城市、疲劳、预算和可用城市规划下一步。",
        default_inputs={
            "start_city_id": "",
            "fatigue_budget": 100,
            "book_budget": 0,
            "cargo_capacity": 120,
            "book_profit_threshold": 0,
            "available_city_ids": [],
            "station_product_whitelist": None,
            "snapshot_id": None,
            "current_holdings": None,
        },
    ),
    TaskSpec(
        task_id="trade_plan_best_cycle",
        category="跑商规划",
        title="最佳循环",
        task_ref="tasks:trade_planner.yaml:trade_plan_best_cycle",
        description="计算固定约束下的最佳收益循环。",
        default_inputs={
            "cargo_capacity": 120,
            "book_budget": 0,
            "book_profit_threshold": 0,
            "available_city_ids": None,
            "start_city_id": None,
            "max_cycle_hops": 6,
            "station_product_whitelist": None,
            "snapshot_id": None,
        },
    ),
    TaskSpec(
        task_id="trade_simulate",
        category="跑商规划",
        title="模拟",
        task_ref="tasks:trade_planner.yaml:trade_simulate",
        description="按滚动决策模拟跑商过程并输出 trace。",
        default_inputs={
            "start_city_id": "",
            "fatigue_budget": 100,
            "book_budget": 0,
            "cargo_capacity": 120,
            "book_profit_threshold": 0,
            "available_city_ids": [],
            "station_product_whitelist": None,
            "snapshot_id": None,
            "max_iterations": 128,
        },
    ),
    TaskSpec(
        task_id="auto_cycle_trade",
        category="自动跑商",
        title="自动循环跑商",
        task_ref="tasks:auto_cycle_trade.yaml:auto_cycle_trade",
        description="读取当前城市，规划循环并逐段执行。",
        default_inputs={
            "fatigue_budget": 100,
            "cargo_capacity": 120,
            "book_budget": 0,
            "book_profit_threshold": 0,
            "max_cycle_hops": 6,
            "max_rounds": 64,
            "use_fatigue_medicine": False,
            "allowed_fatigue_medicines": [],
            "fatigue_medicine_max_uses": 4,
        },
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="city_travel",
        category="城市操作",
        title="城市旅行",
        task_ref="tasks:city_travel.yaml:intercity_select_destination",
        description="选择目的城市并进入站点。",
        default_inputs={
            "to_city_name": "",
            "enter_station_timeout_seconds": 0,
            "use_fatigue_medicine": False,
            "allowed_fatigue_medicines": [],
            "fatigue_medicine_max_uses": 4,
        },
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="enter_city_shop",
        category="城市操作",
        title="进商店",
        task_ref="tasks:city_shop.yaml:enter_city_shop",
        description="进入当前城市的指定商店。",
        default_inputs={"shop_type": "exchange"},
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="buy_goods",
        category="城市操作",
        title="买货",
        task_ref="tasks:buy_goods.yaml:buy_goods",
        description="按商品名称列表执行购买。",
        default_inputs={"product_list": [], "books_used": 0},
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="sell_goods",
        category="城市操作",
        title="卖货",
        task_ref="tasks:sell_goods.yaml:sell_all_goods",
        description="卖出当前货物。",
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
    TaskSpec(
        task_id="battle_input_preview",
        category="战斗调度",
        title="输入预览",
        task_ref="tasks:auto_battle_input_preview.yaml:auto_battle_input_preview",
        description="校验并规范化自动战斗 jobs，不进入实际战斗。",
        default_inputs={
            "jobs": [
                {
                    "route_id": "gp.action_summary.global_supply.savior",
                    "difficulty": 1,
                }
            ],
            "stop_on_failure": True,
        },
    ),
    TaskSpec(
        task_id="battle_dispatch",
        category="战斗调度",
        title="自动战斗 Dispatch",
        task_ref="tasks:auto_battle_dispatch.yaml:auto_battle_dispatch",
        description="进入战斗终端，校验 jobs 并按类别调度。",
        default_inputs={
            "jobs": [
                {
                    "route_id": "gp.action_summary.global_supply.savior",
                    "difficulty": 1,
                }
            ],
            "stop_on_failure": True,
        },
        execution_mode=TASK_EXECUTION_INTERACTIVE,
    ),
)

CATEGORIES: tuple[str, ...] = tuple(dict.fromkeys(task.category for task in WORKBENCH_TASKS))
TASKS_BY_ID: dict[str, TaskSpec] = {task.task_id: task for task in WORKBENCH_TASKS}
TASKS_BY_REF: dict[str, TaskSpec] = {task.task_ref: task for task in WORKBENCH_TASKS}


def find_task(task_id: str) -> TaskSpec:
    return TASKS_BY_ID[str(task_id)]
