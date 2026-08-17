from pathlib import Path

import yaml

from packages.aura_core.config.validator import validate_task_definition
from packages.aura_core.scheduler.validation import InputValidator


REPO_ROOT = Path(__file__).resolve().parents[1]
ADB_PLAN_ROOT = REPO_ROOT / "plans" / "resonance"
PC_PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"
ALL_CITY_IDS = [str(city_id) for city_id in range(1, 22)]
DEFAULT_AVAILABLE_CITY_IDS = list(ALL_CITY_IDS)


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_resonance_pc_runtime_defaults_to_wgc_and_sendinput():
    adb_config = _load_yaml(ADB_PLAN_ROOT / "config.yaml")
    pc_config = _load_yaml(PC_PLAN_ROOT / "config.yaml")

    assert adb_config["runtime"]["family"] == "android_emulator"
    assert adb_config["runtime"]["capture"]["backend"] == "scrcpy_stream"
    assert adb_config["runtime"]["input"]["backend"] == "android_touch"

    runtime = pc_config["runtime"]
    assert runtime["family"] == "windows_desktop"
    assert runtime["provider"] == "windows"
    assert runtime["target"]["process_name"] == "雷索纳斯.exe"
    assert runtime["target"]["class_name"] == "UnityWndClass"
    assert runtime["target"]["visibility_recovery"] == {
        "enabled": True,
        "grace_period_ms": 1000,
        "recovery_timeout_ms": 3000,
        "poll_interval_ms": 100,
    }
    assert runtime["capture"]["backend"] == "wgc"
    assert runtime["capture"]["capture_cursor"] is False
    assert runtime["input"]["backend"] == "sendinput"
    assert runtime["input"]["focus_before_input"] is True


def test_resonance_pc_task_uses_the_new_exact_planner_contract():
    pc_data = _load_yaml(PC_PLAN_ROOT / "tasks" / "auto_cycle_trade_pc.yaml")
    constraints = _load_yaml(
        PC_PLAN_ROOT / "data" / "meta" / "trade_constraints.json"
    )
    locations = _load_yaml(
        PC_PLAN_ROOT / "data" / "meta" / "location_pc.json"
    )
    pc_task = pc_data["auto_cycle_trade_pc"]
    inputs = {item["name"]: item for item in pc_task["meta"]["inputs"]}

    assert set(pc_data) == {"auto_cycle_trade_pc"}
    assert pc_task["meta"]["title"] == "Exact Auto Trade (PC)"
    assert "max_cycle_hops" not in inputs
    assert "max_rounds" not in inputs
    assert "all_plan" not in inputs
    assert "negotiation_budget" not in inputs
    assert inputs["fatigue_budget"]["default"] == 100
    assert inputs["cargo_capacity"]["default"] == 650
    assert inputs["negotiation_max_attempts"]["default"] == 5
    assert inputs["negotiation_max_attempts"]["min"] == 1
    assert inputs["negotiation_max_attempts"]["max"] == 6
    assert inputs["bargain_success_rates_bps"]["default"] == [5000]
    assert inputs["bargain_step_bps"]["default"] == 1000
    assert inputs["raise_success_rates_bps"]["default"] == [5000]
    assert inputs["raise_step_bps"]["default"] == 1000
    assert inputs["trade_level"]["default"] == 20
    assert inputs["auto_cape_island_investment"]["default"] is False
    assert inputs["arrival_timeout_seconds"]["default"] == 3600
    assert inputs["available_city_ids"]["default"] == DEFAULT_AVAILABLE_CITY_IDS
    assert inputs["available_city_ids"]["item"]["enum"] == ALL_CITY_IDS
    assert inputs["required_end_city_ids"]["required"] is False
    assert inputs["required_end_city_ids"]["min"] == 1
    assert inputs["required_end_city_ids"]["item"]["enum"] == ALL_CITY_IDS
    assert inputs["city_prestige"]["default"] == {"default": 20, "overrides": {}}
    assert list(
        inputs["city_prestige"]["properties"]["overrides"]["properties"]
    ) == ALL_CITY_IDS
    assert inputs["product_unlocks"]["default"] == {"mode": "all", "product_ids": []}
    assert constraints["allowed_city_ids"] == ALL_CITY_IDS
    assert constraints["default_available_city_ids"] == DEFAULT_AVAILABLE_CITY_IDS
    assert list(constraints["city_id_to_key"]) == ALL_CITY_IDS
    cities_with_exchange_coordinates = [
        city_id
        for city_id in ALL_CITY_IDS
        if "exchange"
        in locations["city"][constraints["city_id_to_key"][city_id]]
    ]
    assert cities_with_exchange_coordinates == DEFAULT_AVAILABLE_CITY_IDS
    assert "rounds" not in pc_task["returns"]
    assert "rounds_completed" not in pc_task["returns"]
    assert "city_cycle" not in pc_task["returns"]
    assert "entry_route_count" not in pc_task["returns"]
    assert "city_path" in pc_task["returns"]
    assert "required_end_city_ids" in pc_task["returns"]
    assert "selected_end_city_id" in pc_task["returns"]
    assert "expected_fatigue_used" in pc_task["returns"]
    assert "full_negotiation_used" in pc_task["returns"]
    assert "negotiation_max_attempts" in pc_task["returns"]
    assert "fatigue_used" not in pc_task["returns"]
    assert "remaining_fatigue" not in pc_task["returns"]
    assert "negotiation_used" not in pc_task["returns"]
    assert "execution" in pc_task["returns"]
    assert pc_task["steps"]["run"]["action"] == "resonance_pc.auto_cycle_trade_flow"
    assert not (ADB_PLAN_ROOT / "tasks" / "auto_cycle_trade_pc.yaml").exists()


