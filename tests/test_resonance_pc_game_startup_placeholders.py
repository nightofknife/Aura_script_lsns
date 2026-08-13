from __future__ import annotations

from pathlib import Path

import yaml

from packages.aura_core.config.validator import validate_task_definition
from plans.resonance_pc.src.actions import game_startup_pc_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "game_startup_pc.yaml"
MANIFEST_PATH = REPO_ROOT / "plans" / "resonance_pc" / "manifest.yaml"


def test_enter_main_placeholder_is_safe_and_explicit():
    result = actions.resonance_pc_enter_main()
    metadata = actions.resonance_pc_enter_main.__aura_action__

    assert result == {
        "success": False,
        "status": "not_implemented",
        "reason": "pc_game_startup_not_implemented",
        "message": "PC 游戏启动与主界面恢复尚未实现。",
    }
    assert metadata["read_only"] is True
    assert metadata["services"] == {}


def test_close_game_placeholder_is_safe_and_explicit():
    result = actions.resonance_pc_close_game()
    metadata = actions.resonance_pc_close_game.__aura_action__

    assert result == {
        "success": False,
        "status": "not_implemented",
        "reason": "pc_game_close_not_implemented",
        "message": "PC 游戏关闭方式尚未确定。",
    }
    assert metadata["read_only"] is True
    assert metadata["services"] == {}


def test_game_startup_placeholder_task_schema_and_wiring():
    task_data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))

    ok, error = validate_task_definition(task_data)

    assert ok is True, error
    assert set(task_data) == {"enter_main", "close_game"}
    for task_name, action_name in (
        ("enter_main", "resonance_pc.enter_main"),
        ("close_game", "resonance_pc.close_game"),
    ):
        task = task_data[task_name]
        assert task["meta"]["entry_point"] is True
        assert task["meta"]["inputs"] == []
        assert task["steps"][task_name]["action"] == action_name
        assert set(task["returns"]) == {"success", "status", "reason", "message"}


def test_game_startup_placeholders_are_exported_by_manifest():
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    action_names = {item["name"] for item in manifest["exports"]["actions"]}
    task_ids = {item["id"] for item in manifest["exports"]["tasks"]}

    assert {"resonance_pc.enter_main", "resonance_pc.close_game"} <= action_names
    assert {"game_startup_pc/enter_main", "game_startup_pc/close_game"} <= task_ids
