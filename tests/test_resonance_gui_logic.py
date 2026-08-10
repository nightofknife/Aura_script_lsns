from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import (
    DEFAULT_BATTLE_INPUTS,
    DEFAULT_PC_TRADE_CITY_IDS,
    PC_TRADE_CITY_OPTIONS,
    GuiPreferences,
    ResonanceConfigRepository,
)
from packages.resonance_gui.logic import (
    GAME_NAME,
    PC_BATTLE_PREVIEW_TASK_REF,
    PC_BATTLE_TASK_REF,
    PC_GAME_NAME,
    PC_TRADE_PREVIEW_TASK_REF,
    TRADE_PROGRESS_EVENT,
    TRADE_PROGRESS_SCHEMA,
    TradeProgressState,
    expected_profit_per_fatigue,
    parse_inputs_json,
    reduce_trade_progress,
    render_result_text,
    route_product_lines,
    trade_result_summary,
)
from packages.resonance_gui.task_specs import CATEGORIES, TASKS_BY_ID, WORKBENCH_TASKS


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.run_status = "running"
        self.events: list[dict] = []

    def list_tasks(self, game_name: str):
        self.calls.append(("list_tasks", {"game_name": game_name}))
        return [{"task_ref": task.task_ref} for task in WORKBENCH_TASKS]

    def list_runs(self, **kwargs):
        self.calls.append(("list_runs", dict(kwargs)))
        return []

    def run_task(self, **kwargs):
        self.calls.append(("run_task", dict(kwargs)))
        return {"cid": "cid-1", "status": "queued"}

    def poll_events(self, **kwargs):
        self.calls.append(("poll_events", dict(kwargs)))
        events, self.events = self.events, []
        return events

    def get_run(self, cid: str):
        self.calls.append(("get_run", {"cid": cid}))
        return {"cid": cid, "status": self.run_status, "final_result": {"user_data": {"status": "completed"}}}

    def cancel_task(self, cid: str):
        self.calls.append(("cancel_task", {"cid": cid}))
        return {"status": "success"}

    def target_status(self, **kwargs):
        self.calls.append(("target_status", dict(kwargs)))
        return {"ok": True, "target": {"hwnd": 1, "title": "雷索纳斯", "visible": True}}

    def close(self) -> None:
        self.calls.append(("close", {}))


def test_task_specs_cover_resonance_groups():
    assert GAME_NAME == "resonance"
    assert set(CATEGORIES) == {"启动", "用户数据", "市场数据", "跑商规划", "自动跑商", "城市操作", "战斗调度"}
    assert TASKS_BY_ID["player_data_refresh"].task_ref == "tasks:player_data.yaml:player_data_refresh"
    assert TASKS_BY_ID["auto_cycle_trade"].task_ref == "tasks:auto_cycle_trade.yaml:auto_cycle_trade"
    assert TASKS_BY_ID["battle_dispatch"].task_ref == "tasks:auto_battle_dispatch.yaml:auto_battle_dispatch"


def test_parse_inputs_json_requires_object():
    assert parse_inputs_json('{"force": false}') == {"force": False}

    try:
        parse_inputs_json("[1, 2]")
    except ValueError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("list payload should be rejected")


def test_render_result_text_uses_nested_final_result():
    text = render_result_text(
        {
            "dispatch": {"cid": "cid-1"},
            "run": {
                "summary": {"cid": "cid-1", "status": "success"},
                "detail": {"final_result": {"profit": 123}},
            },
        }
    )

    assert "cid-1" in text
    assert '"profit": 123' in text


def test_runner_bridge_requests_resonance_task():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    finished: list[dict] = []
    bridge.taskFinished.connect(finished.append)

    bridge.initialize()
    bridge.enqueue_task("tasks:market_data.yaml:market_data_get_latest", {}, "latest", 30.0)

    run_call = [call for call in fake.calls if call[0] == "run_task"][0][1]
    assert run_call["game_name"] == "resonance"
    assert run_call["task_ref"] == "tasks:market_data.yaml:market_data_get_latest"
    assert run_call["wait"] is False
    assert bridge.current_cid == "cid-1"

    fake.run_status = "success"
    bridge.poll_current()

    assert finished


