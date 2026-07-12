from pathlib import Path

import yaml


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


def test_resonance_pc_task_keeps_the_adb_contract_but_uses_an_isolated_action():
    adb_task = _load_yaml(ADB_PLAN_ROOT / "tasks" / "auto_cycle_trade.yaml")["auto_cycle_trade"]
    pc_data = _load_yaml(PC_PLAN_ROOT / "tasks" / "auto_cycle_trade_pc.yaml")
    pc_task = pc_data["auto_cycle_trade_pc"]

    assert set(pc_data) == {"auto_cycle_trade_pc"}
    assert pc_task["meta"]["title"] == "Auto Cycle Trade (PC)"
    assert pc_task["meta"]["inputs"] == adb_task["meta"]["inputs"]
    assert pc_task["returns"] == adb_task["returns"]
    assert pc_task["steps"]["run"]["action"] == "resonance_pc.auto_cycle_trade_flow"
    assert not (ADB_PLAN_ROOT / "tasks" / "auto_cycle_trade_pc.yaml").exists()


def test_resonance_pc_business_sources_and_assets_are_physically_separate():
    source_files = [
        "src/actions/city_trade_flow_pc_actions.py",
        "src/actions/city_travel_pc_actions.py",
        "src/actions/market_data_pc_actions.py",
        "src/actions/purchase_book_pc_actions.py",
        "src/actions/trade_planner_pc_actions.py",
        "src/services/city_shop_data_pc_service.py",
        "src/services/resonance_pc_market_data_service.py",
        "src/services/resonance_pc_trade_planner_service.py",
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

    task_ids = {item["id"] for item in exports["tasks"]}
    assert "auto_cycle_trade_pc" in task_ids