def test_resonance_pc_exact_planner_dict_inputs_validate_defaults_and_overrides():
    pc_data = _load_yaml(PC_PLAN_ROOT / "tasks" / "auto_cycle_trade_pc.yaml")
    inputs_meta = pc_data["auto_cycle_trade_pc"]["meta"]["inputs"]
    validator = InputValidator(None)

    ok, defaults = validator.validate_inputs_against_meta(inputs_meta, {})

    assert ok is True
    assert defaults["city_prestige"] == {"default": 20, "overrides": {}}
    assert defaults["product_unlocks"] == {"mode": "all", "product_ids": []}
    assert "all_plan" not in defaults
    assert "negotiation_budget" not in defaults
    assert defaults["negotiation_max_attempts"] == 5
    assert defaults["bargain_success_rates_bps"] == [5000]
    assert defaults["bargain_step_bps"] == 1000
    assert defaults["raise_success_rates_bps"] == [5000]
    assert defaults["raise_step_bps"] == 1000
    assert defaults["available_city_ids"] == DEFAULT_AVAILABLE_CITY_IDS
    assert defaults["auto_cape_island_investment"] is False
    assert defaults["arrival_timeout_seconds"] == 3600

    ok, custom = validator.validate_inputs_against_meta(
        inputs_meta,
        {
            "city_prestige": {"default": 15, "overrides": {"3": 12, "8": 10}},
            "product_unlocks": {"mode": "only", "product_ids": ["101", "205"]},
        },
    )

    assert ok is True
    assert custom["city_prestige"] == {
        "default": 15,
        "overrides": {"3": 12, "8": 10},
    }
    assert custom["product_unlocks"] == {
        "mode": "only",
        "product_ids": ["101", "205"],
    }

    ok, all_city_override = validator.validate_inputs_against_meta(
        inputs_meta,
        {"city_prestige": {"default": 20, "overrides": {"6": 10, "20": 12}}},
    )

    assert ok is True
    assert all_city_override["city_prestige"]["overrides"] == {
        "6": 10,
        "20": 12,
    }

    ok, error = validator.validate_inputs_against_meta(
        inputs_meta,
        {"city_prestige": {"default": 20, "overrides": {"22": 10}}},
    )

    assert ok is False
    assert "city_prestige.overrides" in error
    assert "unexpected fields: 22" in error

    ok, custom_negotiation = validator.validate_inputs_against_meta(
        inputs_meta,
        {
            "bargain_success_rates_bps": [6300, 5300],
            "bargain_step_bps": 1170,
            "raise_success_rates_bps": [5000],
            "raise_step_bps": 1000,
        },
    )
    assert ok is True
    assert custom_negotiation["bargain_success_rates_bps"] == [6300, 5300]

    for bad_inputs in (
        {"negotiation_max_attempts": 0},
        {"negotiation_max_attempts": 7},
        {"bargain_success_rates_bps": []},
        {"bargain_success_rates_bps": [10001]},
        {"raise_step_bps": 0},
    ):
        ok, _error = validator.validate_inputs_against_meta(inputs_meta, bad_inputs)
        assert ok is False


