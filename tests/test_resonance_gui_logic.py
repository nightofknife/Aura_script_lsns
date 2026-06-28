from __future__ import annotations

from PySide6.QtCore import QSettings

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import GuiPreferences, ResonanceConfigRepository
from packages.resonance_gui.logic import GAME_NAME, parse_inputs_json, render_result_text
from packages.resonance_gui.task_specs import CATEGORIES, TASKS_BY_ID, WORKBENCH_TASKS


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_tasks(self, game_name: str):
        self.calls.append(("list_tasks", {"game_name": game_name}))
        return [{"task_ref": task.task_ref} for task in WORKBENCH_TASKS]

    def list_runs(self, **kwargs):
        self.calls.append(("list_runs", dict(kwargs)))
        return []

    def run_task(self, **kwargs):
        self.calls.append(("run_task", dict(kwargs)))
        return {
            "dispatch": {"cid": "cid-1", "status": "queued"},
            "run": {
                "summary": {"cid": "cid-1", "status": "success"},
                "detail": {"cid": "cid-1", "final_result": {"ok": True}},
            },
        }

    def close(self) -> None:
        self.calls.append(("close", {}))


def test_task_specs_cover_resonance_groups():
    assert GAME_NAME == "resonance"
    assert set(CATEGORIES) == {"市场数据", "跑商规划", "自动跑商", "城市操作", "战斗调度"}
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

    assert finished
    run_call = [call for call in fake.calls if call[0] == "run_task"][0][1]
    assert run_call["game_name"] == "resonance"
    assert run_call["task_ref"] == "tasks:market_data.yaml:market_data_get_latest"
    assert run_call["wait"] is True


def test_config_repository_uses_resonance_settings(tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    repo = ResonanceConfigRepository(settings=settings)

    repo.save_preferences(GuiPreferences(timeout_sec=42.0, history_limit=7, last_task_id="market_latest"))
    loaded = repo.load_preferences()

    assert loaded.timeout_sec == 42.0
    assert loaded.history_limit == 7
    assert loaded.last_task_id == "market_latest"
