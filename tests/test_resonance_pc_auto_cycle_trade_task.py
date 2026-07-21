from pathlib import Path

import yaml

from packages.aura_core.config.validator import validate_task_definition
from packages.aura_core.scheduler.validation import InputValidator


REPO_ROOT = Path(__file__).resolve().parents[1]
ADB_PLAN_ROOT = REPO_ROOT / "plans" / "resonance"
PC_PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"


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
    pc_task = pc_data["auto_cycle_trade_pc"]
    inputs = {item["name"]: item for item in pc_task["meta"]["inputs"]}

    assert set(pc_data) == {"auto_cycle_trade_pc"}
    assert pc_task["meta"]["title"] == "Exact Auto Trade (PC)"
    assert "max_cycle_hops" not in inputs
    assert "max_rounds" not in inputs
    assert inputs["all_plan"]["default"] == 0
    assert inputs["all_plan"]["enum"] == [0, 1]
    assert inputs["fatigue_budget"]["default"] == 100
    assert inputs["cargo_capacity"]["default"] == 650
    assert inputs["negotiation_budget"]["default"] == 0
    assert inputs["bargain_success_rates_bps"]["default"] == [5000]
    assert inputs["bargain_step_bps"]["default"] == 1000
    assert inputs["raise_success_rates_bps"]["default"] == [5000]
    assert inputs["raise_step_bps"]["default"] == 1000
    assert inputs["trade_level"]["default"] == 20
    assert inputs["city_prestige"]["default"] == {"default": 20, "overrides": {}}
    assert inputs["product_unlocks"]["default"] == {"mode": "all", "product_ids": []}
    assert "rounds" not in pc_task["returns"]
    assert "rounds_completed" not in pc_task["returns"]
    assert "city_cycle" not in pc_task["returns"]
    assert "entry_route_count" not in pc_task["returns"]
    assert "city_path" in pc_task["returns"]
    assert "expected_fatigue_used" in pc_task["returns"]
    assert "full_negotiation_used" in pc_task["returns"]
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
    assert defaults["all_plan"] == 0
    assert defaults["bargain_success_rates_bps"] == [5000]
    assert defaults["bargain_step_bps"] == 1000
    assert defaults["raise_success_rates_bps"] == [5000]
    assert defaults["raise_step_bps"] == 1000

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

    ok, error = validator.validate_inputs_against_meta(
        inputs_meta,
        {"city_prestige": {"default": 20, "overrides": {"6": 10}}},
    )

    assert ok is False
    assert "city_prestige.overrides" in error
    assert "unexpected fields: 6" in error

    ok, custom_negotiation = validator.validate_inputs_against_meta(
        inputs_meta,
        {
            "all_plan": 1,
            "bargain_success_rates_bps": [6300, 5300],
            "bargain_step_bps": 1170,
            "raise_success_rates_bps": [5000],
            "raise_step_bps": 1000,
        },
    )
    assert ok is True
    assert custom_negotiation["all_plan"] == 1
    assert custom_negotiation["bargain_success_rates_bps"] == [6300, 5300]

    for bad_inputs in (
        {"all_plan": 2},
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
    assert "refresh_market" not in inputs
    assert "use_fatigue_medicine" not in {item["name"] for item in task["meta"]["inputs"]}
    assert task["returns"]["preview"] == "{{ nodes.run.output.preview }}"


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
    assert (PC_PLAN_ROOT / "data" / "cache" / "market").is_dir()


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
        "all_plan",
        "bargain_success_rates_bps",
        "bargain_step_bps",
        "raise_success_rates_bps",
        "raise_step_bps",
    }
    for action_name in (
        "resonance_pc.trade_plan_optimal_route",
        "resonance_pc.preview_trade_plan_flow",
        "resonance_pc.auto_cycle_trade_flow",
    ):
        parameters = {
            parameter["name"]: parameter
            for parameter in actions_by_name[action_name]["parameters"]
        }
        parameter_names = set(parameters)
        assert expected_negotiation_parameters.issubset(parameter_names)
        assert parameters["all_plan"]["default"] == 0
        assert parameters["bargain_success_rates_bps"]["default"] == [5000]
        assert parameters["bargain_step_bps"]["default"] == 1000
        assert parameters["raise_success_rates_bps"]["default"] == [5000]
        assert parameters["raise_step_bps"]["default"] == 1000

    task_ids = {item["id"] for item in exports["tasks"]}
    assert "auto_cycle_trade_pc" in task_ids
    assert "preview_trade_plan_pc" in task_ids
    assert "auto_battle_dispatch_pc" in task_ids