def test_runner_bridge_preserves_zero_timeout_for_infinite_wait():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    finished: list[dict] = []
    bridge.taskFinished.connect(finished.append)

    bridge.initialize()
    bridge.enqueue_task("tasks:auto_cycle_trade.yaml:auto_cycle_trade", {}, "cycle", 0.0)

    run_call = [call for call in fake.calls if call[0] == "run_task"][0][1]
    assert run_call["timeout_sec"] == 0.0
    assert run_call["wait"] is False


def test_runner_bridge_cancel_does_not_finish_until_terminal():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    finished: list[dict] = []
    cancelled: list[dict] = []
    bridge.taskFinished.connect(finished.append)
    bridge.cancelRequested.connect(cancelled.append)
    bridge.run_pc_trade({"fatigue_budget": 300}, 0.0)

    bridge.cancel_current()

    assert cancelled and not finished
    assert len([call for call in fake.calls if call[0] == "cancel_task"]) == 1
    bridge.cancel_current()
    assert len([call for call in fake.calls if call[0] == "cancel_task"]) == 1

    fake.run_status = "cancelled"
    bridge.poll_current()
    assert finished


def test_runner_bridge_dispatches_preview_without_execution_only_inputs():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    dispatched: list[dict] = []
    bridge.taskDispatched.connect(dispatched.append)

    bridge.preview_pc_trade(
        {
            "fatigue_budget": 300,
            "start_city_id": "3",
            "negotiation_max_attempts": 6,
            "use_fatigue_medicine": True,
            "allowed_fatigue_medicines": ["药"],
            "fatigue_medicine_max_uses": 4,
            "arrival_timeout_seconds": 2700,
            "auto_cape_island_investment": True,
        },
        0.0,
    )

    run_call = [call for call in fake.calls if call[0] == "run_task"][0][1]
    assert run_call["game_name"] == PC_GAME_NAME
    assert run_call["task_ref"] == PC_TRADE_PREVIEW_TASK_REF
    assert run_call["inputs"] == {"fatigue_budget": 300, "start_city_id": "3"}
    assert dispatched[0]["item"]["kind"] == "trade_preview"


def test_runner_bridge_removes_preview_start_city_from_real_trade_inputs():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)

    bridge.run_pc_trade(
        {
            "fatigue_budget": 300,
            "start_city_id": "3",
            "negotiation_max_attempts": 6,
            "arrival_timeout_seconds": 2700,
            "auto_cape_island_investment": True,
        },
        0.0,
    )

    run_call = [call for call in fake.calls if call[0] == "run_task"][0][1]
    assert run_call["inputs"] == {
        "fatigue_budget": 300,
        "negotiation_max_attempts": 6,
        "arrival_timeout_seconds": 2700,
        "auto_cape_island_investment": True,
    }


def test_runner_bridge_dispatches_pc_battle_to_dedicated_plan():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    dispatched: list[dict] = []
    bridge.taskDispatched.connect(dispatched.append)
    inputs = {
        "jobs": [{"route_id": "gp.action_summary.global_supply.magic", "difficulty": 4}],
        "stop_on_failure": True,
    }

    bridge.run_pc_battle(inputs, 0.0)

    run_call = [call for call in fake.calls if call[0] == "run_task"][0][1]
    assert run_call["game_name"] == PC_GAME_NAME
    assert run_call["task_ref"] == PC_BATTLE_TASK_REF
    assert run_call["inputs"] == inputs
    assert dispatched[0]["item"]["kind"] == "battle_run"


def test_runner_bridge_dispatches_battle_validation_without_game_actions():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    dispatched: list[dict] = []
    bridge.taskDispatched.connect(dispatched.append)
    inputs = {
        "jobs": [{"route_id": "gp.structural_exploration.disordered_roots"}],
        "stop_on_failure": False,
    }

    bridge.validate_pc_battle(inputs, 0.0)

    run_call = [call for call in fake.calls if call[0] == "run_task"][0][1]
    assert run_call["game_name"] == PC_GAME_NAME
    assert run_call["task_ref"] == PC_BATTLE_PREVIEW_TASK_REF
    assert run_call["inputs"] == inputs
    assert dispatched[0]["item"]["kind"] == "battle_preview"