def test_resonance_pc_auto_cycle_trade_task_matches_formal_task_schema():
    pc_data = _load_yaml(PC_PLAN_ROOT / "tasks" / "auto_cycle_trade_pc.yaml")

    ok, error = validate_task_definition(pc_data)

    assert ok is True, error


def test_resonance_pc_preview_trade_plan_task_is_planning_only_and_valid():
    pc_data = _load_yaml(PC_PLAN_ROOT / "tasks" / "preview_trade_plan_pc.yaml")
    task = pc_data["preview_trade_plan_pc"]

    ok, error = validate_task_definition(pc_data)

    assert ok is True, error
    assert list(task["steps"]) == ["run"]
    assert task["steps"]["run"]["action"] == "resonance_pc.preview_trade_plan_flow"
    inputs = {item["name"]: item for item in task["meta"]["inputs"]}
    assert inputs["start_city_id"]["required"] is True
    assert inputs["start_city_id"]["enum"] == ALL_CITY_IDS
    assert inputs["available_city_ids"]["default"] == DEFAULT_AVAILABLE_CITY_IDS
    assert inputs["available_city_ids"]["item"]["enum"] == ALL_CITY_IDS
    assert inputs["required_end_city_ids"]["required"] is False
    assert inputs["required_end_city_ids"]["item"]["enum"] == ALL_CITY_IDS
    assert list(
        inputs["city_prestige"]["properties"]["overrides"]["properties"]
    ) == ALL_CITY_IDS
    assert "refresh_market" not in inputs
    assert "all_plan" not in inputs
    assert "negotiation_budget" not in inputs
    assert "use_fatigue_medicine" not in {item["name"] for item in task["meta"]["inputs"]}
    assert task["returns"]["preview"] == "{{ nodes.run.output.preview }}"
    assert task["returns"]["required_end_city_ids"] == (
        "{{ nodes.run.output.required_end_city_ids }}"
    )
    assert task["returns"]["selected_end_city_id"] == (
        "{{ nodes.run.output.selected_end_city_id }}"
    )


