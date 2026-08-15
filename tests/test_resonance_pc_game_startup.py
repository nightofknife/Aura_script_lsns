from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from packages.aura_core.config.validator import validate_task_definition
from plans.resonance_pc.src.actions import game_startup_pc_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "game_startup_pc.yaml"
MANIFEST_PATH = REPO_ROOT / "plans" / "resonance_pc" / "manifest.yaml"


def _main_state():
    return {
        "ok": True,
        "state": "main",
        "main": True,
        "matched": {"main": []},
        "item_count": 1,
    }


def _services():
    return {
        "process_manager": Mock(),
        "windows_diagnostics": Mock(),
        "app": Mock(),
        "ocr": Mock(),
    }


def _detected_state_for_texts(*texts):
    app = Mock()
    app.capture.return_value = SimpleNamespace(success=True, image=object())
    ocr = Mock()
    ocr.recognize_all.return_value = SimpleNamespace(
        results=[
            SimpleNamespace(text=text, center_point=(100 + index * 20, 100), confidence=0.99)
            for index, text in enumerate(texts)
        ]
    )
    return actions._detect_state(app, ocr, actions.DEFAULT_STARTUP_REGION)


def test_startup_state_treats_non_main_non_update_text_as_other():
    result = _detected_state_for_texts("资讯", "公告")

    assert result["state"] == "other"


def test_startup_state_detects_explicit_update_button():
    result = _detected_state_for_texts("点击任意位置", "立即更新")

    assert result["state"] == "update"
    assert result["matched"]["update"][0]["marker"] == "立即更新"


def test_startup_state_does_not_treat_percent_or_mb_as_update():
    result = _detected_state_for_texts("点击任意位置进入游戏", "下载已经完成", "71%", "128MB")

    assert result["state"] == "other"
    assert result["matched"]["update"] == []


def test_startup_state_prioritizes_main_over_update_button():
    result = _detected_state_for_texts("访问城市", "立即更新")

    assert result["state"] == "main"


def test_enter_main_launches_detached_process_before_using_app(monkeypatch, tmp_path):
    services = _services()
    executable = tmp_path / actions.PROCESS_NAME
    executable.touch()
    events = []
    target = {
        "hwnd": 101,
        "pid": 202,
        "process_name": actions.PROCESS_NAME,
    }

    monkeypatch.setattr(actions, "_resolve_target", lambda _service: None)
    monkeypatch.setattr(actions, "_matching_processes", lambda: [])
    monkeypatch.setattr(actions, "_resolve_executable", lambda _path, _running: executable)
    monkeypatch.setattr(actions, "_wait_for_target", lambda *_args: events.append("window") or target)
    monkeypatch.setattr(actions, "_detect_state", lambda *_args: events.append("capture") or _main_state())
    services["process_manager"].start_process.side_effect = (
        lambda **_kwargs: events.append("launch") or {"status": "success", "pid": 202}
    )

    result = actions.resonance_pc_enter_main(
        round_interval_sec=0,
        **services,
    )

    assert events == ["launch", "window", "capture"]
    assert result["success"] is True
    assert result["launched"] is True
    assert result["pid"] == 202
    services["process_manager"].start_process.assert_called_once_with(
        identifier="resonance_pc",
        executable_path=str(executable),
        cwd=str(executable.parent),
    )


def test_enter_main_does_not_launch_when_matching_process_is_starting(monkeypatch):
    services = _services()
    target = {"hwnd": 101, "pid": 202, "process_name": actions.PROCESS_NAME}
    target_results = iter([None, target])
    monkeypatch.setattr(actions, "_resolve_target", lambda _service: next(target_results))
    monkeypatch.setattr(
        actions,
        "_matching_processes",
        lambda: [{"pid": 202, "name": actions.PROCESS_NAME, "exe": "x", "create_time": 1.0}],
    )
    monkeypatch.setattr(actions, "_detect_state", lambda *_args: _main_state())
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(actions.psutil, "pid_exists", lambda pid: pid == 202)

    result = actions.resonance_pc_enter_main(
        window_timeout_sec=1,
        round_interval_sec=0,
        **services,
    )

    assert result["success"] is True
    assert result["launched"] is False
    services["process_manager"].start_process.assert_not_called()


def test_enter_main_clicks_fixed_point_for_every_other_state(monkeypatch):
    services = _services()
    target = {"hwnd": 101, "pid": 202, "process_name": actions.PROCESS_NAME}
    states = iter(
        [
            {"state": "other", "main": False, "matched": {}, "item_count": 1},
            _main_state(),
        ]
    )
    monkeypatch.setattr(actions, "_resolve_target", lambda _service: target)
    monkeypatch.setattr(actions, "_matching_processes", lambda: [])
    monkeypatch.setattr(actions, "_detect_state", lambda *_args: next(states))

    result = actions.resonance_pc_enter_main(
        round_interval_sec=0,
        **services,
    )

    assert result["success"] is True
    services["app"].click.assert_called_once_with(x=450, y=660)
    services["app"].press_key.assert_not_called()