def test_runner_bridge_filters_pc_trade_progress_by_cid():
    fake = FakeRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    received: list[dict] = []
    bridge.tradeProgress.connect(received.append)
    bridge.run_pc_trade({}, 0.0)
    fake.events = [
        {
            "name": TRADE_PROGRESS_EVENT,
            "payload": {"schema": TRADE_PROGRESS_SCHEMA, "cid": "other", "sequence": 1},
        },
        {
            "name": TRADE_PROGRESS_EVENT,
            "payload": {"schema": TRADE_PROGRESS_SCHEMA, "cid": "cid-1", "sequence": 2},
        },
    ]

    bridge.poll_current()

    assert len(received) == 1
    assert received[0]["payload"]["sequence"] == 2


def test_runner_bridge_stops_after_repeated_poll_failures():
    class BrokenRunner(FakeRunner):
        def get_run(self, cid: str):
            raise RuntimeError("channel closed")

    fake = BrokenRunner()
    bridge = RunnerBridge(runner_factory=lambda: fake)
    failures: list[dict] = []
    bridge.taskFailed.connect(failures.append)
    bridge.run_pc_trade({}, 0.0)

    bridge.poll_current()
    bridge.poll_current()
    bridge.poll_current()

    assert failures[-1]["recoverable"] is False
    assert failures[-1]["attempt"] == 3
    assert bridge.current_cid == ""
    assert bridge.busy is False