def test_resonance_pc_business_sources_and_assets_are_physically_separate():
    source_files = [
        "src/actions/city_trade_flow_pc_actions.py",
        "src/actions/city_travel_pc_actions.py",
        "src/actions/market_data_pc_actions.py",
        "src/actions/purchase_book_pc_actions.py",
        "src/actions/trade_negotiation_pc_actions.py",
        "src/actions/trade_planner_pc_actions.py",
        "src/services/city_shop_data_pc_service.py",
        "src/services/resonance_pc_market_data_service.py",
        "src/services/resonance_pc_trade_planner_service.py",
        "src/services/resonance_pc_trade_exact_solver.py",
    ]
    for relative_path in source_files:
        source_path = PC_PLAN_ROOT / relative_path
        assert source_path.is_file()
        source = source_path.read_text(encoding="utf-8")
        assert "plans.resonance." not in source
        assert 'name="resonance.' not in source

    required_templates = [
        "nav_back_button.png",
        "nav_city_main_button.png",
        "buy_settlement_scale_badge.png",
        "sell_settlement_scale_badge.png",
        "go_destination_button.png",
        "purchase_book_confirm_button.png",
    ]
    for filename in required_templates:
        adb_asset = ADB_PLAN_ROOT / "templates" / filename
        pc_asset = PC_PLAN_ROOT / "templates" / filename
        assert adb_asset.is_file()
        assert pc_asset.is_file()
        assert adb_asset.resolve() != pc_asset.resolve()
        assert not pc_asset.is_symlink()

    for filename in (
        "trade_buy_bargain_button.png",
        "trade_buy_cap20_digits.png",
        "trade_sell_raise_button.png",
        "trade_sell_cap20_digits.png",
    ):
        pc_asset = PC_PLAN_ROOT / "templates" / filename
        assert pc_asset.is_file()
        assert not pc_asset.is_symlink()

    assert (PC_PLAN_ROOT / "data" / "meta" / "location_pc.json").is_file()


def test_resonance_pc_manifest_exports_only_pc_business_symbols():
    manifest = _load_yaml(PC_PLAN_ROOT / "manifest.yaml")
    exports = manifest["exports"]

    service_names = {item["name"] for item in exports["services"]}
    assert service_names == {
        "resonance_pc_city_shop_data",
        "resonance_pc_market_data",
        "resonance_pc_trade_planner",
    }
    assert all(item["module"].startswith("plans.resonance_pc.") for item in exports["services"])
    assert all(item["name"].startswith("resonance_pc.") for item in exports["actions"])
    assert all(item["module"].startswith("plans.resonance_pc.") for item in exports["actions"])

    actions_by_name = {item["name"]: item for item in exports["actions"]}
    expected_negotiation_parameters = {
        "bargain_success_rates_bps",
        "bargain_step_bps",
        "raise_success_rates_bps",
        "raise_step_bps",
    }
    for action_name in ("resonance_pc.preview_trade_plan_flow", "resonance_pc.auto_cycle_trade_flow"):
        parameters = {
            parameter["name"]: parameter
            for parameter in actions_by_name[action_name]["parameters"]
        }
        parameter_names = set(parameters)
        assert expected_negotiation_parameters.issubset(parameter_names)
        assert "all_plan" not in parameter_names
        assert "negotiation_budget" not in parameter_names
        assert parameters["bargain_success_rates_bps"]["default"] == [5000]
        assert parameters["bargain_step_bps"]["default"] == 1000
        assert parameters["raise_success_rates_bps"]["default"] == [5000]
        assert parameters["raise_step_bps"]["default"] == 1000

    planner_parameters = {
        parameter["name"]: parameter
        for parameter in actions_by_name["resonance_pc.trade_plan_optimal_route"]["parameters"]
    }
    assert expected_negotiation_parameters.issubset(planner_parameters)
    assert planner_parameters["all_plan"]["default"] == 0
    assert planner_parameters["negotiation_budget"]["default"] == 0

    for action_name in (
        "resonance_pc.auto_cycle_trade_flow",
        "resonance_pc.buy_goods_on_buy_page",
        "resonance_pc.sell_goods_on_sell_page",
    ):
        parameters = {
            parameter["name"]: parameter
            for parameter in actions_by_name[action_name]["parameters"]
        }
        assert parameters["negotiation_max_attempts"]["default"] == 5

    for action_name in (
        "resonance_pc.trade_plan_optimal_route",
        "resonance_pc.preview_trade_plan_flow",
    ):
        parameter_names = {
            parameter["name"]
            for parameter in actions_by_name[action_name]["parameters"]
        }
        assert "negotiation_max_attempts" not in parameter_names

    task_ids = {item["id"] for item in exports["tasks"]}
    assert "auto_cycle_trade_pc" in task_ids
    assert "preview_trade_plan_pc" in task_ids
    assert "auto_battle_dispatch_pc" in task_ids