def test_enter_main_clicks_detected_update_button_center(monkeypatch):
    services = _services()
    target = {"hwnd": 101, "pid": 202, "process_name": actions.PROCESS_NAME}
    states = iter(
        [
            {
                "state": "update",
                "main": False,
                "matched": {
                    "update": [
                        {"marker": "立即更新", "text": "立即更新", "center": [700, 500], "confidence": 0.99}
                    ]
                },
                "item_count": 1,
            },
            _main_state(),
        ]
    )
    monkeypatch.setattr(actions, "_resolve_target", lambda _service: target)
    monkeypatch.setattr(actions, "_matching_processes", lambda: [])
    monkeypatch.setattr(actions, "_detect_state", lambda *_args: next(states))

    result = actions.resonance_pc_enter_main(round_interval_sec=0, **services)

    assert result["success"] is True
    services["app"].click.assert_called_once_with(x=700, y=500)


def test_close_game_posts_wm_close_to_verified_target(monkeypatch):
    diagnostics = Mock()
    target = {"hwnd": 101, "pid": 202, "process_name": actions.PROCESS_NAME}
    monkeypatch.setattr(actions, "_resolve_target", lambda _service: target)
    monkeypatch.setattr(actions, "_matching_processes", lambda: [])
    monkeypatch.setattr(actions.win32gui, "IsWindow", lambda hwnd: hwnd == 101)
    post_message = Mock()
    monkeypatch.setattr(actions.win32gui, "PostMessage", post_message)
    monkeypatch.setattr(actions, "_wait_for_process_exit", lambda pid, timeout: pid == 202)

    result = actions.resonance_pc_close_game(
        graceful_timeout_sec=2,
        windows_diagnostics=diagnostics,
    )

    assert result["status"] == "stopped"
    assert result["method"] == "wm_close"
    post_message.assert_called_once_with(101, actions.win32con.WM_CLOSE, 0, 0)


def test_close_game_force_fallback_revalidates_process_name(monkeypatch):
    diagnostics = Mock()
    target = {"hwnd": 101, "pid": 202, "process_name": actions.PROCESS_NAME}
    process = Mock()
    process.name.return_value = actions.PROCESS_NAME
    monkeypatch.setattr(actions, "_resolve_target", lambda _service: target)
    monkeypatch.setattr(actions, "_matching_processes", lambda: [])
    monkeypatch.setattr(actions.win32gui, "IsWindow", lambda _hwnd: True)
    monkeypatch.setattr(actions.win32gui, "PostMessage", Mock())
    monkeypatch.setattr(actions, "_wait_for_process_exit", lambda *_args: False)
    monkeypatch.setattr(actions.psutil, "Process", lambda pid: process if pid == 202 else None)

    result = actions.resonance_pc_close_game(
        graceful_timeout_sec=0,
        force_after_timeout=True,
        windows_diagnostics=diagnostics,
    )

    assert result["method"] == "terminate"
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5.0)


def test_close_game_is_idempotent_when_game_is_absent(monkeypatch):
    monkeypatch.setattr(actions, "_resolve_target", lambda _service: None)
    monkeypatch.setattr(actions, "_matching_processes", lambda: [])

    result = actions.resonance_pc_close_game(windows_diagnostics=Mock())

    assert result["success"] is True
    assert result["status"] == "already_stopped"


def test_game_startup_action_metadata_declares_mutation_and_services():
    enter_meta = actions.resonance_pc_enter_main.__aura_action__
    close_meta = actions.resonance_pc_close_game.__aura_action__

    assert enter_meta["read_only"] is False
    assert enter_meta["services"] == {
        "process_manager": "plans/aura_base/process_manager",
        "windows_diagnostics": "plans/aura_base/windows_diagnostics",
        "app": "plans/aura_base/app",
        "ocr": "plans/aura_base/ocr",
    }
    assert close_meta["read_only"] is False
    assert close_meta["services"] == {"windows_diagnostics": "plans/aura_base/windows_diagnostics"}


def test_game_startup_task_schema_and_wiring():
    task_data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))

    ok, error = validate_task_definition(task_data)

    assert ok is True, error
    assert set(task_data) == {"enter_main", "close_game"}
    assert task_data["enter_main"]["steps"]["enter_main"]["action"] == "resonance_pc.enter_main"
    assert task_data["close_game"]["steps"]["close_game"]["action"] == "resonance_pc.close_game"
    assert {item["name"] for item in task_data["enter_main"]["meta"]["inputs"]} >= {
        "executable_path",
        "launch_if_not_running",
        "window_timeout_sec",
    }
    assert {item["name"] for item in task_data["enter_main"]["meta"]["inputs"]} == {
        "executable_path",
        "launch_if_not_running",
        "window_timeout_sec",
        "max_settle_rounds",
        "round_interval_sec",
    }
    assert {item["name"] for item in task_data["close_game"]["meta"]["inputs"]} == {
        "graceful_timeout_sec",
        "force_after_timeout",
    }


def test_game_startup_actions_are_exported_by_manifest():
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    action_names = {item["name"] for item in manifest["exports"]["actions"]}
    task_ids = {item["id"] for item in manifest["exports"]["tasks"]}

    assert {"resonance_pc.enter_main", "resonance_pc.close_game"} <= action_names
    assert {"game_startup_pc/enter_main", "game_startup_pc/close_game"} <= task_ids