def test_config_repository_uses_resonance_settings(tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    repo = ResonanceConfigRepository(settings=settings)

    assert repo.load_preferences().timeout_sec == 0.0

    repo.save_preferences(GuiPreferences(timeout_sec=42.0, history_limit=7, last_task_id="market_latest"))
    loaded = repo.load_preferences()

    assert loaded.timeout_sec == 42.0
    assert loaded.history_limit == 7
    assert loaded.last_task_id == "market_latest"

    trade_inputs = repo.load_trade_inputs()
    assert trade_inputs["auto_cape_island_investment"] is False
    assert trade_inputs["cargo_capacity"] == 650
    assert trade_inputs["start_city_id"] == ""
    assert trade_inputs["negotiation_max_attempts"] == 5
    assert trade_inputs["arrival_timeout_seconds"] == 1800
    assert trade_inputs["available_city_ids"] == DEFAULT_PC_TRADE_CITY_IDS
    assert "all_plan" not in trade_inputs
    assert "negotiation_budget" not in trade_inputs
    trade_inputs["fatigue_budget"] = 300
    trade_inputs["all_plan"] = 1
    trade_inputs["negotiation_budget"] = 9
    trade_inputs["available_city_ids"] = ["3", "1"]
    trade_inputs["start_city_id"] = "3"
    repo.save_trade_inputs(trade_inputs)
    trade_inputs["auto_cape_island_investment"] = True
    trade_inputs["arrival_timeout_seconds"] = 2700
    repo.save_trade_inputs(trade_inputs)
    assert repo.load_trade_inputs()["auto_cape_island_investment"] is True
    assert repo.load_trade_inputs()["fatigue_budget"] == 300
    assert repo.load_trade_inputs()["arrival_timeout_seconds"] == 2700
    assert "all_plan" not in repo.load_trade_inputs()
    assert "negotiation_budget" not in repo.load_trade_inputs()
    assert repo.load_trade_inputs()["available_city_ids"] == ["3", "1"]
    assert repo.load_trade_inputs()["start_city_id"] == "3"

    assert repo.load_battle_inputs() == DEFAULT_BATTLE_INPUTS
    battle_inputs = {
        "jobs": [
            {
                "route_id": "ct.regional_ops_center.wilderness_station",
                "difficulty": 3,
                "threat_level": 101,
            }
        ],
        "stop_on_failure": False,
    }
    repo.save_battle_inputs(battle_inputs)
    assert repo.load_battle_inputs() == battle_inputs


def test_config_repository_migrates_legacy_default_city_selection(tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue(
        "trade/inputs_json",
        json.dumps(
            {
                "available_city_ids": ["3", "4", "1", "5", "7", "8", "9", "2"],
            },
            ensure_ascii=False,
        ),
    )
    repo = ResonanceConfigRepository(settings=settings)

    assert [city_id for city_id, _name in PC_TRADE_CITY_OPTIONS] == DEFAULT_PC_TRADE_CITY_IDS
    assert repo.load_trade_inputs()["available_city_ids"] == DEFAULT_PC_TRADE_CITY_IDS


def test_config_repository_migrates_previous_pc_default_city_selection(tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    previous_default = [
        city_id
        for city_id in (str(value) for value in range(1, 21))
        if city_id not in {"14", "17", "19"}
    ]
    settings.setValue(
        "trade/inputs_json",
        json.dumps({"available_city_ids": previous_default}, ensure_ascii=False),
    )

    repo = ResonanceConfigRepository(settings=settings)

    assert repo.load_trade_inputs()["available_city_ids"] == DEFAULT_PC_TRADE_CITY_IDS


def test_gui_trade_city_options_match_complete_location_data():
    plan_meta = REPO_ROOT / "plans" / "resonance_pc" / "data" / "meta"
    constraints = json.loads(
        (plan_meta / "trade_constraints.json").read_text(encoding="utf-8")
    )
    locations = json.loads(
        (plan_meta / "location_pc.json").read_text(encoding="utf-8")
    )["city"]
    complete_city_ids = [
        city_id
        for city_id in constraints["allowed_city_ids"]
        if "maploc" in locations[constraints["city_id_to_key"][city_id]]
    ]

    assert [city_id for city_id, _name in PC_TRADE_CITY_OPTIONS] == complete_city_ids
    assert DEFAULT_PC_TRADE_CITY_IDS == complete_city_ids
    assert all(
        "exchange" in locations[constraints["city_id_to_key"][city_id]]
        for city_id in complete_city_ids
    )


def test_trade_progress_reducer_rejects_foreign_and_stale_events():
    state = TradeProgressState(cid="cid-1")
    foreign = {
        "name": TRADE_PROGRESS_EVENT,
        "payload": {"schema": TRADE_PROGRESS_SCHEMA, "cid": "cid-2", "sequence": 1},
    }
    assert reduce_trade_progress(state, foreign, expected_cid="cid-1").sequence == -1

    planning = {
        "name": TRADE_PROGRESS_EVENT,
        "payload": {
            "schema": TRADE_PROGRESS_SCHEMA,
            "cid": "cid-1",
            "sequence": 4,
            "stage": "planning",
            "state": "completed",
            "snapshot_id": "snap-1",
            "data": {"route": [{"from_city": "A", "to_city": "B"}], "summary": {"expected_profit": 9}},
        },
    }
    reduced = reduce_trade_progress(state, planning, expected_cid="cid-1")
    assert reduced.route[0]["to_city"] == "B"
    assert reduced.summary["expected_profit"] == 9
    assert reduce_trade_progress(reduced, planning, expected_cid="cid-1").sequence == 4


def test_trade_result_summary_and_route_products_support_new_and_old_shapes():
    payload = {
        "cid": "cid-1",
        "status": "success",
        "final_result": {
            "user_data": {
                "status": "completed",
                "city_path": ["A", "B"],
                "expected_profit": 123,
                "market_source": "fallback_cache",
                "market_stale_reason": "network down",
                "warnings": ["event ignored"],
                "route": [
                    {
                        "from_city": "A",
                        "to_city": "B",
                        "buys": [{"product_name": "货物", "quantity": 7}],
                    }
                ],
            }
        },
    }
    summary = trade_result_summary(payload)
    assert summary["expected_profit"] == 123
    assert summary["final_city"] == "B"
    assert summary["warnings"] == ["行情更新失败，已使用本地市场快照。", "event ignored"]
    assert route_product_lines(summary["route"][0]) == ["货物 x7"]
    assert route_product_lines({"buy_products": ["旧货物"]}) == ["旧货物"]


def test_expected_profit_per_fatigue_handles_normal_and_zero_cost_plans():
    assert expected_profit_per_fatigue({"expected_profit": 900, "expected_fatigue_used": 30}) == 30.0
    assert expected_profit_per_fatigue({"expected_profit": 900, "expected_fatigue_used": 0}) is None
